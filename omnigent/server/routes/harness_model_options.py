"""Generic harness model-options endpoint.

Exposes ``GET /v1/harness-model-options?harness=<canonical-harness>``
so the web UI can populate the AgentPicker with model options for any
harness that provides a registered model source.

Provider registry
-----------------
Add new model sources by registering a resolver in
``_HARNESS_MODEL_PROVIDERS``. Each resolver returns a list of normalized
model dicts with ``id``, ``label``, ``provider``, ``tier``, ``kind``,
``manual_fallback_only``, ``requires_credentials``, ``billing_risk``,
``context_limit``, and ``output_limit``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Query, Request

from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user

logger = logging.getLogger(__name__)

# ── OpenCode free-catalog provider ───────────────────────────────────

_OPENCODE_CATALOG_PATH = Path.home() / ".cache" / "homelab" / "opencode-free-models.json"


def _resolve_opencode_free_models() -> dict[str, Any]:
    """Resolve OpenCode free models from the local catalog.

    :returns: ``{"models": [...], "source": "opencode-free-catalog",
        "last_synced_at": "..."}`` or an error-shaped dict.
    """
    if not _OPENCODE_CATALOG_PATH.exists():
        return {
            "models": [],
            "source": "opencode-free-catalog",
            "last_synced_at": None,
            "error": "Catalog not found. Run opencode models to refresh.",
        }

    try:
        raw = json.loads(_OPENCODE_CATALOG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to read OpenCode free-model catalog: %s", exc)
        return {
            "models": [],
            "source": "opencode-free-catalog",
            "last_synced_at": None,
            "error": f"Catalog unreadable: {exc}",
        }

    all_models = raw.get("models", [])
    free_models = [m for m in all_models if m.get("free") is True]

    return {
        "models": [
            {
                "id": f"opencode/{m['id']}",
                "label": m.get("name") or m["id"].replace("-", " ").title(),
                "provider": "OpenCode",
                "tier": "free",
                "kind": "manual-fallback",
                "manual_fallback_only": m.get("explicit_manual_fallback_only", False),
                "requires_credentials": False,
                "billing_risk": "none-observed",
                "context_limit": m.get("context_limit"),
                "output_limit": m.get("output_limit"),
            }
            for m in free_models
        ],
        "source": "opencode-free-catalog",
        "last_synced_at": raw.get("last_synced_at"),
    }


# ── MiniMax Token Plan lane ──────────────────────────────────────────
#
# Separate, explicit lane for the **MiniMax Token Plan / subscription**
# models that opencode 1.17.13 ships as built-in providers under
# ``minimax-coding-plan/`` and ``minimax-cn-coding-plan/``. Catalog is
# produced by HomeLab's
# ``scripts/sync-opencode-minimax-token-plan-models.py`` (24h refresh
# via ``opencode-minimax-token-plan-refresh.timer``). The resolver does
# NOT shell out to opencode or call MiniMax: it just reads the
# pre-built catalog and rejects any model id whose provider prefix is
# NOT one of the two Token-Plan prefixes — so the API-metered
# ``minimax/`` / ``minimax-cn/`` variants can never leak into this lane
# even if a future catalog run misconfigured filters.
#
# Exposed under harness key ``opencode-native-minimax-token-plan``,
# distinct from ``opencode-native`` (the OpenCode Free lane) so the
# picker can show both side-by-side without either polluting the other.

_MINIMAX_TOKEN_PLAN_CATALOG_PATH = (
    Path.home() / ".cache" / "homelab" / "opencode-minimax-token-plan-models.json"
)

# Provider-id prefixes that are admission-controlled for this lane.
# Anything else (e.g. ``minimax/`` or ``minimax-cn/``) is rejected
# even if a buggy catalog ran them through. Defense-in-depth.
_MINIMAX_TOKEN_PLAN_PROVIDER_PREFIXES: tuple[str, str] = (
    "minimax-coding-plan",
    "minimax-cn-coding-plan",
)

_MINIMAX_REGION_LABELS: dict[str, str] = {
    "minimax-coding-plan": "international",
    "minimax-cn-coding-plan": "China",
}


def _resolve_opencode_minimax_token_plan_models() -> dict[str, Any]:
    """Resolve MiniMax **Token Plan** (subscription) models only.

    Reads the catalog at
    ``~/.cache/homelab/opencode-minimax-token-plan-models.json`` (owned
    by ``scripts/sync-opencode-minimax-token-plan-models.py`` in the
    HomeLab repo) and translates each entry into a normalized
    ``HarnessModelOption``.

    API-metered model ids (prefix ``minimax/`` or ``minimax-cn/``) are
    deliberately excluded from the catalog by the sync script; this
    resolver additionally re-checks each entry's id and rejects
    anything that is not under one of the two Token-Plan prefixes, so
    the endpoint can never advertise an API-metered model. If the
    catalog is missing or unreadable, returns ``{"models": [], ...,
    "error": "..."}`` rather than crashing — the picker surfaces the
    error and the operator can run the sync script manually.

    :returns: ``{"models": [...], "source": "opencode-minimax-token-plan-catalog",
        "last_synced_at": "..."}``. ``last_synced_at`` is whatever the
        sync script wrote.
    """
    if not _MINIMAX_TOKEN_PLAN_CATALOG_PATH.is_file():
        return {
            "models": [],
            "source": "opencode-minimax-token-plan-catalog",
            "last_synced_at": None,
            "error": (
                "MiniMax Token Plan catalog not found. Run "
                "sync-opencode-minimax-token-plan-models.py to populate."
            ),
        }
    try:
        raw = json.loads(_MINIMAX_TOKEN_PLAN_CATALOG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to read MiniMax Token Plan catalog: %s", exc)
        return {
            "models": [],
            "source": "opencode-minimax-token-plan-catalog",
            "last_synced_at": None,
            "error": f"Catalog unreadable: {exc}",
        }

    models: list[dict[str, Any]] = []
    for entry in raw.get("models", []):
        full_id: str = entry.get("id") or ""
        # ``full_id`` is the OpenCode-qualified form:
        # ``opencode/<provider>/<model>``. Strip the leading
        # ``opencode/`` to get the opencode-CLI form used by the
        # provider prefix check.
        bare_id = full_id[len("opencode/"):] if full_id.startswith("opencode/") else full_id
        provider_id = bare_id.split("/", 1)[0] if "/" in bare_id else ""
        if provider_id not in _MINIMAX_TOKEN_PLAN_PROVIDER_PREFIXES:
            # Defense-in-depth: catalog already filters; this refuses to
            # pass through any API-metered id that sneaks in.
            logger.warning(
                "MiniMax Token Plan catalog returned non-Token-Plan "
                "id %r; omitting from picker.", full_id,
            )
            continue
        region_label = _MINIMAX_REGION_LABELS.get(provider_id, "")
        model_name = entry.get("model_name") or bare_id.split("/", 1)[-1]
        raw_line = entry.get("raw_line") or bare_id
        models.append(
            {
                "id": full_id,
                "label": (
                    f"{entry.get('name') or model_name} "
                    f"— Token Plan / Subscription ({region_label})"
                ),
                "provider": "MiniMax",
                "tier": "subscription",
                "kind": "token-plan",
                "manual_fallback_only": True,
                "requires_credentials": True,
                # Boolean ONLY — the catalog carries no secret value
                # and neither does this endpoint.
                "credentials_present": bool(entry.get("credentials_present")),
                "credential_env_var": "MINIMAX_API_KEY",
                "billing_risk": "token-plan-subscription",
                "context_limit": entry.get("context_limit"),
                "output_limit": entry.get("output_limit"),
                "provider_id": provider_id,
                "region": entry.get("region", ""),
                "release_date": entry.get("release_date", ""),
                "raw_line": raw_line,
            }
        )

    return {
        "models": models,
        "source": "opencode-minimax-token-plan-catalog",
        "last_synced_at": raw.get("last_synced_at"),
    }


# ── Provider registry ────────────────────────────────────────────────
#
# Map canonical harness id → resolver function.
# Each resolver returns a dict with ``models`` (list) and metadata.

_HARNESS_MODEL_PROVIDERS: dict[str, Callable[[], dict[str, Any]]] = {
    "opencode-native": _resolve_opencode_free_models,
    "opencode-native-minimax-token-plan": _resolve_opencode_minimax_token_plan_models,
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
