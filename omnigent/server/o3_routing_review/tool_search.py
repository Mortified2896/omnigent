"""Destination evidence and private Codex metadata for O3 tool discovery."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import tomllib

CAPABILITIES_ENV = "OMNIGENT_O3_TOOL_SEARCH_CAPABILITIES"
CAPABILITIES_FILENAME = "tool-search-capabilities.json"
STATE_DIRECTORY_NAME = "o3"


def capability_path() -> Path:
    """Keep local route qualifications outside the portable model catalogue."""
    return Path(
        os.environ.get(CAPABILITIES_ENV)
        or Path.home() / ".omnigent" / STATE_DIRECTORY_NAME / CAPABILITIES_FILENAME
    )


def read_capabilities(path: Path | None = None) -> dict[str, Any] | None:
    """Missing evidence disables optimization; malformed evidence fails closed."""
    path = path or capability_path()
    if not path.exists():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    if raw.get("schema_version") != 1 or not isinstance(raw.get("routes"), dict):
        raise ValueError("invalid O3 tool-search qualification registry")
    return raw


def qualified_route(registry: dict[str, Any], provider: str, route: str) -> dict[str, Any] | None:
    """Require exact provider/route evidence, never a model-name heuristic."""
    row = registry["routes"].get(route)
    if (
        not isinstance(row, dict)
        or row.get("provider") != provider
        or row.get("supports_search_tool") is not True
        or row.get("search_call") is not True
        or row.get("loaded_tool_call") is not True
        or not isinstance(row.get("resolved_model"), str)
        or not row.get("evidence")
    ):
        return None
    return row


def combo_capability(
    combo: dict[str, Any], registry: dict[str, Any], catalog: dict[str, Any]
) -> dict[str, Any] | None:
    """Intersect all primary/fallback targets with installed client metadata."""
    targets = combo.get("models")
    if not isinstance(targets, list) or not targets:
        return None
    entries = {row["slug"]: row for row in catalog["models"]}
    descriptors = []
    for target in targets:
        if not isinstance(target, dict) or target.get("kind") != "model":
            return None
        provider, route = target.get("providerId"), target.get("model")
        if not isinstance(provider, str) or not isinstance(route, str):
            return None
        row = qualified_route(registry, provider, route)
        entry = entries.get(row["resolved_model"]) if row else None
        if entry is None or entry.get("supports_search_tool") is not True:
            return None
        descriptors.append(entry)
    # Preserve the negotiated alias window rather than a destination's larger one.
    window = combo.get("computed_context_length")
    if type(window) is not int or window <= 0:
        return None
    descriptor = copy.deepcopy(descriptors[0])
    descriptor.update(
        slug=combo["name"],
        display_name="O3 approved route",
        context_window=window,
        supports_search_tool=True,
        visibility="hide",
        availability_nux=None,
        upgrade=None,
    )
    for key in descriptor:
        if key.startswith("supports_") and isinstance(descriptor[key], bool):
            descriptor[key] = all(entry.get(key) is True for entry in descriptors)
    # A route advertises only the shared client feature, not another model's mode.
    descriptor.pop("tool_mode", None)
    return descriptor


def write_alias_catalog(
    home: Path, combo: dict[str, Any], registry: dict[str, Any], catalog: dict[str, Any]
) -> dict[str, Any]:
    """Merge into a private copy; never change the host catalogue or MCP registry."""
    descriptor = combo_capability(combo, registry, catalog)
    extended = copy.deepcopy(catalog)
    extended["models"] = [row for row in extended["models"] if row["slug"] != combo["name"]]
    if descriptor is not None:
        extended["models"].append(descriptor)
    path = home / f"{STATE_DIRECTORY_NAME}-model-catalog.json"
    path.write_text(json.dumps(extended), encoding="utf-8")
    evidence = {
        "alias": combo["name"],
        "supports_search_tool": descriptor is not None,
        "targets": combo["models"],
        "context_window": combo.get("computed_context_length"),
        "qualification_sha256": hashlib.sha256(
            json.dumps(registry, sort_keys=True).encode()
        ).hexdigest(),
    }
    (home / f"{STATE_DIRECTORY_NAME}-tool-search.json").write_text(
        json.dumps(evidence, indent=2), encoding="utf-8"
    )
    return {"path": path, **evidence}


async def prepare_alias_catalog(home: Path, model: str | None, codex_path: str) -> Path | None:
    """Resolve alias metadata before the native client builds its first request."""
    if model is None or not model.startswith("custom/o3-route-"):
        return None
    from omnigent.inner.codex_executor import read_codex_model_catalog

    from .omniroute import OmniRouteClient

    registry = read_capabilities()
    if registry is None:
        return None
    client = OmniRouteClient.from_env()
    health = await client._request("GET", "/api/monitoring/health")
    if (
        registry.get("gateway_url") != client.base_url
        or not registry.get("gateway_version")
        or registry["gateway_version"] != health.body.get("version")
    ):
        raise ValueError("O3 tool-search qualifications must be refreshed for this gateway")
    fallback_chains = await client._request("GET", "/api/fallback/chains")
    if fallback_chains.body:
        # An external fallback frontier cannot borrow the Combo's qualifications.
        registry = {**registry, "routes": {}, "unqualified_fallback_chains": True}
    combo = await client.get_combo(model)
    if combo is None:
        raise ValueError("O3 route disappeared before Codex startup")
    config = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
    path = config.get("model_catalog_json")
    if path:
        catalog = json.loads(Path(path).read_text(encoding="utf-8"))
    else:
        import asyncio

        catalog = await asyncio.to_thread(read_codex_model_catalog, codex_path, home)
    if not catalog:
        raise ValueError("O3 cannot establish the installed Codex model catalogue")
    result = write_alias_catalog(home, combo, registry, catalog)
    return result["path"]
