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

pytestmark = pytest.mark.asyncio


def _gateway_config(
    *,
    name: str = "omniroute",
    default_model: str = "custom/best-coding",
    pinned: dict[str, str] | None = None,
    api_key_ref: str = "env:OMNIROUTE_API_KEY",
    base_url: str = "http://127.0.0.1:20128/v1",
) -> dict[str, Any]:
    """Build a minimal gateway-kind provider config.

    The provider declares ``default: [openai, pi]`` so the picker sees it
    as the harness default — mirroring the operator's deployment. The
    default fixtures pin the three GPT-5.6 model ids the operator's
    deployment makes default, since pretty much every test inspects them.
    """

    models: dict[str, str] = {
        "default": default_model,
        "gpt56_sol": "codex/gpt-5.6-sol",
        "gpt56_terra": "codex/gpt-5.6-terra",
        "gpt56_luna": "codex/gpt-5.6-luna",
    }
    if pinned is not None:
        models.update(pinned)
    return {
        "providers": {
            name: {
                "kind": "gateway",
                "default": ["openai", "pi"],
                "openai": {
                    "base_url": base_url,
                    "api_key_ref": api_key_ref,
                    "wire_api": "chat",
                    "models": models,
                },
            }
        }
    }


def _transport_returning(payload: dict[str, Any]) -> httpx.MockTransport:
    """Return a MockTransport that answers with *payload* for any URL."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    return httpx.MockTransport(handler)


def _transport_failing(exc: Exception) -> httpx.MockTransport:
    """Return a MockTransport that raises *exc* on any request."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    return httpx.MockTransport(handler)


def _payload(model_entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Wrap OpenAI-compatible ``/v1/models`` rows into the response envelope."""

    return {"object": "list", "data": model_entries}


def test_dedupe_catalog_collapses_duplicate_ids() -> None:
    """OpenRouter-style catalogs publish each model under prefixed aliases."""
    entries = [
        OmniRouteCatalogEntry(id="codex/gpt-5.4", display_name="GPT 5.4"),
        OmniRouteCatalogEntry(id="cx/gpt-5.4", display_name="GPT 5.4 (alt)"),
        OmniRouteCatalogEntry(id="custom/best-coding"),
        OmniRouteCatalogEntry(id="custom/best-coding"),
    ]
    out = dedupe_catalog(entries)
    # Three distinct ids; first occurrence wins so the operator-curated
    # ``codex/`` prefix survives over the alias ``cx/``.
    assert [e.id for e in out] == ["codex/gpt-5.4", "cx/gpt-5.4", "custom/best-coding"]


def test_filter_catalog_for_harness_pi_keeps_every_chat_model() -> None:
    """Pi is multi-model; chat filter must preserve claude/gpt/generic ids."""
    entries = [
        OmniRouteCatalogEntry(id="codex/gpt-5.4"),
        OmniRouteCatalogEntry(id="custom/best-coding"),
        OmniRouteCatalogEntry(id="claude-opus-4.7"),
        OmniRouteCatalogEntry(id="minimax/M3"),
    ]
    out = filter_catalog_for_harness(entries, "pi")
    assert len(out) == 4


def test_filter_catalog_for_harness_codex_rejects_claude_and_generic() -> None:
    """Codex rejects claude and generic ids; preserves codex/gpt entries."""
    entries = [
        OmniRouteCatalogEntry(id="codex/gpt-5.4"),
        OmniRouteCatalogEntry(id="custom/best-coding"),  # generic → rejected
        OmniRouteCatalogEntry(id="claude-opus-4.7"),
        OmniRouteCatalogEntry(id="minimax/M3"),
    ]
    out = filter_catalog_for_harness(entries, "codex")
    assert [e.id for e in out] == ["codex/gpt-5.4"]


def test_merge_pinned_models_appends_pins_not_in_catalog() -> None:
    """The configured default is guaranteed a slot even when not in the catalog."""
    entries = [
        OmniRouteCatalogEntry(id="codex/gpt-5.4"),
        OmniRouteCatalogEntry(id="custom/best-coding"),
    ]
    pinned = ["codex/gpt-5.6-sol", "codex/gpt-5.6-terra", "codex/gpt-5.6-luna"]
    out = merge_pinned_models(entries, pinned)
    ids = [e.id for e in out]
    # Existing rows first, in order; then the pinned ids absent from the catalog.
    assert ids == [
        "codex/gpt-5.4",
        "custom/best-coding",
        "codex/gpt-5.6-sol",
        "codex/gpt-5.6-terra",
        "codex/gpt-5.6-luna",
    ]


def test_merge_pinned_models_skips_pins_already_in_catalog() -> None:
    """A pinned id that already lives in the catalog stays in its native row."""
    entries = [OmniRouteCatalogEntry(id="custom/best-coding")]
    pinned = ["custom/best-coding"]
    out = merge_pinned_models(entries, pinned)
    assert [e.id for e in out] == ["custom/best-coding"]


def test_build_picker_options_marks_default_and_uses_model_key() -> None:
    """Picker rows carry ``id`` / ``model`` (identical) and one ``isDefault`` row."""
    entries = [
        OmniRouteCatalogEntry(id="custom/best-coding"),
        OmniRouteCatalogEntry(id="codex/gpt-5.4"),
    ]
    rows = build_picker_options(entries, default_model="custom/best-coding")
    by_id = {row["id"]: row for row in rows}
    assert by_id["custom/best-coding"]["isDefault"] is True
    assert by_id["codex/gpt-5.4"]["isDefault"] is False
    # The picker reads ``id`` and ``model`` interchangeably downstream; both
    # must be set to the resolved catalog id.
    for row in rows:
        assert row["id"] == row["model"]
        assert "displayName" in row
        assert row["isCurrent"] is False


async def test_fetch_omni_route_catalog_uses_bearer_and_strips_non_llm() -> None:
    """Live catalog read hits ``/v1/models`` with ``Authorization: Bearer``."""
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

    # Build a provider entry through the public loader to keep the test
    # aligned with how ``omni_route_model_options`` constructs one.
    from omnigent.onboarding.provider_config import default_provider_for_harness, load_config

    entry = default_provider_for_harness(
        load_config.__wrapped__() if hasattr(load_config, "__wrapped__") else _gateway_config(),
        "pi",
    )

    entries = fetch_omni_route_catalog(entry, transport=httpx.MockTransport(handler))
    assert captured["url"].endswith("/v1/models")
    assert captured["authorization"].startswith("Bearer ")

    ids = [e.id for e in entries]
    # Non-LLM types (embedding / image / rerank) were dropped before they
    # could reach the harness filter — they have no chat wire and never
    # drive a coding session.
    assert ids == ["codex/gpt-5.4"]


async def test_fetch_omni_route_catalog_raises_on_http_error() -> None:
    """The picker fails loud on HTTP errors instead of returning rows."""
    from omnigent.onboarding.provider_config import default_provider_for_harness

    entry = default_provider_for_harness(_gateway_config(), "pi")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream down")

    with pytest.raises(OmniRouteModelOptionsError) as exc_info:
        fetch_omni_route_catalog(entry, transport=httpx.MockTransport(handler))
    assert "503" in str(exc_info.value)


async def test_fetch_omni_route_catalog_raises_when_all_non_chat() -> None:
    """A catalog of only embed/image/audio/etc. produces a clear failure."""
    from omnigent.onboarding.provider_config import default_provider_for_harness

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

    This is the focused regression for the user's deployment — the live
    catalog may not yet advertise the GPT-5.6 ids, but the configured
    ``models:`` pins guarantee they appear in the picker.
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
    # The configured default is the only ``isDefault`` row — a side-effect
    # of the catalog also publishing ``auto/best-coding``.
    defaults = [row for row in rows if row["isDefault"]]
    assert [row["id"] for row in defaults] == ["custom/best-coding"]


def test_omni_route_model_options_dedupes_exact_id_repeats() -> None:
    """Exact id duplicates collapse to one row; prefixed aliases coexist."""
    rows = omni_route_model_options(
        "pi",
        config_loader=lambda: _gateway_config(),
        transport=_transport_returning(
            _payload(
                [
                    {"id": "codex/gpt-5.4"},
                    {"id": "codex/gpt-5.4"},  # exact repeat → dropped
                    {"id": "custom/best-coding"},
                    {"id": "custom/best-coding"},  # exact repeat → dropped
                ]
            )
        ),
    )
    # The four-row catalog collapsed to two unique ids, in first-occurrence
    # order. Prefixed aliases such as ``codex/x`` vs ``cx/x`` are distinct
    # ids in the gateway's vocabulary and would both survive the id-keyed
    # dedupe (the runtime must choose its preferred prefix).
    assert [row["id"] for row in rows[:2]] == ["codex/gpt-5.4", "custom/best-coding"]


def test_omni_route_model_options_codex_filters_to_openai_family_only() -> None:
    """Codex rejects claude and bare ids; preserves codex/gpt entries + pins."""
    rows = omni_route_model_options(
        "codex",
        config_loader=lambda: _gateway_config(),
        transport=_transport_returning(
            _payload(
                [
                    {"id": "custom/best-coding"},  # generic → rejected
                    {"id": "codex/gpt-5.4"},
                    {"id": "claude-opus-4.7"},  # wrong family → rejected
                    {"id": "minimax/M3"},  # wrong family → rejected
                ]
            )
        ),
    )
    ids = [row["id"] for row in rows]
    assert "custom/best-coding" not in ids
    assert "claude-opus-4.7" not in ids
    assert "minimax/M3" not in ids
    # Live chat row preserved:
    assert "codex/gpt-5.4" in ids
    # Pinned ids are openai-family — they survive both the live filter and
    # the harness family filter.
    assert "codex/gpt-5.6-sol" in ids
    assert "codex/gpt-5.6-terra" in ids
    assert "codex/gpt-5.6-luna" in ids


def test_omni_route_model_options_fails_loud_when_filter_excludes_default() -> None:
    """An empty post-filter set raises — no silent fallback to curated list."""
    with pytest.raises(OmniRouteModelOptionsError):
        omni_route_model_options(
            "codex",
            # The family default is bare/ambiguous ("custom/best-coding");
            # the codex harness rejects it via family_mismatch and the
            # filter empties the set, which must raise rather than silently
            # return a curated catalog.
            config_loader=lambda: _gateway_config(default_model="custom/best-coding"),
            transport=_transport_returning(
                _payload([{"id": "custom/best-coding"}, {"id": "claude-opus-4.7"}])
            ),
        )


def test_omni_route_model_options_raises_when_no_provider_configured() -> None:
    """No provider default → a clear, actionable picker error (no fallback)."""
    with pytest.raises(OmniRouteModelOptionsError) as exc_info:
        omni_route_model_options(
            "pi",
            config_loader=lambda: {"providers": {}},  # no provider at all
            transport=_transport_returning(_payload([{"id": "gpt-4o"}])),
        )
    assert "no default provider" in str(exc_info.value)


def test_omni_route_model_options_raises_when_provider_is_not_gateway() -> None:
    """A databricks provider has no chat catalog surface — fail loud."""
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
    """End-to-end: ``host.model_options`` for ``pi-native`` returns picker rows."""
    import omnigent.omni_route_picker as omni_route_picker_module
    from omnigent.host.connect import HostProcess
    from omnigent.host.frames import HostModelOptionsFrame, HostModelOptionsResultFrame
    from omnigent.host.identity import HostIdentity

    host = HostProcess(HostIdentity(host_id="t1", name="test"), "http://localhost:8000")

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
        HostModelOptionsFrame(request_id="r1", harness="pi-native")
    )
    assert result == HostModelOptionsResultFrame(
        request_id="r1",
        status="ok",
        models=[
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
        ],
    )


async def test_host_handle_model_options_surfaces_picker_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``host.model_options`` returns the picker error message verbatim."""
    import omnigent.omni_route_picker as omni_route_picker_module
    from omnigent.host.connect import HostProcess
    from omnigent.host.frames import HostModelOptionsFrame
    from omnigent.host.identity import HostIdentity

    host = HostProcess(HostIdentity(host_id="t1", name="test"), "http://localhost:8000")

    def fake_omni_route_model_options(harness: str) -> list[dict[str, Any]]:
        raise OmniRouteModelOptionsError(
            "provider 'omniroute' has no 'openai' family to fetch models from"
        )

    monkeypatch.setattr(
        omni_route_picker_module,
        "omni_route_model_options",
        fake_omni_route_model_options,
    )
    result = await host._handle_model_options(HostModelOptionsFrame(request_id="r1", harness="pi"))
    assert result.status == "failed"
    assert result.request_id == "r1"
    assert "no 'openai' family" in (result.error or "")
