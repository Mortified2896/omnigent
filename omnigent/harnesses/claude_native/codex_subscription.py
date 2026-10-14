"""Qualify an explicitly configured Claude-to-Codex subscription bridge."""

from __future__ import annotations

import asyncio
import subprocess
from urllib.parse import urlparse

import httpx

from omnigent.harnesses.claude_native.codex_bridge import BRIDGE_PROTOCOL


def bridge_profile(config) -> dict | None:
    from omnigent.inference_config import load_runtime_inference_config

    profile = load_runtime_inference_config().get("claude_codex_bridge")
    if not isinstance(profile, dict) or config is None:
        return None
    url = config.env.get("ANTHROPIC_BASE_URL")
    if (
        url != profile.get("base_url")
        or config.api_key_helper != profile.get("auth_command")
        or tuple(profile.get("models", [])) != config.routable_models
        or urlparse(url or "").hostname not in {"127.0.0.1", "localhost"}
    ):
        return None
    return profile


async def subscription_identity(config) -> dict | None:
    profile = bridge_profile(config)
    if profile is None:
        return None

    def credential():
        # Same owner-configured helper as native Claude; stdout stays in memory.
        result = subprocess.run(
            profile["auth_command"],
            shell=True,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        return result.stdout.strip()

    key = await asyncio.to_thread(credential)
    async with httpx.AsyncClient(trust_env=False, timeout=20) as client:
        response = await client.get(profile["base_url"] + "/identity", headers={"x-api-key": key})
        response.raise_for_status()
        identity = response.json()
    if (
        identity.get("protocol") != BRIDGE_PROTOCOL
        or identity.get("provider") != "openai"
        or identity.get("account_type") != "chatgpt"
        or not identity.get("entitlement_key", "").startswith("chatgpt-plan:bridge:")
        or not identity.get("connection_id", "").startswith("codex-claude-bridge:")
    ):
        return None
    return identity


async def qualified_models(config) -> list[dict] | None:
    identity = await subscription_identity(config)
    if identity is None:
        return None
    # Exact allowlist plus the CLI's live catalog, never provider-name inference.
    rows = []
    for model in identity.get("models", []):
        name = model.get("model")
        if name not in config.routable_models:
            continue
        efforts = [
            e
            for e in model.get("supportedReasoningEfforts", [])
            if e.get("reasoningEffort") in {"low", "medium", "high", "xhigh", "max"}
        ]
        if not efforts:
            continue
        rows.append(
            {
                "id": name,
                "model": name,
                "displayName": (model.get("displayName") or name) + " · Claude Code",
                "supportedReasoningEfforts": efforts,
                "defaultReasoningEffort": model.get("defaultReasoningEffort"),
                "accessLane": "claude-codex",
                "advisorProvider": "openai",
                "advisorHarness": "claude-native",
                "advisorAccessClass": "chatgpt_plan",
                "advisorConnectionId": identity["connection_id"],
                "advisorEntitlementKey": identity["entitlement_key"],
                "advisorCanonicalModel": name,
            }
        )
    return rows
