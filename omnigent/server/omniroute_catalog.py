"""Live OmniRoute combo catalog — surfaces runnable routing combos in the SPA.

This module is the bridge between the local OmniRoute runtime (``/v1/models``)
and the web UI's model picker. It is **not** the same surface
:mod:`omnigent.opencode_native_provider.fetch_omniroute_combo_models`
exposes to OpenCode's per-session model catalog — that path stays raw so
OpenCode's prompt-time provider model can name each combo verbatim. Here we
attach curated display names, the allowed reasoning-effort range, and the
explicit-approval requirement so the picker can render them the way an
operator would read them ("OmniRoute Coding Fast", not ``auto/coding:fast``).

The catalog is best-effort. When the OmniRoute endpoint is unreachable:

* **Live fetch fails, cache has entries** — the cached catalog is returned
  (``source == "cache"``). The UI keeps the last-known picker choices.
* **Live fetch fails, cache is empty** — the curated fallback catalog of the
  three required combos is returned (``source == "fallback_curated"``). The
  picker never goes empty.
* **Live fetch succeeds** — the catalog is returned with
  ``source == "live"`` and the cache is refreshed.

A ``cachetools.TTLCache`` keys on the bare base URL and a SHA-256 prefix of
the API key (never the raw key) so distinct credentials don't poison each
other's listings.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

import cachetools
import httpx

from omnigent.server.omniroute_routes import get_route_profile

_logger = logging.getLogger(__name__)


# ── Curated fallback ─────────────────────────────────────────────────────
#
# Even when the OmniRoute endpoint is unreachable, the UI must show the
# three "must-have" coding combos the team committed to. Their native ids
# are already in :data:`omnigent.server.omniroute_routes.NATIVE_OMNIROUTE_ROUTE_IDS`,
# so the picker can resolve them via the snapshot builder without a live
# fetch.
CURATED_COMBO_IDS: tuple[str, ...] = (
    "auto/best-coding",
    "auto/coding:fast",
    "auto/coding:reliable",
)
CURATED_COMBO_DISPLAY_NAMES: dict[str, str] = {
    "auto/best-coding": "OmniRoute Coding Best",
    "auto/coding:fast": "OmniRoute Coding Fast",
    "auto/coding:reliable": "OmniRoute Coding Reliable",
}


@dataclass(frozen=True)
class OmniRouteComboEntry:
    """A single catalog row the web UI's model picker consumes.

    Wire-shaped (``provider``/``kind`` are stored as literals so the snapshot
    builder can emit them verbatim) so the SPA never has to translate.

    :param id: Native OmniRoute combo id, e.g. ``"auto/coding:fast"``.
        Preserved verbatim — colons, slashes, brackets, dots all stay.
    :param display_name: Curated human label the picker shows.
    :param provider: Always ``"omniroute"`` for entries from this catalog.
    :param kind: Always ``"combo"`` — distinguishes a curated combo row
        from a concrete model entry on the picker.
    :param reasoning_efforts: Allowed reasoning-effort values for this
        combo, e.g. ``("medium", "high")``.
    :param max_reasoning_effort: Highest effort the combo accepts, e.g.
        ``"high"``.
    :param default_reasoning_effort: Recommended effort, e.g. ``"medium"``.
    :param requires_explicit_approval: ``True`` for combos the routing
        agent gates on a confirm step (e.g. pro routes).
    """

    id: str
    display_name: str
    provider: Literal["omniroute"]
    kind: Literal["combo"]
    reasoning_efforts: tuple[str, ...]
    max_reasoning_effort: str
    default_reasoning_effort: str
    requires_explicit_approval: bool

    def to_wire(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "provider": self.provider,
            "kind": self.kind,
            "reasoning_efforts": list(self.reasoning_efforts),
            "max_reasoning_effort": self.max_reasoning_effort,
            "default_reasoning_effort": self.default_reasoning_effort,
            "requires_explicit_approval": self.requires_explicit_approval,
        }


# ── Cache + transport ────────────────────────────────────────────────────

_FETCH_TIMEOUT_S = 5.0
# Five-minute TTL: long enough that an idle session never thrashes the
# OmniRoute endpoint, short enough that a real topology change (a combo
# added/removed) shows up without an explicit refresh.
_CATALOG_TTL_S = 300
_CATALOG_MAXSIZE = 32

_CatalogCacheKey = tuple[str, str]  # (base_url, credential_fingerprint)
_catalog_cache: cachetools.TTLCache[_CatalogCacheKey, list[OmniRouteComboEntry]] = (
    cachetools.TTLCache(
        maxsize=_CATALOG_MAXSIZE,
        ttl=_CATALOG_TTL_S,
    )
)
_catalog_cache_lock = threading.Lock()


def _credential_fingerprint(api_key: str | None) -> str:
    """Return a non-secret identity of *api_key* for cache keying.

    Two catalogs sharing the same base URL but different credentials can
    legitimately return different listings (e.g. a tenant-restricted
    deployment), so the cache key must carry credential identity without
    storing the secret. An empty key returns ``""`` so a key-less caller
    still participates in the cache.
    """
    if not api_key:
        return ""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]


def _cache_key_for(base_url: str, api_key: str | None) -> _CatalogCacheKey:
    return (base_url.rstrip("/"), _credential_fingerprint(api_key))


def _resolve_base_url(base_url: str | None) -> str:
    """Resolve the OmniRoute base URL — explicit arg wins, else env, else localhost default."""
    if base_url:
        return base_url.rstrip("/")
    env_url = os.environ.get("OMNIGENT_OMNIROUTE_BASE_URL")
    if env_url:
        return env_url.rstrip("/")
    return "http://127.0.0.1:20128/v1"


def _resolve_api_key(api_key: str | None) -> str | None:
    """Resolve the OmniRoute API key without logging it."""
    if api_key:
        return api_key
    for env_name in (
        "OMNIGENT_OMNIROUTE_API_KEY",
        "OMNIGENT_ROUTER_API_KEY",
        "OMNIROUTE_API_KEY",
    ):
        value = os.environ.get(env_name)
        if value:
            return value
    return None


def _curated_entry(combo_id: str) -> OmniRouteComboEntry:
    """Build the fallback-catalog entry for one curated combo id.

    Looks up the native profile so the picker shows the same allowed
    reasoning efforts as the runtime validator enforces. Falls back to a
    conservative medium..high range when the profile is missing — which
    today cannot happen for the curated ids, but guards against drift if
    the catalog constants are edited without re-syncing the profile data.
    """
    profile = get_route_profile(combo_id)
    display_name = CURATED_COMBO_DISPLAY_NAMES.get(combo_id, combo_id)
    if profile is not None:
        return OmniRouteComboEntry(
            id=combo_id,
            display_name=display_name,
            provider="omniroute",
            kind="combo",
            reasoning_efforts=tuple(profile.allowed_reasoning_efforts),
            max_reasoning_effort=profile.max_reasoning_effort,
            default_reasoning_effort=profile.default_reasoning_effort,
            requires_explicit_approval=profile.requires_explicit_approval,
        )
    # Defensive: the curated ids ARE in the catalog, so this is unreachable
    # in practice. Kept so a future catalog edit that drops a curated id
    # still yields a picker row instead of crashing the snapshot builder.
    return OmniRouteComboEntry(
        id=combo_id,
        display_name=display_name,
        provider="omniroute",
        kind="combo",
        reasoning_efforts=("medium", "high"),
        max_reasoning_effort="high",
        default_reasoning_effort="medium",
        requires_explicit_approval=False,
    )


def curated_combo_catalog() -> list[OmniRouteComboEntry]:
    """Return the curated fallback catalog (always non-empty)."""
    return [_curated_entry(combo_id) for combo_id in CURATED_COMBO_IDS]


def dedupe_preserve_order(items: list[OmniRouteComboEntry]) -> list[OmniRouteComboEntry]:
    """Drop duplicate ids, keeping the first occurrence."""
    seen: set[str] = set()
    out: list[OmniRouteComboEntry] = []
    for item in items:
        if item.id in seen:
            continue
        seen.add(item.id)
        out.append(item)
    return out


def _live_entry_from_catalog_entry(raw: Mapping[str, object]) -> OmniRouteComboEntry | None:
    """Translate a single ``/v1/models`` row into a combo catalog entry."""
    model_id = raw.get("id")
    if not isinstance(model_id, str) or not model_id:
        return None
    if raw.get("owned_by") != "combo":
        return None
    profile = get_route_profile(model_id)
    if profile is not None:
        efforts = tuple(profile.allowed_reasoning_efforts)
        max_effort = profile.max_reasoning_effort
        default_effort = profile.default_reasoning_effort
        requires = profile.requires_explicit_approval
        display = profile.display_name or CURATED_COMBO_DISPLAY_NAMES.get(model_id, model_id)
    else:
        # Unknown to the static catalog — render with sensible defaults so
        # a freshly-discovered combo still gets a picker row.
        efforts = ("low", "medium", "high")
        max_effort = "high"
        default_effort = "medium"
        requires = False
        display = CURATED_COMBO_DISPLAY_NAMES.get(model_id, model_id)
    return OmniRouteComboEntry(
        id=model_id,
        display_name=display,
        provider="omniroute",
        kind="combo",
        reasoning_efforts=efforts,
        max_reasoning_effort=max_effort,
        default_reasoning_effort=default_effort,
        requires_explicit_approval=requires,
    )


async def _fetch_live_catalog(
    *,
    base_url: str,
    api_key: str | None,
    transport: httpx.AsyncBaseTransport | None,
) -> list[OmniRouteComboEntry]:
    """Issue the live ``GET /v1/models`` to OmniRoute and return combo entries.

    Returns an empty list (not an exception) when the endpoint is
    unreachable — callers translate that into the cache-or-curated
    fallback.
    """
    endpoint = base_url.rstrip("/") + "/models"
    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    client_kwargs: dict[str, Any] = {"timeout": _FETCH_TIMEOUT_S}
    if transport is not None:
        client_kwargs["transport"] = transport
    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            response = await client.get(endpoint, headers=headers or None)
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        _logger.debug(
            "omniroute_catalog: live fetch from %s failed (%s); using fallback",
            base_url,
            type(exc).__name__,
        )
        return []
    entries = payload.get("data") if isinstance(payload, Mapping) else None
    if not isinstance(entries, list):
        return []
    out: list[OmniRouteComboEntry] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        combo = _live_entry_from_catalog_entry(entry)
        if combo is not None:
            out.append(combo)
    return out


async def fetch_live_omniroute_combo_catalog(
    base_url: str | None = None,
    api_key: str | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[OmniRouteComboEntry]:
    """Return only currently reachable OmniRoute combos.

    This deliberately bypasses the cache and curated fallback for entry points
    that must not advertise a route while OmniRoute is down.
    """
    return dedupe_preserve_order(
        await _fetch_live_catalog(
            base_url=_resolve_base_url(base_url),
            api_key=_resolve_api_key(api_key),
            transport=transport,
        )
    )


async def fetch_omniroute_combo_catalog(
    base_url: str | None = None,
    api_key: str | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[list[OmniRouteComboEntry], Literal["live", "cache", "fallback_curated"]]:
    """Fetch the OmniRoute combo catalog with graceful degradation.

    Resolution order:

    1. **Live fetch** from ``{base_url}/v1/models`` — returns
       ``("combos", "live")`` on success.
    2. **Cache replay** — when live fetch fails AND the cache has
       entries for the same ``(base_url, credential_fingerprint)``,
       returns the cached rows with ``source == "cache"``.
    3. **Curated fallback** — when live fetch fails AND the cache is
       empty, returns the three curated combos with
       ``source == "fallback_curated"``.

    Never raises — callers (snapshot builder + ``GET /v1/omniroute/combos``)
    can trust the tuple. ``verified`` is False for ``fallback_curated``
    (the picker shows the curated combos but the server didn't actually
    see them) and True for ``live`` + ``cache`` (the rows were once read
    from OmniRoute).

    :param base_url: Explicit OmniRoute base URL. Defaults to the env
        ``OMNIGENT_OMNIROUTE_BASE_URL`` or the bundled localhost default.
    :param api_key: Explicit API key. Defaults to ``OMNIGENT_OMNIROUTE_API_KEY``
        then ``OMNIROUTE_API_KEY`` env vars (never logged).
    :param transport: Optional httpx transport override used by tests to
        swap the live endpoint for a recorded/transport-mocked one.
    :returns: ``(combos, source)`` tuple. ``combos`` is always non-empty.
    """
    resolved_base = _resolve_base_url(base_url)
    resolved_key = _resolve_api_key(api_key)
    key = _cache_key_for(resolved_base, resolved_key)

    entries = await _fetch_live_catalog(
        base_url=resolved_base,
        api_key=resolved_key,
        transport=transport,
    )

    if entries:
        deduped = dedupe_preserve_order(entries)
        with _catalog_cache_lock:
            _catalog_cache[key] = deduped
        return deduped, "live"

    with _catalog_cache_lock:
        cached = _catalog_cache.get(key)
    if cached:
        return list(cached), "cache"

    return curated_combo_catalog(), "fallback_curated"


def _cache_snapshot() -> dict[_CatalogCacheKey, list[OmniRouteComboEntry]]:
    """Return a copy of the cache for test assertions (avoids lock contention)."""
    with _catalog_cache_lock:
        return {k: list(v) for k, v in _catalog_cache.items()}


def _clear_cache_for_tests() -> None:
    """Empty the cache (test-only). Imports are guarded so prod callers can't."""
    with _catalog_cache_lock:
        _catalog_cache.clear()


def omniroute_combo_display_name(combo_id: str) -> str:
    """Return the curated display name for a combo id, falling back to the id verbatim.

    Public helper used by the session snapshot and ``GET /v1/omniroute/combos``
    handlers — never logs, never raises. Preserves the raw id (colons,
    slashes, brackets) so an unknown combo still dispatches correctly.
    """
    if combo_id in CURATED_COMBO_DISPLAY_NAMES:
        return CURATED_COMBO_DISPLAY_NAMES[combo_id]
    profile = get_route_profile(combo_id)
    if profile is not None and profile.display_name:
        return profile.display_name
    return combo_id


def reset_omniroute_catalog_cache() -> None:
    """Clear the module catalog cache; exposed for use by tests + tooling."""
    _clear_cache_for_tests()


__all__ = [
    "CURATED_COMBO_DISPLAY_NAMES",
    "CURATED_COMBO_IDS",
    "OmniRouteComboEntry",
    "curated_combo_catalog",
    "dedupe_preserve_order",
    "fetch_live_omniroute_combo_catalog",
    "fetch_omniroute_combo_catalog",
    "omniroute_combo_display_name",
    "reset_omniroute_catalog_cache",
]
