"""Read-only route for OpenCode free-model catalog.

Exposes ``GET /v1/opencode/free-models`` so the web UI can populate
the AgentPicker with validated free models from the local catalog.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request

from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user

logger = logging.getLogger(__name__)

_CATALOG_PATH = Path.home() / ".cache" / "homelab" / "opencode-free-models.json"


def create_opencode_free_models_router(
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    """Build the OpenCode free-models router.

    :param auth_provider: Auth provider, or ``None`` for single-user mode.
    :returns: A configured :class:`APIRouter`.
    """
    router = APIRouter()

    @router.get("/opencode/free-models")
    async def get_opencode_free_models(request: Request) -> dict[str, Any]:
        """Return the validated OpenCode free-model catalog.

        Reads the local catalog written by the ``opencode-free-model-refresh``
        timer and returns only models with ``free=true``. Fails with a clear
        error if the catalog is missing or unparseable.

        :returns: ``{"models": [...], "last_synced_at": "...", "free_model_count": N}``.
        """
        require_user(request, auth_provider)

        if not _CATALOG_PATH.exists():
            return {
                "models": [],
                "last_synced_at": None,
                "free_model_count": 0,
                "error": "Catalog not found. Run opencode models to refresh.",
            }

        try:
            raw = json.loads(_CATALOG_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to read OpenCode free-model catalog: %s", exc)
            return {
                "models": [],
                "last_synced_at": None,
                "free_model_count": 0,
                "error": f"Catalog unreadable: {exc}",
            }

        all_models = raw.get("models", [])
        free_models = [m for m in all_models if m.get("free") is True]

        return {
            "models": [
                {
                    "id": f"opencode/{m['id']}",
                    "name": m.get("name") or m["id"].replace("-", " ").title(),
                    "context_limit": m.get("context_limit"),
                    "output_limit": m.get("output_limit"),
                }
                for m in free_models
            ],
            "last_synced_at": raw.get("last_synced_at"),
            "free_model_count": len(free_models),
        }

    return router
