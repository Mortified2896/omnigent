"""Security and catalog tests for OpenCode ChatGPT OAuth discovery."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from omnigent import opencode_subscription as subscription

_ACCESS = "fake-access-token-for-tests"
_REFRESH = "fake-refresh-token-for-tests"


def _write(path: Path, value: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _catalog(home: Path) -> Path:
    return _write(
        home / ".cache/opencode/models.json",
        {
            "openai": {
                "id": "openai",
                "npm": "@ai-sdk/openai",
                "doc": "https://platform.openai.com/docs/models",
                "env": ["OPENAI_API_KEY"],
                "models": {},
            },
            "anthropic": {"id": "anthropic", "npm": "@ai-sdk/anthropic", "models": {}},
        },
    )


def _auth(credential: dict[str, object], *, provider: str = "openai") -> dict[str, object]:
    return {provider: credential}


def _oauth(**overrides: object) -> dict[str, object]:
    return {
        "type": "oauth",
        "access": _ACCESS,
        "refresh": _REFRESH,
        "accountId": "fake-account-id",
        "expires": 2_000,
        **overrides,
    }


def _discover(tmp_path: Path, auth: object, *, now_ms: int = 1_000):
    home = tmp_path / "home"
    catalog = _catalog(home)
    _write(home / ".local/share/opencode/auth.json", auth)
    return subscription.discover_chatgpt_oauth_state(
        home=home,
        bridge_root=home / ".omnigent/opencode-native",
        catalog_path=catalog,
        now_ms=now_ms,
    )


def test_valid_chatgpt_oauth_is_classified_without_returning_secrets(tmp_path: Path) -> None:
    state, error = _discover(tmp_path, _auth(_oauth()))
    assert error is None
    assert state is not None and state.provider_id == "openai"
    rendered = repr(state)
    assert _ACCESS not in rendered
    assert _REFRESH not in rendered
    assert "fake-account-id" not in rendered


@pytest.mark.parametrize(
    ("auth", "message"),
    [
        (_auth({"type": "api", "key": "fake-key"}), "not present"),
        (_auth(_oauth(), provider="anthropic"), "not present"),
        (_auth(_oauth(expires=500)), "expired or unusable"),
        (_auth(_oauth(access="")), "malformed or unusable"),
    ],
)
def test_non_chatgpt_or_unusable_credentials_fail_closed(
    tmp_path: Path, auth: object, message: str
) -> None:
    state, error = _discover(tmp_path, auth)
    assert state is None
    assert error is not None and message in error
    assert _ACCESS not in error and _REFRESH not in error


def test_custom_openai_sdk_provider_is_not_chatgpt_oauth(tmp_path: Path) -> None:
    home = tmp_path / "home"
    catalog = _write(
        home / ".cache/opencode/models.json",
        {
            "custom": {
                "id": "custom",
                "npm": "@ai-sdk/openai",
                "doc": "https://example.com/provider",
                "env": ["CUSTOM_API_KEY"],
            }
        },
    )
    _write(home / ".local/share/opencode/auth.json", _auth(_oauth(), provider="custom"))
    state, error = subscription.discover_chatgpt_oauth_state(
        home=home,
        bridge_root=home / ".omnigent/opencode-native",
        catalog_path=catalog,
        now_ms=1_000,
    )
    assert state is None
    assert error == "OpenCode ChatGPT OAuth credential is not present."


def test_builtin_provider_env_is_structurally_classified(tmp_path: Path) -> None:
    home = tmp_path / "home"
    catalog = _catalog(home)
    assert subscription.chatgpt_provider_env("openai", catalog_path=catalog) == ("OPENAI_API_KEY",)
    assert subscription.chatgpt_provider_env("anthropic", catalog_path=catalog) == ()


def test_missing_and_malformed_auth_fail_safely(tmp_path: Path) -> None:
    home = tmp_path / "home"
    catalog = _catalog(home)
    kwargs = {
        "home": home,
        "bridge_root": home / ".omnigent/opencode-native",
        "catalog_path": catalog,
        "now_ms": 1_000,
    }
    state, error = subscription.discover_chatgpt_oauth_state(**kwargs)
    assert state is None and error == "OpenCode authentication is not present."

    path = home / ".local/share/opencode/auth.json"
    path.parent.mkdir(parents=True)
    path.write_text("{broken", encoding="utf-8")
    state, error = subscription.discover_chatgpt_oauth_state(**kwargs)
    assert state is None and error == "OpenCode ChatGPT OAuth credential is not present."


def test_newest_isolated_oauth_store_is_selected(tmp_path: Path) -> None:
    home = tmp_path / "home"
    catalog = _catalog(home)
    root = home / ".omnigent/opencode-native"
    older = _write(root / "older/xdg-data/opencode/auth.json", _auth(_oauth()))
    newer = _write(root / "newer/xdg-data/opencode/auth.json", _auth(_oauth()))
    os.utime(older, ns=(1_000, 1_000))
    os.utime(newer, ns=(2_000, 2_000))
    state, error = subscription.discover_chatgpt_oauth_state(
        home=home, bridge_root=root, catalog_path=catalog, now_ms=1_000
    )
    assert error is None
    assert state is not None and state.auth_path == newer
    assert state.xdg_config_home == root / "newer/xdg-config"


def test_verbose_catalog_preserves_exact_ids_and_reasoning_metadata() -> None:
    output = """openai/gpt-5.4-fast
{
  "id": "gpt-5.4-fast",
  "providerID": "openai",
  "name": "GPT-5.4 Fast",
  "variants": {"low": {"reasoningEffort": "low"}}
}
openai/gpt-5.4-fast
{"id":"gpt-5.4-fast","providerID":"openai","name":"duplicate"}
openai/rewritten
{"id":"different","providerID":"openai","name":"reject"}
"""
    models = subscription._parse_verbose_models(output, "openai")
    assert models == [
        {
            "id": "openai/gpt-5.4-fast",
            "metadata": {
                "id": "gpt-5.4-fast",
                "providerID": "openai",
                "name": "GPT-5.4 Fast",
                "variants": {"low": {"reasoningEffort": "low"}},
            },
        }
    ]


def test_subscription_provider_config_removal_preserves_unrelated_providers() -> None:
    from omnigent.opencode_native_provider import remove_provider_config

    config = {
        "provider": {
            "openai": {"options": {"apiKey": "fake-api-key"}},
            "anthropic": {"options": {"apiKey": "fake-anthropic-key"}},
        }
    }
    result = remove_provider_config(config, provider_id="openai")
    assert "openai" not in result["provider"]
    assert "anthropic" in result["provider"]
    assert "openai" in config["provider"]


def test_catalog_command_removes_api_key_and_never_returns_stderr_secret(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs["env"]
        return subprocess.CompletedProcess(
            command,
            0,
            stdout='openai/model-one\n{"id":"model-one","providerID":"openai","name":"One"}\n',
            stderr=f"must-not-surface {_ACCESS}",
        )

    monkeypatch.setattr(subscription.subprocess, "run", fake_run)
    monkeypatch.setenv("OPENAI_API_KEY", "fake-api-key")
    state = subscription.OpenCodeOAuthState(
        auth_path=tmp_path / "auth.json",
        xdg_data_home=tmp_path / "data",
        xdg_config_home=tmp_path / "config",
        provider_id="openai",
    )
    models, error = subscription.read_open_code_oauth_models(state, executable="opencode")
    assert error is None
    assert [model["id"] for model in models] == ["openai/model-one"]
    assert "OPENAI_API_KEY" not in captured["env"]
    assert captured["command"] == ["opencode", "models", "openai", "--verbose", "--pure"]
    assert _ACCESS not in json.dumps(models)
