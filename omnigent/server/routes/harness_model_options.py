"""Generic harness model-options endpoint.

Exposes ``GET /v1/harness-model-options?harness=<canonical-harness>``
so the web UI can populate the AgentPicker with model options for any
harness that provides a registered model source.

Provider registry
-----------------
The provider registry is built dynamically from
:data:`omnigent.inner._opencode_native_lane_config.OPENCODE_NATIVE_LANES`
— the shared single source of truth for every OpenCode-backed lane.
Adding a new lane is a one-config-entry change in that table; the
resolver and the executor both consume it without further edits.

Each resolver returns a list of normalized model dicts with ``id``,
``label``, ``provider``, ``tier``, ``kind``, ``manual_fallback_only``,
``requires_credentials``, ``billing_risk``, ``context_limit``, and
``output_limit``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Query, Request

from omnigent.inner._opencode_native_lane_config import (
    OPENCODE_NATIVE_LANES,
    OpenCodeNativeLaneConfig,
    all_resolver_ids,
    empty_state_disclaimer,
)
from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user

logger = logging.getLogger(__name__)


# ── Generic lane resolver ──────────────────────────────────────────


def _resolve_lane_models(lane: OpenCodeNativeLaneConfig) -> dict[str, Any]:
    """Resolve models for *lane* from its local catalog.

    Reads the catalog at ``lane.catalog_path`` (which the sync script
    owns — see ``scripts/sync-opencode-<lane>-models.py`` in the
    HomeLab repo) and translates each entry into a normalized
    ``HarnessModelOption``. Filters at three layers:

    1. **Sync layer** (offline) — the sync script's provider-prefix
       allowlist keeps only verified ids. Catalog carries no
       API-billed ids to begin with.
    2. **Verify layer** (pre-run) — the verify script refuses
       API-billed / non-lane ids with a clear non-zero exit.
    3. **Resolver layer** (this function) — re-checks each entry
       against ``lane.allowed_provider_prefixes`` (SUBSCRIPTION
       lanes) or ``m.get("free") is True`` (FREE lanes) so a buggy
       future catalog run cannot leak cross-lane ids into the
       picker.

    :param lane: The lane config from the shared table.
    :returns: ``{"models": [...], "source": ..., "last_synced_at":
        ..., "error": ...}``. The ``error`` key is present when the
        catalog is missing or unreadable, or when the entry's
        provider prefix is not in the allowlist.
    """
    if not lane.catalog_path.is_file():
        return {
            "models": [],
            "source": lane.source_label or f"{lane.resolver_id}-catalog",
            "last_synced_at": None,
            "error": lane.empty_state_message + empty_state_disclaimer(lane),
        }
    try:
        raw = json.loads(lane.catalog_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(
            "Failed to read catalog for lane %s: %s",
            lane.resolver_id,
            exc,
        )
        return {
            "models": [],
            "source": lane.source_label or f"{lane.resolver_id}-catalog",
            "last_synced_at": None,
            "error": f"Catalog unreadable: {exc}",
        }

    if lane.lane_variant.value == "free":
        models = _filter_and_normalize_free_lane(raw.get("models", []), lane)
    else:
        models = _filter_and_normalize_subscription_lane(
            raw.get("models", []), lane
        )

    return {
        "models": models,
        "source": lane.source_label or f"{lane.resolver_id}-catalog",
        "last_synced_at": raw.get("last_synced_at"),
    }


def _filter_and_normalize_free_lane(
    raw_models: list[dict[str, Any]],
    lane: OpenCodeNativeLaneConfig,
) -> list[dict[str, Any]]:
    """Filter and normalize entries for a FREE lane (e.g. OpenCode Free).

    FREE lanes filter by ``m.get("free") is True`` rather than by
    provider prefix (the OpenCode Free catalog mixes several
    providers and the "free" flag is the cross-provider signal).
    The id the picker returns is always the fully-qualified
    ``opencode/<id>`` form — the runner consumes it verbatim.
    """
    models: list[dict[str, Any]] = []
    for m in raw_models:
        if m.get("free") is not True:
            continue
        bare_id = m.get("id", "")
        if not bare_id:
            continue
        full_id = f"opencode/{bare_id}" if not bare_id.startswith("opencode/") else bare_id
        models.append(
            {
                "id": full_id,
                "label": m.get("name") or bare_id.replace("-", " ").title(),
                "provider": lane.display_provider,
                "tier": lane.tier,
                "kind": lane.kind,
                "manual_fallback_only": bool(
                    m.get("explicit_manual_fallback_only", False)
                ),
                "requires_credentials": False,
                "billing_risk": lane.billing_risk,
                "context_limit": m.get("context_limit"),
                "output_limit": m.get("output_limit"),
                "variants": m.get("variants", []),
            }
        )
    return models


def _filter_and_normalize_subscription_lane(
    raw_models: list[dict[str, Any]],
    lane: OpenCodeNativeLaneConfig,
) -> list[dict[str, Any]]:
    """Filter and normalize entries for a SUBSCRIPTION lane.

    SUBSCRIPTION lanes filter by provider-prefix allowlist. The
    resolver layer re-checks every entry's provider prefix against
    ``lane.allowed_provider_prefixes`` even though the sync script
    already filtered — defense-in-depth against a buggy future
    catalog run that shipped an API-metered id.

    Returns ``[]`` when the allowlist is empty (fail-closed state
    for lanes like Codex Subscription that don't have a verified
    prefix yet).
    """
    if not lane.allowed_provider_prefixes:
        # Fail closed — no verified provider prefix admits any
        # model. The catalog is still reported as the source so
        # the picker can show "configured but not verified" if
        # useful.
        return []

    models: list[dict[str, Any]] = []
    for entry in raw_models:
        full_id: str = entry.get("id") or ""
        # ``full_id`` is the OpenCode-qualified form:
        # ``opencode/<provider>/<model>``. Strip the leading
        # ``opencode/`` to get the opencode-CLI form used by the
        # provider prefix check.
        bare_id = (
            full_id[len("opencode/"):] if full_id.startswith("opencode/") else full_id
        )
        provider_id = bare_id.split("/", 1)[0] if "/" in bare_id else ""
        if provider_id not in lane.allowed_provider_prefixes:
            logger.warning(
                "Lane %s catalog returned non-allowed id %r; omitting from picker.",
                lane.resolver_id,
                full_id,
            )
            continue
        region_label = lane.region_labels.get(provider_id, "")
        model_name = entry.get("model_name") or bare_id.split("/", 1)[-1]
        raw_line = entry.get("raw_line") or bare_id
        label = entry.get("name") or model_name
        if lane.label_suffix:
            # Region-aware suffix (e.g. "Token Plan / Subscription (China)").
            if region_label:
                label = f"{label} — {lane.label_suffix} ({region_label})"
            else:
                label = f"{label} — {lane.label_suffix}"
        models.append(
            {
                "id": full_id,
                "label": label,
                "provider": lane.display_provider,
                "tier": lane.tier,
                "kind": lane.kind,
                "manual_fallback_only": True,
                "requires_credentials": True,
                # Boolean ONLY — the catalog carries no secret value
                # and neither does this endpoint.
                "credentials_present": bool(entry.get("credentials_present")),
                "credential_env_var": lane.credential_env_var,
                "billing_risk": lane.billing_risk,
                "context_limit": entry.get("context_limit"),
                "output_limit": entry.get("output_limit"),
                "provider_id": provider_id,
                "region": entry.get("region", ""),
                "release_date": entry.get("release_date", ""),
                "raw_line": raw_line,
            }
        )
    return models


# ── Provider registry ────────────────────────────────────────────────
#
# Map canonical harness id → resolver function. Built dynamically
# from the shared ``OPENCODE_NATIVE_LANES`` table so adding a future
# lane is a one-config-entry change.

_HARNESS_MODEL_PROVIDERS: dict[str, Callable[[], dict[str, Any]]] = {
    lane.resolver_id: (lambda lane=lane: _resolve_lane_models(lane))
    for lane in OPENCODE_NATIVE_LANES
}


def create_harness_model_options_router(
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    """Build the harness model-options router.

    :param auth_provider: Auth provider, or ``None`` for single-user mode.
    :returns: A configured :class:`APIRouter`.
    """
    router = APIRouter()

    @router.get("/harness-model-options")
    async def get_harness_model_options(
        request: Request,
        harness: str = Query(..., description="Canonical harness id, e.g. ``opencode-native``"),
    ) -> dict[str, Any]:
        """Return model options for the given harness.

        Looks up *harness* in the provider registry and returns the
        resolved model list. Unsupported harnesses return a clear empty
        response rather than failing silently.

        :param harness: Canonical harness id.
        :returns: ``{"harness": ..., "source": ..., "models": [...]}``.
        """
        require_user(request, auth_provider)

        resolver = _HARNESS_MODEL_PROVIDERS.get(harness)
        if resolver is None:
            return {
                "harness": harness,
                "source": None,
                "models": [],
                "last_synced_at": None,
                "note": "No model provider registered for this harness.",
            }

        result = resolver()
        return {
            "harness": harness,
            "source": result.get("source"),
            "models": result.get("models", []),
            "last_synced_at": result.get("last_synced_at"),
            "error": result.get("error"),
        }

    return router
