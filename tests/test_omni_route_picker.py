"""Focused tests for the OmniRoute-backed pre-launch model picker."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from omnigent.omni_route_picker import (
    OmniRouteCatalogEntry,
    OmniRouteModelOptionsError,
    build_picker_options,
    dedupe_catalog,
    fetch_omni_route_catalog,
    filter_catalog_for_harness,
    merge_pinned_models,
    omni_route_model_options,
)
from omnigent.onboarding.provider_config import default_provider_for_harness


def _gateway_config(
    *,
    name: str = "omniroute",
    default_model: str = "custom/best-coding",
    pinned: dict[str, str] | None = None,
    api_key: str | None = "test-key",
    base_url: str = "http://127.0.0.1:20128/v1",
) -> dict[str, Any]:
    """Build a minimal gateway-kind provider config.

    The provider declares ``default: [openai, pi]`` so the picker sees it
    as the harness default. The default fixtures pin the three GPT-5.6
    model ids the operator's deployment pins, since pretty much every
    test inspects them.
    """
    models: dict[str, str] = {
        "default": default_model,
        "gpt56_sol": "codex/gpt-5.6-sol",
        "gpt56_terra": "codex/gpt-5.6-terra",
        "gpt56_luna": "codex/gpt-5.6-luna",
    }
    if pinned is not None:
        models.update(pinned)
    openai_block: dict[str, Any] = {
        "base_url": base_url,
        "wire_api": "chat",
        "models": models,
    }
    if api_key is not None:
        openai_block["api_key"] = api_key
    return {
        "providers": {
            name: {
                "kind": "gateway",
                "default": ["openai", "pi"],
                "openai": openai_block,
            }
        }
    }


def _transport_returning(payload: dict[str, Any]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    return httpx.MockTransport(handler)


def _payload(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"object": "list", "data": rows}


def test_dedupe_catalog_collapses_duplicate_ids() -> None:
    entries = [
        OmniRouteCatalogEntry(id="codex/gpt-5.4", display_name="GPT 5.4"),
        OmniRouteCatalogEntry(id="cx/gpt-5.4", display_name="GPT 5.4 (alt)"),
        OmniRouteCatalogEntry(id="custom/best-coding"),
        OmniRouteCatalogEntry(id="custom/best-coding"),
    ]
    out = dedupe_catalog(entries)
    assert [e.id for e in out] == ["codex/gpt-5.4", "cx/gpt-5.4", "custom/best-coding"]


def test_filter_catalog_for_harness_pi_keeps_every_chat_model() -> None:
    entries = [
        OmniRouteCatalogEntry(id="codex/gpt-5.4"),
        OmniRouteCatalogEntry(id="custom/best-coding"),
        OmniRouteCatalogEntry(id="claude-opus-4.7"),
        OmniRouteCatalogEntry(id="minimax/M3"),
    ]
    out = filter_catalog_for_harness(entries, "pi")
    assert len(out) == 4


def test_filter_catalog_for_harness_codex_rejects_non_gpt() -> None:
    entries = [
        OmniRouteCatalogEntry(id="codex/gpt-5.4"),
        OmniRouteCatalogEntry(id="custom/best-coding"),
        OmniRouteCatalogEntry(id="claude-opus-4.7"),
        OmniRouteCatalogEntry(id="minimax/M3"),
    ]
    out = filter_catalog_for_harness(entries, "codex")
    assert [e.id for e in out] == ["codex/gpt-5.4"]


def test_merge_pinned_models_appends_pins_not_in_catalog() -> None:
    entries = [
        OmniRouteCatalogEntry(id="codex/gpt-5.4"),
        OmniRouteCatalogEntry(id="custom/best-coding"),
    ]
    pinned = ["codex/gpt-5.6-sol", "codex/gpt-5.6-terra", "codex/gpt-5.6-luna"]
    out = merge_pinned_models(entries, pinned)
    assert [e.id for e in out] == [
        "codex/gpt-5.4",
        "custom/best-coding",
        "codex/gpt-5.6-sol",
        "codex/gpt-5.6-terra",
        "codex/gpt-5.6-luna",
    ]


def test_merge_pinned_models_skips_pins_already_in_catalog() -> None:
    out = merge_pinned_models(
        [OmniRouteCatalogEntry(id="custom/best-coding")], ["custom/best-coding"]
    )
    assert [e.id for e in out] == ["custom/best-coding"]


def test_build_picker_options_marks_default_and_uses_model_key() -> None:
    entries = [
        OmniRouteCatalogEntry(id="custom/best-coding"),
        OmniRouteCatalogEntry(id="codex/gpt-5.4"),
    ]
    rows = build_picker_options(entries, default_model="custom/best-coding")
    by_id = {row["id"]: row for row in rows}
    assert by_id["custom/best-coding"]["isDefault"] is True
    assert by_id["codex/gpt-5.4"]["isDefault"] is False
    for row in rows:
        assert row["id"] == row["model"]
        assert "displayName" in row
        assert row["isCurrent"] is False


async def test_fetch_omni_route_catalog_uses_bearer_and_strips_non_llm() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json=_payload(
                [
                    {"id": "codex/gpt-5.4", "name": "GPT 5.4"},
                    {"id": "mistral-embed", "type": "embedding"},
                    {"id": "img", "type": "image"},
                    {"id": "reranker", "type": "rerank"},
                ]
            ),
        )

    entry = default_provider_for_harness(_gateway_config(), "pi")
    entries = fetch_omni_route_catalog(entry, transport=httpx.MockTransport(handler))
    assert captured["url"].endswith("/v1/models")
    assert (captured["authorization"] or "").startswith("Bearer ")
    assert [e.id for e in entries] == ["codex/gpt-5.4"]


async def test_fetch_omni_route_catalog_raises_on_http_error() -> None:
    entry = default_provider_for_harness(_gateway_config(), "pi")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream down")

    with pytest.raises(OmniRouteModelOptionsError) as exc_info:
        fetch_omni_route_catalog(entry, transport=httpx.MockTransport(handler))
    assert "503" in str(exc_info.value)


async def test_fetch_omni_route_catalog_raises_when_all_non_chat() -> None:
    entry = default_provider_for_harness(_gateway_config(), "pi")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_payload(
                [
                    {"id": "a-embed", "type": "embedding"},
                    {"id": "b-image", "type": "image"},
                    {"id": "c-audio", "type": "audio"},
                ]
            ),
        )

    with pytest.raises(OmniRouteModelOptionsError) as exc_info:
        fetch_omni_route_catalog(entry, transport=httpx.MockTransport(handler))
    assert "no chat-capable models" in str(exc_info.value)


def test_omni_route_model_options_pi_includes_default_and_three_gpt56() -> None:
    """The Pi picker contract: ``custom/best-coding`` default + GPT-5.6 trio.

    The configured ``models:`` pins guarantee the GPT-5.6 ids appear in
    the picker even when the live catalog has not yet published them.
    """
    rows = omni_route_model_options(
        "pi",
        config_loader=lambda: _gateway_config(),
        transport=_transport_returning(
            _payload(
                [
                    {"id": "auto/best-coding"},
                    {"id": "custom/best-coding"},
                    {"id": "codex/gpt-5.4"},
                ]
            )
        ),
    )
    ids = [row["id"] for row in rows]
    assert "custom/best-coding" in ids
    assert "codex/gpt-5.6-sol" in ids
    assert "codex/gpt-5.6-terra" in ids
    assert "codex/gpt-5.6-luna" in ids
    defaults = [row for row in rows if row.get("isDefault")]
    assert [row["id"] for row in defaults] == ["custom/best-coding"]


def test_omni_route_model_options_dedupes_exact_id_repeats() -> None:
    rows = omni_route_model_options(
        "pi",
        config_loader=lambda: _gateway_config(),
        transport=_transport_returning(
            _payload(
                [
                    {"id": "codex/gpt-5.4"},
                    {"id": "codex/gpt-5.4"},
                    {"id": "custom/best-coding"},
                    {"id": "custom/best-coding"},
                ]
            )
        ),
    )
    assert [row["id"] for row in rows[:2]] == ["codex/gpt-5.4", "custom/best-coding"]


def test_omni_route_model_options_codex_filters_to_openai_family_only() -> None:
    rows = omni_route_model_options(
        "codex",
        config_loader=lambda: _gateway_config(),
        transport=_transport_returning(
            _payload(
                [
                    {"id": "custom/best-coding"},  # generic → rejected
                    {"id": "codex/gpt-5.4"},
                    {"id": "claude-opus-4.7"},
                    {"id": "minimax/M3"},
                ]
            )
        ),
    )
    ids = [row["id"] for row in rows]
    assert "custom/best-coding" not in ids
    assert "claude-opus-4.7" not in ids
    assert "minimax/M3" not in ids
    assert "codex/gpt-5.4" in ids
    assert "codex/gpt-5.6-sol" in ids
    assert "codex/gpt-5.6-terra" in ids
    assert "codex/gpt-5.6-luna" in ids


def test_omni_route_model_options_fails_loud_when_filter_excludes_default() -> None:
    with pytest.raises(OmniRouteModelOptionsError):
        omni_route_model_options(
            "codex",
            config_loader=lambda: _gateway_config(default_model="custom/best-coding"),
            transport=_transport_returning(
                _payload([{"id": "custom/best-coding"}, {"id": "claude-opus-4.7"}])
            ),
        )


def test_omni_route_model_options_raises_when_no_provider_configured() -> None:
    with pytest.raises(OmniRouteModelOptionsError) as exc_info:
        omni_route_model_options(
            "pi",
            config_loader=lambda: {"providers": {}},
            transport=_transport_returning(_payload([{"id": "gpt-4o"}])),
        )
    assert "no default provider" in str(exc_info.value)


def test_omni_route_model_options_raises_when_provider_is_not_gateway() -> None:
    config = {
        "providers": {
            "workspace": {
                "kind": "databricks",
                "profile": "DEFAULT",
                "default": ["openai", "pi"],
            }
        }
    }
    with pytest.raises(OmniRouteModelOptionsError) as exc_info:
        omni_route_model_options(
            "pi",
            config_loader=lambda: config,
            transport=_transport_returning(_payload([{"id": "gpt-4o"}])),
        )
    assert "databricks" in str(exc_info.value)


async def test_host_handle_model_options_routes_pi_through_omni_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import omnigent.omni_route_picker as omni_route_picker_module
    from omnigent.host.connect import HostProcess
    from omnigent.host.frames import HostModelOptionsFrame, HostModelOptionsResultFrame
    from omnigent.host.identity import HostIdentity

    host = HostProcess(
        HostIdentity(host_id="t1", name="test"), "http://localhost:8000"
    )

    def fake_omni_route_model_options(harness: str) -> list[dict[str, Any]]:
        return [
            {
                "id": "custom/best-coding",
                "model": "custom/best-coding",
                "displayName": "Best coding",
                "isDefault": True,
                "isCurrent": False,
            },
            {
                "id": "codex/gpt-5.6-sol",
                "model": "codex/gpt-5.6-sol",
                "displayName": "GPT 5.6 Sol",
                "isDefault": False,
                "isCurrent": False,
            },
        ]

    monkeypatch.setattr(
        omni_route_picker_module,
        "omni_route_model_options",
        fake_omni_route_model_options,
    )

    result = await host._handle_model_options(
        HostModelOptionsFrame(request_id="r1", harness="qwen")
    )

    assert result.status == "ok"
    assert result.error is None
    assert [row["id"] for row in (result.models or [])] == [
        "custom/best-coding",
        "codex/gpt-5.6-sol",
    ]


async def test_host_handle_model_options_surfaces_picker_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import omnigent.omni_route_picker as omni_route_picker_module
    from omnigent.host.connect import HostProcess
    from omnigent.host.frames import HostModelOptionsFrame
    from omnigent.host.identity import HostIdentity

    host = HostProcess(
        HostIdentity(host_id="t1", name="test"), "http://localhost:8000"
    )

    def fake_omni_route_model_options(harness: str) -> list[dict[str, Any]]:
        raise OmniRouteModelOptionsError(
            "provider 'omniroute' has no 'openai' family to fetch models from"
        )

    monkeypatch.setattr(
        omni_route_picker_module,
        "omni_route_model_options",
        fake_omni_route_model_options,
    )

    # ``qwen`` is a representative OpenAI-compatible harness that has no
    # specialized branch on v0.8.1 — it falls through to the OmniRoute
    # picker on the catch-all path.
    result = await host._handle_model_options(
        HostModelOptionsFrame(request_id="r1", harness="qwen")
    )

    assert result.status == "failed"
    assert result.request_id == "r1"
    assert "no 'openai' family" in (result.error or "")
