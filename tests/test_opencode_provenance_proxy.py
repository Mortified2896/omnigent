"""Allow-listed structured metadata tests for the OpenCode provenance observer."""

from __future__ import annotations

import pytest

from omnigent.opencode_provenance_proxy import (
    ActiveExecution,
    ModelRequest,
    OpenCodeProvenanceProxy,
    StructuredRuntimeProvenance,
    structured_runtime_provenance,
)


def test_metadata_extracts_model_reasoning_and_stream() -> None:
    metadata = OpenCodeProvenanceProxy._metadata(
        b'{"model":"auto/coding:reliable","reasoning_effort":"high","stream":true}'
    )
    assert metadata == ModelRequest("auto/coding:reliable", "high", True)


def test_metadata_returns_none_for_non_dict() -> None:
    metadata = OpenCodeProvenanceProxy._metadata(b"[]")
    assert metadata == ModelRequest(None, None, None)


def test_structured_provenance_verified_only_for_concrete_provider_model() -> None:
    headers = {
        "x-omniroute-requested-model": "auto/coding:reliable",
        "x-omniroute-selected-provider": "codex",
        "x-omniroute-selected-model": "codex/gpt-5.4-mini",
        "x-omniroute-decision-id": "dec-1",
        "x-omniroute-request-id": "req-1",
        "x-omniroute-fallback-used": "true",
        "x-omniroute-selection-strategy": "round-robin",
        "x-omniroute-billing-class": "subscription",
    }
    provenance = structured_runtime_provenance(headers, approved_combo="auto/coding:reliable")
    assert provenance == StructuredRuntimeProvenance(
        requested_model="auto/coding:reliable",
        actual_provider="codex",
        actual_provider_model="codex/gpt-5.4-mini",
        verified=True,
        fallback_used=True,
        omniroute_request_id="req-1",
        omniroute_decision_id="dec-1",
        selection_strategy="round-robin",
        billing_class="subscription",
    )


def test_structured_provenance_unverified_when_only_approved_combo_echoes() -> None:
    headers = {
        "x-omniroute-requested-model": "auto/coding:reliable",
        "x-omniroute-selected-model": "auto/coding:reliable",
    }
    provenance = structured_runtime_provenance(headers, approved_combo="auto/coding:reliable")
    assert provenance.actual_provider is None
    assert provenance.actual_provider_model is None
    assert provenance.verified is False


def test_structured_provenance_unverified_for_auto_group() -> None:
    headers = {
        "x-omniroute-selected-provider": "auto",
        "x-omniroute-selected-model": "auto/smart",
    }
    provenance = structured_runtime_provenance(headers, approved_combo="auto/smart")
    assert provenance.verified is False


def test_structured_provenance_rejects_unknown_values() -> None:
    headers = {"x-omniroute-selected-provider": "unknown"}
    provenance = structured_runtime_provenance(headers, approved_combo="auto/coding:reliable")
    assert provenance.actual_provider is None
    assert provenance.verified is False


def test_safe_error_caps_response_to_code_only() -> None:
    assert (
        OpenCodeProvenanceProxy._safe_error(b'{"error":{"code":"rate_limited","message":"oops"}}')
        == "rate_limited"
    )
    assert OpenCodeProvenanceProxy._safe_error(b"not json") == "upstream_error"


@pytest.mark.asyncio
async def test_register_clear_round_trip() -> None:
    proxy = OpenCodeProvenanceProxy("https://omniroute.example/v1")
    proxy.register("bridge", ActiveExecution("conv_1", "auto/x", "high"))
    assert proxy._active["bridge"].conversation_id == "conv_1"
    proxy.clear("bridge", "conv_1")
    assert "bridge" not in proxy._active
    proxy.clear("bridge", "conv_other")  # no-op for mismatched conversation
    assert "bridge" not in proxy._active
