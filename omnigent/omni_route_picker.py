"""OmniRoute-backed pre-launch model selector for OpenAI-compatible gateways.

Drives the ``host.model_options`` picker for harnesses whose default provider
is an OpenAI-compatible gateway (OmniRoute / OpenRouter / LiteLLM / a local
proxy). Reads the authoritative catalog from
``GET <base_url>/v1/models`` and renders the picker rows the Web UI consumes.

Scope contract — narrow on purpose, one thing only:

* Single source of truth: the picker enumerates models the configured
  provider reports live; it does not maintain a hardcoded second list.
* Default preservation: the configured ``models:`` ``default`` and every
  other pinned alias appear in the picker even when the live catalog has
  not yet advertised them.
* Per-harness compatibility filter: routed through
  :func:`omnigent.model_override.model_family_mismatch` so the picker
  cannot drift from the dispatch gate.
* Deduplication by model id (OpenRouter-style catalogs publish a model
  under every prefixed alias).
* Excludes embedding / image / audio / video / music / rerank entries
  before they reach the picker — the wire protocol differs and these ids
  cannot drive a coding session.
* Clear failures: the picker raises :class:`OmniRouteModelOptionsError`
  instead of silently substituting another provider, an empty list, or a
  curated catalog.

The wired call site lives in
:func:`omnigent.host.connect.HostProcess._handle_model_options`.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from omnigent.model_catalog import _models_url
from omnigent.model_override import model_family_mismatch
from omnigent.onboarding.provider_config import (
    KEY_KIND,
    OPENAI_FAMILY,
    ProviderEntry,
    default_provider_for_harness,
    load_config,
)

_logger = logging.getLogger(__name__)


_FETCH_TIMEOUT_S = 8.0

# Non-chat-capable entry kinds to exclude from the picker. Chat models
# either omit ``type`` or report ``"chat"``; the other kinds have distinct
# wire protocols and cannot drive a coding session via the chat wire Pi /
# Codex / openai-agents / qwen use.
_NON_CHAT_MODEL_TYPES: frozenset[str] = frozenset(
    {
        "embedding",
        "image",
        "audio",
        "video",
        "music",
        "rerank",
    }
)

_DEFAULT_SHELL_TIMEOUT_S = 8.0


class OmniRouteModelOptionsError(Exception):
    """Raised when the OmniRoute picker cannot resolve its catalog.

    The :func:`omnigent.host.connect.HostProcess._handle_model_options`
    handler maps this to a 502 with the message intact — never silently
    fallback to another provider or to an empty list.
    """


@dataclass(frozen=True)
class OmniRouteCatalogEntry:
    """One model from the live OmniRoute catalog, ready for the picker.

    :param id: Provider-local model id — exactly what the gateway's
        ``/v1/models`` returned (e.g. ``"codex/gpt-5.4-mini"`` or
        ``"custom/best-coding"``). Used as the picker's ``id`` and the
        session's ``--model`` argument.
    :param display_name: Optional human-readable name from the catalog's
        ``"name"`` field. Falls back to the id when absent.
    """

    id: str
    display_name: str | None = None


def _resolve_provider(
    harness: str,
    config: dict[str, Any],
) -> ProviderEntry:
    """Pick the provider entry the picker reads from.

    Resolution rules — same precedence the spawn paths use, narrowed to
    inline-credential providers a chat gateway can serve:

    1. ``default_provider_for_harness(config, harness)``. When the harness
       has no default, fall back to ``pi`` (the picker's primary
       beneficiary per the design brief).
    2. Require a ``gateway`` / ``local`` / ``key`` entry with an
       ``openai`` family. A subscription / databricks / cli-config entry
       has no chat-wire base URL, so it cannot drive the picker; surface
       the mismatch as a clear error rather than silently degrading.
    """
    entry = default_provider_for_harness(config, harness)
    if entry is None:
        entry = default_provider_for_harness(config, "pi")
    if entry is None:
        raise OmniRouteModelOptionsError(
            f"no default provider configured for harness {harness!r}; "
            "add a gateway-kind provider with an 'openai' family to "
            "enable the pre-launch OmniRoute picker."
        )
    if entry.kind not in ("gateway", "local", "key"):
        raise OmniRouteModelOptionsError(
            f"harness {harness!r} resolves to provider {entry.name!r} "
            f"(kind {entry.kind!r}); the OmniRoute picker requires a "
            "gateway / local / key-kind provider with an 'openai' family."
        )
    if entry.families.get(OPENAI_FAMILY) is None:
        raise OmniRouteModelOptionsError(
            f"provider {entry.name!r} (kind {entry.kind!r}) has no 'openai' "
            "family; configure one to enable the OmniRoute picker."
        )
    return entry


def _resolve_bearer(entry: ProviderEntry) -> str:
    """Resolve the configured provider's bearer token.

    Goes through :meth:`ProviderEntry.family` so ``base_url`` /
    ``api_key`` / ``api_key_ref`` are lazily expanded (env vars and
    ``env:`` / ``keychain:`` refs resolved). Falls back to the entry's
    ``auth_command`` (mirrors
    :func:`omnigent.model_catalog._resolve_bearer_token`).
    """
    family = entry.family(OPENAI_FAMILY)
    if family is None:
        raise OmniRouteModelOptionsError(
            f"provider {entry.name!r} has no 'openai' family to read a credential from"
        )
    if family.api_key:
        return family.api_key
    if entry.kind == KEY_KIND and family.auth_command:
        try:
            result = subprocess.run(
                family.auth_command,
                capture_output=True,
                text=True,
                timeout=_DEFAULT_SHELL_TIMEOUT_S,
                check=True,
                shell=True,
            )
        except Exception as exc:
            raise OmniRouteModelOptionsError(
                f"failed to run auth_command for provider {entry.name!r}: {exc}"
            ) from exc
        token = result.stdout.strip()
        if not token:
            raise OmniRouteModelOptionsError(
                f"auth_command for provider {entry.name!r} printed an empty token"
            )
        return token
    raise OmniRouteModelOptionsError(
        f"provider {entry.name!r} has no credential configured (api_key / "
        "api_key_ref / auth_command)"
    )


def fetch_omni_route_catalog(
    entry: ProviderEntry,
    *,
    transport: httpx.BaseTransport | None = None,
) -> list[OmniRouteCatalogEntry]:
    """Read the live model catalog from the provider's OpenAI-compatible gateway.

    Hits ``GET <base_url>/v1/models`` with a bearer token and returns the
    chat-capable entries. Non-LLM entries (embedding/image/audio/video/
    music/rerank) are dropped at this boundary so downstream stages never
    see them.

    :raises OmniRouteModelOptionsError: When the family has no base URL,
        the bearer cannot be resolved, the HTTP call fails, the response
        shape is unexpected, or every returned entry is non-chat.
    """
    family = entry.families.get(OPENAI_FAMILY)
    if family is None or not family.base_url:
        raise OmniRouteModelOptionsError(
            f"provider {entry.name!r} has no 'openai' base_url to fetch models from"
        )
    token = _resolve_bearer(entry)
    url = _models_url(family.base_url)
    try:
        with httpx.Client(transport=transport, timeout=_FETCH_TIMEOUT_S) as client:
            resp = client.get(url, headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as exc:
        raise OmniRouteModelOptionsError(
            f"OmniRoute catalog request to {url} failed: {exc}"
        ) from exc
    if resp.status_code >= 400:
        raise OmniRouteModelOptionsError(
            f"OmniRoute catalog request to {url} returned HTTP "
            f"{resp.status_code}: {resp.text[:200]!r}"
        )
    try:
        payload = resp.json()
    except ValueError as exc:
        raise OmniRouteModelOptionsError(
            f"OmniRoute catalog response from {url} was not valid JSON"
        ) from exc
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise OmniRouteModelOptionsError(
            f"OmniRoute catalog response from {url} has no 'data' list"
        )
    entries: list[OmniRouteCatalogEntry] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        model_id = item.get("id")
        if not isinstance(model_id, str) or not model_id:
            continue
        if item.get("type") in _NON_CHAT_MODEL_TYPES:
            continue
        display_name = item.get("name")
        entries.append(
            OmniRouteCatalogEntry(
                id=model_id,
                display_name=display_name if isinstance(display_name, str) else None,
            )
        )
    if not entries:
        raise OmniRouteModelOptionsError(
            f"OmniRoute catalog at {url} returned no chat-capable models; "
            "the picker refuses to fall back to a curated catalog."
        )
    return entries


def _pinned_models(entry: ProviderEntry) -> list[str]:
    """Return the configured ``models:`` values, in insertion order, deduped."""
    family = entry.families.get(OPENAI_FAMILY)
    if family is None or not family.models:
        return []
    seen: set[str] = set()
    ordered: list[str] = []
    for model_id in family.models.values():
        if not isinstance(model_id, str) or not model_id or model_id in seen:
            continue
        seen.add(model_id)
        ordered.append(model_id)
    return ordered


def dedupe_catalog(
    entries: list[OmniRouteCatalogEntry],
) -> list[OmniRouteCatalogEntry]:
    """Collapse duplicate ids, preserving first-occurrence order."""
    seen: set[str] = set()
    return [e for e in entries if not (e.id in seen or seen.add(e.id))]


def filter_catalog_for_harness(
    entries: list[OmniRouteCatalogEntry],
    harness: str,
) -> list[OmniRouteCatalogEntry]:
    """Keep only entries the harness can drive (multi-model harnesses keep all)."""
    return [e for e in entries if model_family_mismatch(harness, e.id) is None]


def merge_pinned_models(
    entries: list[OmniRouteCatalogEntry],
    pinned_ids: list[str],
    *,
    harness: str | None = None,
) -> list[OmniRouteCatalogEntry]:
    """Append harness-compatible pinned ids not already in *entries*."""
    existing = {e.id for e in entries}
    out: list[OmniRouteCatalogEntry] = list(entries)
    for model_id in pinned_ids:
        if model_id in existing:
            continue
        if harness is not None and model_family_mismatch(harness, model_id) is not None:
            continue
        out.append(OmniRouteCatalogEntry(id=model_id))
        existing.add(model_id)
    return out


def build_picker_options(
    entries: list[OmniRouteCatalogEntry],
    *,
    default_model: str | None,
) -> list[dict[str, Any]]:
    """Render catalog entries as the Web UI's picker row format."""
    return [
        {
            "id": e.id,
            "model": e.id,
            "displayName": e.display_name or e.id,
            "isDefault": default_model is not None and e.id == default_model,
            "isCurrent": False,
        }
        for e in entries
    ]


def omni_route_model_options(
    harness: str,
    *,
    config_loader: Callable[[], dict[str, Any]] = load_config,
    transport: httpx.BaseTransport | None = None,
) -> list[dict[str, Any]]:
    """Resolve the pre-launch OmniRoute picker rows for *harness*.

    Top-level orchestrator: reads provider config, fetches the live catalog
    (failing loud on any misconfiguration / upstream error), dedupes /
    filters / merges the configured pinned ids, and returns picker rows.
    The host's ``host.model_options`` handler converts any raised
    :class:`OmniRouteModelOptionsError` into a 502 so the UI shows the
    operator exactly what is wrong rather than a silent empty list.
    """
    from omnigent.harness_aliases import canonicalize_harness

    canonical = canonicalize_harness(harness) or harness
    try:
        config = config_loader()
    except Exception as exc:
        raise OmniRouteModelOptionsError(
            f"failed to load omnigent provider config: {exc}"
        ) from exc
    entry = _resolve_provider(canonical, config)
    catalog = fetch_omni_route_catalog(entry, transport=transport)
    deduped = dedupe_catalog(catalog)
    filtered = filter_catalog_for_harness(deduped, canonical)
    if not filtered:
        raise OmniRouteModelOptionsError(
            f"no OmniRoute models survive the {canonical!r} harness filter "
            f"(provider {entry.name!r}); check providers.<name>.openai.base_url "
            "and the catalog contents."
        )
    merged = merge_pinned_models(filtered, _pinned_models(entry), harness=canonical)
    family = entry.families.get(OPENAI_FAMILY)
    default_id = family.default_model if family is not None else None
    return build_picker_options(merged, default_model=default_id)
