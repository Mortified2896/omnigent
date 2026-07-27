"""Catalog contract for OpenCode direct provider model sources."""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

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
    oauth_state = type("OAuthState", (), {"provider_id": "openai"})()
    monkeypatch.setattr(options, "_OPENCODE_MODEL_CATALOG_PATH", models)
    monkeypatch.setattr(options, "_OPENCODE_AUTH_PATH", auth)
    monkeypatch.setattr(options, "_OPENCODE_MINIMAX_TOKEN_PLAN_CATALOG_PATH", minimax)
    monkeypatch.setattr(
        options, "discover_chatgpt_oauth_state", lambda **_kwargs: (oauth_state, None)
    )
    monkeypatch.setattr(
        options,
        "read_open_code_oauth_models",
        lambda _state: (
            [
                {
                    "id": "openai/gpt-5-codex",
                    "metadata": {
                        "name": "GPT-5 Codex",
                        "variants": {
                            "low": {"reasoningEffort": "low"},
                            "high": {"reasoningEffort": "high"},
                        },
                    },
                }
            ],
            None,
        ),
    )

    groups = [resolver() for resolver in options._HARNESS_MODEL_PROVIDERS["opencode-native"]]
    assert groups[0]["models"][0]["id"] == "anthropic/claude-fable-5"
    assert groups[1]["models"][0]["id"] == "openai/gpt-5-codex"
    assert groups[2]["models"][0]["id"] == "minimax-coding-plan/MiniMax-M3"
    assert groups[2]["models"][0]["reasoning_efforts"] == []


def test_api_response_contains_only_sanitized_oauth_classification(monkeypatch) -> None:
    fake_access = "fake-access-token-that-must-not-leak"
    groups = [
        {
            "label": "Codex Subscription",
            "source": "opencode-codex-subscription-catalog",
            "error": None,
            "models": [
                {
                    "id": "openai/gpt-5.4",
                    "label": "GPT-5.4",
                    "provider": "Codex Subscription",
                    "source": "direct",
                    "provider_id": "openai",
                    "access_source": "codex-subscription",
                    "credential_source": "oauth:chatgpt",
                    "availability": "available",
                    "reasoning_efforts": ["low", "high"],
                }
            ],
        }
    ]

    async def resolved_groups():
        return groups

    monkeypatch.setattr(options, "_resolve_opencode_groups", resolved_groups)
    app = FastAPI()
    app.include_router(options.create_harness_model_options_router(), prefix="/v1")
    response = TestClient(app).get("/v1/harness-model-options?harness=opencode-native")
    assert response.status_code == 200
    payload = response.json()
    assert payload["groups"][0]["models"][0]["credential_source"] == "oauth:chatgpt"
    assert payload["models"] == payload["groups"][0]["models"]
    serialized = response.text
    assert fake_access not in serialized
    sensitive_fields = ("access_token", "refresh_token", "accountId")
    assert all(secret not in serialized for secret in sensitive_fields)


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
async def test_codex_group_has_scoped_unavailable_state(monkeypatch) -> None:
    monkeypatch.setattr(
        options,
        "discover_chatgpt_oauth_state",
        lambda **_kwargs: (None, "OpenCode authentication is not present."),
    )

    async def unavailable_omniroute() -> dict[str, object]:
        return {
            "models": [],
            "source": "omniroute",
            "error": "OmniRoute is currently unavailable.",
        }

    monkeypatch.setattr(options, "_resolve_omniroute_models", unavailable_omniroute)
    groups = await options._resolve_opencode_groups()
    assert [group["label"] for group in groups] == [
        "Codex Subscription",
        "MiniMax Token Plan",
        "OmniRoute Combos",
        "Other OpenCode Models",
    ]
    assert groups[0]["models"] == []
    assert groups[0]["error"] == "OpenCode authentication is not present."
    assert groups[2]["error"] == "OmniRoute is currently unavailable."


@pytest.mark.asyncio
async def test_codex_models_are_removed_from_other_group(monkeypatch) -> None:
    duplicate = {"id": "openai/gpt-5.4", "label": "GPT-5.4"}
    monkeypatch.setattr(
        options,
        "_resolve_existing_opencode_models",
        lambda: {"models": [duplicate, {"id": "google/gemini", "label": "Gemini"}]},
    )
    monkeypatch.setattr(
        options,
        "_resolve_codex_subscription_models",
        lambda: {"models": [duplicate], "source": "opencode-codex-subscription-catalog"},
    )
    monkeypatch.setattr(options, "_resolve_minimax_token_plan_models", lambda: {"models": []})
    monkeypatch.setattr(
        options,
        "_HARNESS_MODEL_PROVIDERS",
        {
            "opencode-native": (
                options._resolve_existing_opencode_models,
                options._resolve_codex_subscription_models,
                options._resolve_minimax_token_plan_models,
            )
        },
    )

    async def no_routes() -> dict[str, object]:
        return {"models": [], "source": "omniroute"}

    monkeypatch.setattr(options, "_resolve_omniroute_models", no_routes)
    groups = await options._resolve_opencode_groups()
    assert [model["id"] for model in groups[0]["models"]] == ["openai/gpt-5.4"]
    assert [model["id"] for model in groups[3]["models"]] == ["google/gemini"]
