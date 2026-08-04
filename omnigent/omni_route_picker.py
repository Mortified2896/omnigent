"""OmniRoute-backed pre-launch model selector for OpenAI-compatible gateways.

Drives the ``host.model_options`` picker when the active provider is an
OpenAI-compatible gateway (OmniRoute, OpenRouter, LiteLLM, ...). Reads the
authoritative model catalog from the provider's ``GET <base_url>/v1/models``
and renders the rows the Web UI consumes (``{id, model, displayName,
isDefault, isCurrent}``) — the same shape the claude-native picker uses.

Design constraints (per the ``omnigent/AGENTS.md`` "narrow scope" contract —
this module is small on purpose and does one thing):

* Single source of truth: the picker only enumerates models the configured
  provider reported live; it does NOT maintain a second hardcoded list.
* Default preservation: the family's ``models["default"]`` AND every other
  pinned alias in ``models:`` (e.g. ``gpt56_sol: codex/gpt-5.6-sol``) must
  appear in the picker even when the live catalog does not yet advertise
  them — providers pin aliases for models the aggregator has not published,
  and silently dropping them would erase the operator's intentional default.
* Per-harness compatibility filter: Claude-native keeps its own picker, but
  every other harness that consumes the openai/anthropic surface routes
  through here with its family rule applied
  (:func:`omnigent.model_override.model_family_mismatch`).
* Deduplication by model id — the OpenAI ``/v1/models`` response duplicates
  the same model under multiple prefixes (e.g. ``codex/gpt-5.4`` and
  ``cx/gpt-5.4``); the picker collapses duplicates and keeps one display row
  per resolved id.
* Filter evaluator/rerank/image/audio/embedding-only routes out before they
  reach the picker. The wire protocol differs and these ids cannot drive a
  coding session, so a normal coding-model listing would mislead users into
  selecting them.
* Clear failures: the picker fails loud (raises an exception the
  ``host.model_options`` handler maps to a 502) instead of silently falling
  back to another provider, returning an empty list, or substituting a
  curated catalog. Operators want a misconfigured provider to be visible —
  not silently working around the bug.

The module deliberately exposes a small surface (:func:`omni_route_model_options`
plus the dataclass :class:`OmniRouteModelOptionsError`). The wired call site
lives in :func:`omnigent.host.connect.HostProcess._handle_model_options`.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from omnigent.model_override import model_family_mismatch
from omnigent.onboarding.provider_config import (
    OPENAI_FAMILY,
    ProviderEntry,
    default_provider_for_harness,
    load_config,
)

_logger = logging.getLogger(__name__)


# Catalog fetch timeout. The host picker is a pre-launch preview, not a
# critical hot path — short enough that a slow gateway fails loud before
# the route-layer 15s timeout trips.
_OMNIROUTE_FETCH_TIMEOUT_S = 8.0

# Non-chat-capable entry kinds to exclude from the picker. Each entry is the
# value of the OpenAI-compatible ``"type"`` field the catalog reports; chat
# models either omit ``type`` or report ``"chat"``. Embeddings, image, audio,
# video, music, and rerank models have distinct wire protocols and cannot
# drive a coding session via the chat wire Pi uses.
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
    :param display_name: Optional human-readable name from the catalog
        (the Omni ``"name"`` field). Falls back to the id when absent.
    """

    id: str
    display_name: str | None = None


def _resolve_omni_route_provider(
    harness: str,
    config: dict[str, Any],
) -> ProviderEntry:
    """Pick the provider entry the picker reads from.

    Resolution rules — same precedence the spawn paths use, narrowed to
    a single family a gateway kind can serve (``openai``):

    1. Walk ``default_provider_for_harness`` for ``harness``. When the
       harness declares no default, fall back to the ``pi`` default (the
       picker is pre-launch and the Pi picker is the primary beneficiary
       per the design brief).
    2. Require a gateway/local/key entry with an ``openai`` family — the
       gateway's ``/v1/models`` is openai-compatible. A subscription /
       databricks / cli-config / bedrock entry has no chat-wire base URL,
       so it cannot drive the picker; surface the mismatch as a clear
       error rather than silently degrading.

    :param harness: Canonical or alias harness id, e.g. ``"pi-native"``.
    :param config: Loaded ``~/.omnigent/config.yaml``.
    :returns: The resolved provider entry.
    :raises OmniRouteModelOptionsError: When no OmniRoute-style gateway is
        configured for this harness. Carries the harness name and the
        configured-default name (when found) so the route layer can show
        the operator what is missing.
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
    family = entry.families.get(OPENAI_FAMILY)
    if family is None:
        raise OmniRouteModelOptionsError(
            f"provider {entry.name!r} (kind {entry.kind!r}) has no 'openai' "
            "family; configure one to enable the OmniRoute picker."
        )
    return entry


def _models_url(base_url: str) -> str:
    """Derive the catalog URL from a provider's OpenAI-style base URL.

    Mirrors :func:`omnigent.model_catalog._models_url` so the picker and the
    runtime catalog hit the same path on the same base URL.

    :param base_url: Endpoint base URL, e.g. ``"http://127.0.0.1:20128/v1"``.
    :returns: The listing URL — ``<base>/models`` when the base already
        ends in ``/v1``, else ``<base>/v1/models``.
    """
    trimmed = base_url.rstrip("/")
    if trimmed.endswith("/v1"):
        return f"{trimmed}/models"
    return f"{trimmed}/v1/models"


def _bearer_for_entry(entry: ProviderEntry) -> str:
    """Resolve the configured provider's bearer token.

    Mirrors :func:`omnigent.model_catalog._resolve_bearer_token` without
    importing it directly (the runtime catalog path would force-include
    the heavy httpx / runtime-credentials stack on a host picker cold
    path; the picker needs to stay self-contained).

    :param entry: The provider entry whose credential we resolve.
    :returns: The bearer token (env / inline / auth-command stdout).
    :raises OmniRouteModelOptionsError: With the operator-actionable cause
        when the credential cannot be resolved.
    """
    from omnigent.onboarding.provider_config import KEY_KIND, resolve_secret

    family = entry.families.get(OPENAI_FAMILY)
    if family is None:
        raise OmniRouteModelOptionsError(
            f"provider {entry.name!r} has no 'openai' family to read a credential from"
        )
    if family.api_key:
        return family.api_key
    if family.api_key_ref:
        try:
            return resolve_secret(family.api_key_ref)
        except Exception as exc:
            raise OmniRouteModelOptionsError(
                f"failed to resolve credential for provider {entry.name!r} "
                f"({family.api_key_ref!r}): {exc}"
            ) from exc
    if entry.kind == KEY_KIND and family.auth_command:
        import shlex
        import subprocess

        try:
            result = subprocess.run(
                shlex.split(family.auth_command),
                capture_output=True,
                text=True,
                timeout=8.0,
                check=True,
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
    LLM-endpoint entries. Non-LLM entries (embedding/image/audio/video/music/
    rerank) are dropped at this boundary so downstream stages never see
    them — the picker exists for chat models, not catalog exploration.

    :param entry: The provider entry whose base URL we hit.
    :param transport: Optional httpx transport override for tests; ``None``
        uses the default transport.
    :returns: The catalog as a list of :class:`OmniRouteCatalogEntry`,
        preserving the order reported by the catalog endpoint.
    :raises OmniRouteModelOptionsError: When the family has no base URL,
        the bearer cannot be resolved, the HTTP call fails, or the response
        shape is unexpected.
    """
    family = entry.families.get(OPENAI_FAMILY)
    if family is None or not family.base_url:
        raise OmniRouteModelOptionsError(
            f"provider {entry.name!r} has no 'openai' base_url to fetch models from"
        )
    token = _bearer_for_entry(entry)
    url = _models_url(family.base_url)
    try:
        with httpx.Client(transport=transport, timeout=_OMNIROUTE_FETCH_TIMEOUT_S) as client:
            resp = client.get(
                url,
                headers={"Authorization": f"Bearer {token}"},
            )
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
    except json.JSONDecodeError as exc:
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
            # Incompatible wire protocol; skip before dedupe/filter rather
            # than offering it and watching dispatch reject it.
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
    """Return the configured family ``models:`` aliases preserved verbatim.

    Operators pin aliases through ``providers.<name>.openai.models`` — each
    alias is either a default model id (``default`` key) or a named slot
    (``gpt56_sol: codex/gpt-5.6-sol``). These aliases must appear in the
    picker even when the live OmniRoute catalog has not yet published the
    underlying ids, so this helper walks the configured map and yields the
    model ids to merge back into the picker rows.

    :param entry: The provider entry whose family model-pinning we read.
    :returns: A list of model ids, deduplicated while preserving the
        configured ``models:`` insertion order.
    """
    family = entry.families.get(OPENAI_FAMILY)
    if family is None or not family.models:
        return []
    seen: set[str] = set()
    ordered: list[str] = []
    for model_id in family.models.values():
        if not isinstance(model_id, str) or not model_id:
            continue
        if model_id in seen:
            continue
        seen.add(model_id)
        ordered.append(model_id)
    return ordered


def dedupe_catalog(entries: list[OmniRouteCatalogEntry]) -> list[OmniRouteCatalogEntry]:
    """Collapse duplicate ids, preserving first-occurrence order.

    OpenRouter-style catalogs publish the same model under every prefixed
    alias (e.g. ``codex/gpt-5.4`` and ``cx/gpt-5.4`` for the same model);
    the picker renders one row per model, so pick the first occurrence and
    drop the rest. Preserves the catalog's native ordering — the operator
    tailors their catalog so its order ranks models (the first GPT-5.4
    variant is the most desirable), and reordering rows here would lose
    that signal.

    :param entries: The raw catalog list.
    :returns: A new list containing exactly one entry per id, in
        first-occurrence order.
    """
    seen: set[str] = set()
    out: list[OmniRouteCatalogEntry] = []
    for entry in entries:
        if entry.id in seen:
            continue
        seen.add(entry.id)
        out.append(entry)
    return out


def filter_catalog_for_harness(
    entries: list[OmniRouteCatalogEntry],
    harness: str,
) -> list[OmniRouteCatalogEntry]:
    """Keep only entries the harness can drive.

    Delegates the family rule to
    :func:`omnigent.model_override.model_family_mismatch` so the picker
    cannot drift from the dispatch gate — a model the runtime would
    refuse never appears in the picker. Pi / openai-agents (multi-model)
    pass every id through.

    :param entries: The deduped catalog.
    :param harness: Canonical harness spelling, e.g. ``"codex"``.
    :returns: The harness-compatible subset, preserving order.
    """
    return [entry for entry in entries if model_family_mismatch(harness, entry.id) is None]


def merge_pinned_models(
    entries: list[OmniRouteCatalogEntry],
    pinned_ids: list[str],
    *,
    harness: str | None = None,
) -> list[OmniRouteCatalogEntry]:
    """Append harness-compatible pinned ids not already in *entries*.

    The configured default is guaranteed a slot — even when the live catalog
    has not yet published it (e.g. a model the aggregator added since the
    catalog was last refreshed). Pinned ids already in the catalog are
    left alone (so we keep the catalog's natural row order and display
    name). When *harness* is provided, pinned ids the harness can never
    drive are dropped — the configured default cannot bypass the family
    filter, because exposing an id the runtime would reject would be the
    silent failure this picker is designed to avoid.

    :param entries: The harness-filtered catalog rows.
    :param pinned_ids: Configured ``models:`` values, in insertion order.
    :param harness: Optional harness spelling; when provided, every pinned
        id is run through the same family filter as the live catalog.
    :returns: The merged list — catalog rows first, then any pinned ids
        not yet present and harness-compatible, in pinned insertion order.
    """
    existing = {entry.id for entry in entries}
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
    """Render catalog entries as the Web UI's picker row format.

    Each row carries ``id`` (the model id), ``model`` (the same id, so
    callers can read either key — matches the claude-native picker shape),
    ``displayName``, and ``isDefault``. The id is identical to ``model`` so
    the picker's downstream launch serialization sends a single, unambiguous
    argument whether the harness reads ``id`` or ``model``.

    :param entries: The filtered, deduped, default-merged catalog.
    :param default_model: The configured default id, marked as
        ``isDefault: True``. ``None`` leaves every row's ``isDefault``
        False (the harness will fall back to its own default).
    :returns: A list of picker row dicts ready for the host's
        :class:`HostModelOptionsResultFrame`.
    """
    rows: list[dict[str, Any]] = []
    for entry in entries:
        is_default = default_model is not None and entry.id == default_model
        rows.append(
            {
                "id": entry.id,
                "model": entry.id,
                "displayName": entry.display_name or entry.id,
                "isDefault": is_default,
                "isCurrent": False,
            }
        )
    return rows


def omni_route_model_options(
    harness: str,
    *,
    config_loader: Callable[[], dict[str, Any]] = load_config,
    transport: httpx.BaseTransport | None = None,
) -> list[dict[str, Any]]:
    """Resolve the pre-launch OmniRoute picker rows for *harness*.

    Top-level orchestrator: reads the provider config, fetches the live
    catalog (failing loud on any misconfiguration or upstream error),
    dedupes / filters / merges the configured pinned ids, and returns
    picker rows. The host's ``host.model_options`` handler converts any
    raised :class:`OmniRouteModelOptionsError` into a 502 so the UI shows
    the operator exactly what is wrong rather than a silent empty list.

    :param harness: Harness spelling (any alias accepted by
        :func:`omnigent.harness_aliases.canonicalize_harness`).
    :param config_loader: Injection seam for tests; defaults to
        :func:`load_config`.
    :param transport: Optional httpx transport override for tests; ``None``
        uses the default transport.
    :returns: Picker rows — never empty (a fresh provider always has the
        configured default as a fallback row).
    :raises OmniRouteModelOptionsError: When the picker cannot resolve. The
        caller surfaces the message verbatim.
    """
    from omnigent.harness_aliases import canonicalize_harness

    canonical = canonicalize_harness(harness) or harness
    try:
        config = config_loader()
    except Exception as exc:
        raise OmniRouteModelOptionsError(
            f"failed to load omnigent provider config: {exc}"
        ) from exc
    entry = _resolve_omni_route_provider(canonical, config)
    catalog = fetch_omni_route_catalog(entry, transport=transport)
    deduped = dedupe_catalog(catalog)
    filtered = filter_catalog_for_harness(deduped, canonical)
    if not filtered:
        # The picker cannot survive on an empty filtered set: a filter that
        # excludes the configured default means the provider is
        # misconfigured for this harness. Raise instead of returning an
        # empty list — the operator should see the cause.
        raise OmniRouteModelOptionsError(
            f"no OmniRoute models survive the {canonical!r} harness filter "
            f"(provider {entry.name!r}); check providers.<name>.openai.base_url "
            "and the catalog contents."
        )
    pinned = _pinned_models(entry)
    family = entry.families.get(OPENAI_FAMILY)
    default_id = family.default_model if family is not None else None
    # ``merge_pinned_models`` runs pinned ids through the same family
    # filter as the catalog so a configured default that the harness
    # cannot drive cannot bypass the runtime dispatch gate.
    merged = merge_pinned_models(filtered, pinned, harness=canonical)
    return build_picker_options(merged, default_model=default_id)
