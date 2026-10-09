"""A native CLI catalogue is not evidence that the gateway serves a model."""

from __future__ import annotations

import json
from io import BytesIO
from urllib.error import URLError

import pytest

from omnigent.harnesses.codex_native import app_server


@pytest.fixture
def gateway(monkeypatch):
    launch = app_server.NativeCodexLaunch(
        config_overrides=[],
        model=None,
        profile=None,
        summary="gateway",
        credential_env={"GATEWAY_KEY": "test-only-credential"},
    )
    monkeypatch.setattr(
        app_server, "native_codex_launch_base_url", lambda _launch: "http://127.0.0.1:20128/v1"
    )
    return launch


NATIVE = [
    {
        "id": "gpt-6-luna",
        "model": "gpt-6-luna",
        "displayName": "GPT-6-Luna",
        "isDefault": True,
        "supportedReasoningEfforts": [{"reasoningEffort": "max"}],
    }
]


@pytest.mark.parametrize("model", ["gpt-6-luna", "codex/gpt-6-luna"])
def test_gateway_uses_confirmed_wire_name_and_native_efforts(monkeypatch, gateway, model):
    def fetch(request, *, timeout):
        assert request.full_url == "http://127.0.0.1:20128/api/v1/providers/codex/models"
        assert request.get_header("Authorization") == "Bearer test-only-credential"
        assert timeout == 10
        return BytesIO(
            json.dumps(
                {
                    "data": [
                        {"id": model},
                        {"id": model},
                        {"id": "codex/model-not-in-native-catalog"},
                        {"id": "openai/gpt-6-luna"},
                    ]
                }
            ).encode()
        )

    monkeypatch.setattr("urllib.request.urlopen", fetch)
    rows = app_server.omniroute_codex_model_options(gateway, NATIVE)
    assert rows == [{**NATIVE[0], "id": "codex/gpt-6-luna", "model": "codex/gpt-6-luna"}]


@pytest.mark.parametrize("payload", [{"data": []}, {"error": "expired"}, {"data": None}])
def test_expired_or_empty_gateway_does_not_inherit_cli_models(monkeypatch, gateway, payload):
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda *a, **k: BytesIO(json.dumps(payload).encode())
    )
    assert app_server.omniroute_codex_model_options(gateway, NATIVE) == []


def test_unreachable_gateway_is_not_qualified(monkeypatch, gateway):
    def unavailable(*args, **kwargs):
        raise URLError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", unavailable)
    assert app_server.omniroute_codex_model_options(gateway, NATIVE) == []


def test_unconfigured_gateway_does_not_probe_another_connection(monkeypatch, gateway):
    monkeypatch.setattr(app_server, "_launch_bearer_token", lambda _launch: None)
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda *a, **k: pytest.fail("unconfigured probe")
    )
    assert app_server.omniroute_codex_model_options(gateway, NATIVE) == []
