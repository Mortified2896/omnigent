"""Catalog contract for OpenCode direct provider model sources."""

from __future__ import annotations

import json

import pytest

from omnigent.harness_plugins import native_agents
from omnigent.server.routes import harness_model_options as options


def _write(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_provider_plans_are_not_coding_harnesses() -> None:
    identities = {
        value
        for agent in native_agents()
        for value in (agent.key, agent.agent_name, agent.harness)
    }
    assert sum(agent.harness == "opencode-native" for agent in native_agents()) == 1
    assert not any(
        "minimax-token-plan" in value or "codex-subscription" in value for value in identities
    )
    assert not hasattr(options, "OPENCODE_NATIVE_MINIMAX_TOKEN_PLAN_CODING_AGENT")
    assert not hasattr(options, "OPENCODE_NATIVE_CODEX_SUBSCRIPTION_CODING_AGENT")


def test_opencode_groups_preserve_provider_model_ids_and_isolate_failures(
    tmp_path, monkeypatch
) -> None:
    models = _write(
        tmp_path / "models.json",
        {"anthropic": {"name": "Anthropic", "models": {"claude-fable-5": {"name": "Fable"}}}},
    )
    auth = _write(tmp_path / "auth.json", {"anthropic": {"type": "oauth"}})
    minimax = _write(
        tmp_path / "minimax.json",
        {
            "models": [
                {
                    "id": "opencode/minimax-coding-plan/MiniMax-M3",
                    "name": "MiniMax M3",
                    "credentials_present": True,
                    "raw_metadata": {"reasoning_options": [{"type": "toggle"}]},
                },
                {"id": "opencode/minimax/MiniMax-M3", "name": "metered"},
            ]
        },
    )
    codex = _write(
        tmp_path / "codex.json",
        {
            "models": [
                {
                    "id": "opencode/openai/gpt-5-codex",
                    "name": "GPT-5 Codex",
                    "model_source": "codex-subscription",
                    "credential_source": "oauth:chatgpt",
                    "credentials_present": True,
                    "raw_metadata": {
                        "reasoning_options": [{"type": "effort", "values": ["low", "high"]}]
                    },
                }
            ]
        },
    )
    monkeypatch.setattr(options, "_OPENCODE_MODEL_CATALOG_PATH", models)
    monkeypatch.setattr(options, "_OPENCODE_AUTH_PATH", auth)
    monkeypatch.setattr(options, "_OPENCODE_MINIMAX_TOKEN_PLAN_CATALOG_PATH", minimax)
    monkeypatch.setattr(options, "_OPENCODE_CODEX_SUBSCRIPTION_CATALOG_PATH", codex)

    groups = [resolver() for resolver in options._HARNESS_MODEL_PROVIDERS["opencode-native"]]
    assert groups[0]["models"][0]["id"] == "anthropic/claude-fable-5"
    assert groups[1]["models"][0]["id"] == "openai/gpt-5-codex"
    assert groups[2]["models"][0]["id"] == "minimax-coding-plan/MiniMax-M3"
    assert groups[2]["models"][0]["reasoning_efforts"] == []


def test_minimax_includes_all_and_only_authenticated_provider_ids(tmp_path, monkeypatch) -> None:
    catalog = _write(
        tmp_path / "minimax.json",
        {
            "models": [
                {
                    "id": f"opencode/{provider}/MiniMax-M{index}",
                    "name": f"MiniMax M{index}",
                    "credentials_present": True,
                }
                for provider in ("minimax-coding-plan", "minimax-cn-coding-plan")
                for index in range(1, 8)
            ]
            + [
                {
                    "id": "opencode/minimax-coding-plan/unavailable",
                    "credentials_present": False,
                },
                {"id": "opencode/minimax/not-a-plan", "credentials_present": True},
            ]
        },
    )
    monkeypatch.setattr(options, "_OPENCODE_MINIMAX_TOKEN_PLAN_CATALOG_PATH", catalog)

    result = options._resolve_minimax_token_plan_models()
    ids = [model["id"] for model in result["models"]]
    assert len(ids) == 14
    assert all(id.startswith(("minimax-coding-plan/", "minimax-cn-coding-plan/")) for id in ids)
    assert "minimax-coding-plan/unavailable" not in ids


@pytest.mark.asyncio
async def test_codex_group_is_omitted_without_authenticated_oauth_entries(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        options, "_OPENCODE_CODEX_SUBSCRIPTION_CATALOG_PATH", tmp_path / "missing.json"
    )

    async def unavailable_omniroute() -> dict[str, object]:
        return {
            "models": [],
            "source": "omniroute",
            "error": "OmniRoute is currently unavailable.",
        }

    monkeypatch.setattr(options, "_resolve_omniroute_models", unavailable_omniroute)
    groups = await options._resolve_opencode_groups()
    assert "Codex Subscription" not in [group["label"] for group in groups]
    assert groups[-1]["label"] == "OmniRoute"
    assert groups[-1]["error"]


def test_codex_requires_open_code_oauth_catalog_marker(tmp_path, monkeypatch) -> None:
    cache = _write(
        tmp_path / "codex.json",
        {
            "models": [
                {
                    "id": "openai/gpt-5-codex",
                    "model_source": "codex-subscription",
                    "name": "unverified",
                }
            ]
        },
    )
    monkeypatch.setattr(options, "_OPENCODE_CODEX_SUBSCRIPTION_CATALOG_PATH", cache)
    result = options._resolve_codex_subscription_models()
    assert result["models"] == []
    assert result["error"]
