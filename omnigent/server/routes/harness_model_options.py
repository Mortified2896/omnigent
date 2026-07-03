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


# ── Provider registry ────────────────────────────────────────────────
#
# Map canonical harness id → resolver function.
# Each resolver returns a dict with ``models`` (list) and metadata.

_HARNESS_MODEL_PROVIDERS: dict[str, Callable[[], dict[str, Any]]] = {
    "opencode-native": _resolve_opencode_free_models,
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
