"""Authenticated OpenCode model catalogs for the new-session picker.

Provider plans are model sources under the single ``opencode-native``
executor. Reading one catalog must not make the other provider groups
unavailable.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query, Request

from omnigent.opencode_subscription import (
    discover_chatgpt_oauth_state,
    read_open_code_oauth_models,
)
from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user

logger = logging.getLogger(__name__)

_OPENCODE_MODEL_CATALOG_PATH = Path.home() / ".cache" / "opencode" / "models.json"
_OPENCODE_AUTH_PATH = Path.home() / ".local" / "share" / "opencode" / "auth.json"
_OPENCODE_FREE_CATALOG_PATH = Path.home() / ".cache" / "homelab" / "opencode-free-models.json"
_OPENCODE_MINIMAX_TOKEN_PLAN_CATALOG_PATH = (
    Path.home() / ".cache" / "homelab" / "opencode-minimax-token-plan-models.json"
)
_OPENCODE_NATIVE_BRIDGE_ROOT = Path.home() / ".omnigent" / "opencode-native"
_OPENCODE_PREFIX = "opencode/"
_MINIMAX_PROVIDERS = frozenset({"minimax-coding-plan", "minimax-cn-coding-plan"})


def _catalog_error(source: str, message: str) -> dict[str, Any]:
    return {"models": [], "source": source, "last_synced_at": None, "error": message}


def _read_json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("catalog must be a JSON object")
    return value


def _read_catalog(path: Path, source: str, missing: str) -> dict[str, Any]:
    if not path.is_file():
        return _catalog_error(source, missing)
    try:
        return _read_json_object(path)
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        logger.warning("Failed to read %s: %s", source, exc)
        return _catalog_error(source, f"Catalog unreadable: {exc}")


def _bare_model_id(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value[len(_OPENCODE_PREFIX) :] if value.startswith(_OPENCODE_PREFIX) else value


def _reasoning_efforts(metadata: object) -> list[str]:
    """Extract explicit effort values from generated or live OpenCode metadata."""
    if not isinstance(metadata, dict):
        return []
    options = metadata.get("reasoning_options")
    if isinstance(options, list):
        for option in options:
            if isinstance(option, dict) and option.get("type") == "effort":
                values = option.get("values")
                if isinstance(values, list) and all(isinstance(value, str) for value in values):
                    return values
    variants = metadata.get("variants")
    if not isinstance(variants, dict):
        return []
    return list(
        dict.fromkeys(
            variant.get("reasoningEffort")
            for variant in variants.values()
            if isinstance(variant, dict) and isinstance(variant.get("reasoningEffort"), str)
        )
    )


def _model_option(
    *,
    model_id: str,
    label: str,
    provider: str,
    metadata: object = None,
    **extra: Any,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": model_id,
        "label": label,
        "provider": provider,
        "reasoning_efforts": _reasoning_efforts(metadata),
        **extra,
    }
    if isinstance(metadata, dict):
        variants = metadata.get("variants")
        if isinstance(variants, dict):
            result["variants"] = list(variants)
    return result


def _resolve_existing_opencode_models() -> dict[str, Any]:
    """Return authenticated non-plan OpenCode providers from its real cache."""
    source = "opencode-authenticated-catalog"
    raw = _read_catalog(_OPENCODE_MODEL_CATALOG_PATH, source, "OpenCode catalog not found.")
    if "error" in raw:
        return raw
    auth = _read_catalog(
        _OPENCODE_AUTH_PATH, "opencode-auth", "OpenCode authentication not found."
    )
    authenticated = set(auth) if "error" not in auth else set()
    # The Zen provider is usable without a separately stored provider token.
    authenticated.add("opencode")
    models: list[dict[str, Any]] = []
    for provider_id in sorted(authenticated):
        if provider_id in _MINIMAX_PROVIDERS or provider_id in {"openai", "codex", "omniroute"}:
            continue
        provider = raw.get(provider_id)
        if not isinstance(provider, dict):
            continue
        provider_name = (
            provider.get("name") if isinstance(provider.get("name"), str) else provider_id
        )
        provider_models = provider.get("models")
        if not isinstance(provider_models, dict):
            continue
        for model_key, metadata in provider_models.items():
            if not isinstance(model_key, str) or not isinstance(metadata, dict):
                continue
            label = metadata.get("name") if isinstance(metadata.get("name"), str) else model_key
            models.append(
                _model_option(
                    model_id=f"{provider_id}/{model_key}",
                    label=label,
                    provider=str(provider_name),
                    metadata=metadata,
                    source="authenticated-opencode",
                    availability="authenticated",
                )
            )
    return {"models": models, "source": source, "last_synced_at": None}


def _resolve_opencode_free_models() -> dict[str, Any]:
    source = "opencode-free-catalog"
    raw = _read_catalog(
        _OPENCODE_FREE_CATALOG_PATH, source, "OpenCode free-model catalog not found."
    )
    if "error" in raw:
        return raw
    models = [
        _model_option(
            model_id=f"opencode/{entry['id']}",
            label=str(entry.get("name") or entry["id"]),
            provider="OpenCode",
            metadata={
                "reasoning_options": [{"type": "effort", "values": entry.get("variants", [])}]
            },
            source="direct",
            availability="available",
        )
        for entry in raw.get("models", [])
        if isinstance(entry, dict)
        and entry.get("free") is True
        and isinstance(entry.get("id"), str)
    ]
    return {"models": models, "source": source, "last_synced_at": raw.get("last_synced_at")}


def _resolve_minimax_token_plan_models() -> dict[str, Any]:
    source = "opencode-minimax-token-plan-catalog"
    raw = _read_catalog(
        _OPENCODE_MINIMAX_TOKEN_PLAN_CATALOG_PATH, source, "MiniMax Token Plan catalog not found."
    )
    if "error" in raw:
        return raw
    models: list[dict[str, Any]] = []
    for entry in raw.get("models", []):
        if not isinstance(entry, dict):
            continue
        model_id = _bare_model_id(entry.get("id"))
        provider_id = model_id.partition("/")[0]
        if (
            provider_id not in _MINIMAX_PROVIDERS
            or not model_id
            or entry.get("credentials_present") is not True
        ):
            continue
        metadata = entry.get("raw_metadata")
        models.append(
            _model_option(
                model_id=model_id,
                label=str(entry.get("name") or entry.get("model_name") or model_id),
                provider="MiniMax Token Plan",
                metadata=metadata,
                source="direct",
                provider_id=provider_id,
                access_source="minimax-token-plan",
                availability="available" if entry.get("credentials_present") else "needs-auth",
            )
        )
    return {"models": models, "source": source, "last_synced_at": raw.get("last_synced_at")}


async def _resolve_omniroute_models() -> dict[str, Any]:
    """Return routes only while OmniRoute is reachable, never a fallback."""
    from omnigent.server.omniroute_catalog import fetch_live_omniroute_combo_catalog

    combos = await fetch_live_omniroute_combo_catalog()
    if not combos:
        return _catalog_error("omniroute", "OmniRoute is currently unavailable.")
    return {
        "models": [
            _model_option(
                model_id=combo.id,
                label=combo.display_name,
                provider="OmniRoute",
                metadata={
                    "reasoning_options": [
                        {"type": "effort", "values": list(combo.reasoning_efforts)}
                    ]
                },
                source="omniroute",
                route_id=combo.id,
                availability="available",
            )
            for combo in combos
        ],
        "source": "omniroute",
        "last_synced_at": None,
    }


def _resolve_codex_subscription_models() -> dict[str, Any]:
    """Read only the live model catalog backed by OpenCode ChatGPT OAuth."""
    source = "opencode-codex-subscription-catalog"
    state, error = discover_chatgpt_oauth_state(
        bridge_root=_OPENCODE_NATIVE_BRIDGE_ROOT,
        catalog_path=_OPENCODE_MODEL_CATALOG_PATH,
    )
    if state is None:
        return _catalog_error(source, error or "Codex Subscription is unavailable.")
    entries, error = read_open_code_oauth_models(state)
    if error:
        return _catalog_error(source, error)

    models: list[dict[str, Any]] = []
    for entry in entries:
        model_id = entry.get("id")
        metadata = entry.get("metadata")
        if not isinstance(model_id, str) or not isinstance(metadata, dict):
            continue
        label = metadata.get("name") if isinstance(metadata.get("name"), str) else model_id
        models.append(
            _model_option(
                model_id=model_id,
                label=label,
                provider="Codex Subscription",
                metadata=metadata,
                source="direct",
                provider_id=state.provider_id,
                access_source="codex-subscription",
                credential_source="oauth:chatgpt",
                availability="available",
            )
        )
    if not models:
        return _catalog_error(source, "OpenCode exposes no ChatGPT OAuth models.")
    return {"models": models, "source": source, "last_synced_at": None, "error": None}


# Provider plans have exactly one executor: the existing OpenCode native harness.
_HARNESS_MODEL_PROVIDERS: dict[str, tuple[Callable[[], dict[str, Any]], ...]] = {
    "opencode-native": (
        _resolve_existing_opencode_models,
        _resolve_codex_subscription_models,
        _resolve_minimax_token_plan_models,
    ),
}


async def _resolve_opencode_groups() -> list[dict[str, Any]]:
    existing, codex, minimax = await asyncio.gather(
        *(asyncio.to_thread(resolver) for resolver in _HARNESS_MODEL_PROVIDERS["opencode-native"])
    )
    codex_ids = {model["id"] for model in codex["models"]}
    existing["models"] = [
        model for model in existing["models"] if model.get("id") not in codex_ids
    ]
    codex["label"] = "Codex Subscription"
    minimax["label"] = "MiniMax Token Plan"
    omniroute = await _resolve_omniroute_models()
    omniroute["label"] = "OmniRoute Combos"
    existing["label"] = "Other OpenCode Models"
    return [codex, minimax, omniroute, existing]


def create_harness_model_options_router(auth_provider: AuthProvider | None = None) -> APIRouter:
    router = APIRouter()

    @router.get("/harness-model-options")
    async def get_harness_model_options(
        request: Request,
        harness: str = Query(..., description="Canonical harness id, e.g. opencode-native"),
    ) -> dict[str, Any]:
        require_user(request, auth_provider)
        groups = await _resolve_opencode_groups() if harness == "opencode-native" else []
        return {
            "harness": harness,
            "source": "direct" if groups else None,
            "models": [model for group in groups for model in group.get("models", [])],
            "groups": groups,
            "last_synced_at": None,
            "error": None,
        }

    return router
