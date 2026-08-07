"""Tests for the thin pre-launch model picker adapter."""

from __future__ import annotations

import pytest

from omnigent.model_catalog import ModelEntry, ModelListing
from omnigent.onboarding.provider_config import FamilyConfig, ProviderEntry
from omnigent.prelaunch_model_options import (
    PrelaunchModelOptionsError,
    _pinned_pi_models,
    _render_options,
    pi_native_prelaunch_model_options,
)


def _listing(*ids: str, source: str = "openai-compatible") -> ModelListing:
    return ModelListing(
        source=source,
        verified=True,
        models=tuple(ModelEntry(id=model_id, family="other") for model_id in ids),
        note="live test catalog",
    )


def test_render_options_dedupes_and_merges_pinned_without_faking_default() -> None:
    options = _render_options(
        _listing("custom/best-coding", "codex/gpt-5.6-sol", "custom/best-coding"),
        pinned=("custom/best-coding", "codex/gpt-5.6-terra"),
    )

    assert [row["id"] for row in options] == [
        "custom/best-coding",
        "codex/gpt-5.6-sol",
        "codex/gpt-5.6-terra",
    ]
    assert not any(row.get("isDefault") for row in options)


def test_render_options_marks_only_the_explicit_configured_default() -> None:
    options = _render_options(
        _listing("custom/best-coding", "codex/gpt-5.6-sol"),
        default_model="custom/best-coding",
    )

    assert options[0]["isDefault"] is True
    assert "isDefault" not in options[1]


def test_render_options_fails_loud_when_provider_resolution_failed() -> None:
    listing = ModelListing(
        source="none",
        verified=False,
        models=(),
        note="model enumeration failed: listing endpoint returned HTTP 503",
    )

    with pytest.raises(PrelaunchModelOptionsError, match="HTTP 503"):
        _render_options(listing, pinned=("custom/best-coding",))


def test_render_options_does_not_let_pinned_ids_mask_empty_live_catalog() -> None:
    with pytest.raises(PrelaunchModelOptionsError, match="live test catalog"):
        _render_options(_listing(), pinned=("custom/best-coding",))


def test_pinned_pi_models_reads_both_pi_capable_families_and_dedupes() -> None:
    provider = ProviderEntry(
        name="omniroute",
        kind="gateway",
        families={
            "anthropic": FamilyConfig(
                base_url="https://gateway.invalid/v1",
                api_key="secret",
                models={"default": "custom/best-coding", "fast": "claude-fast"},
            ),
            "openai": FamilyConfig(
                base_url="https://gateway.invalid/v1",
                api_key="secret",
                models={"default": "custom/best-coding", "sol": "codex/gpt-5.6-sol"},
            ),
        },
        default_families=frozenset({"pi"}),
    )

    assert _pinned_pi_models(provider) == (
        "custom/best-coding",
        "claude-fast",
        "codex/gpt-5.6-sol",
    )


def test_pi_adapter_uses_upstream_listing_plus_configured_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import omnigent.prelaunch_model_options as module

    provider = ProviderEntry(
        name="omniroute",
        kind="gateway",
        families={
            "openai": FamilyConfig(
                base_url="https://gateway.invalid/v1",
                api_key="secret",
                models={
                    "default": "custom/best-coding",
                    "sol": "codex/gpt-5.6-sol",
                    "terra": "codex/gpt-5.6-terra",
                },
            )
        },
        default_families=frozenset({"pi"}),
    )
    observed: dict[str, str] = {}

    def fake_list_models(spec: object, harness: str) -> ModelListing:
        observed["harness"] = harness
        observed["spec_harness"] = str(spec.executor.config["harness"])  # type: ignore[attr-defined]
        return _listing("custom/best-coding", "codex/gpt-5.6-sol")

    monkeypatch.setattr(module, "list_models_for_worker", fake_list_models)
    monkeypatch.setattr(module, "_configured_pi_provider", lambda: provider)

    options = pi_native_prelaunch_model_options()

    assert observed == {"harness": "pi-native", "spec_harness": "pi-native"}
    assert [row["id"] for row in options] == [
        "custom/best-coding",
        "codex/gpt-5.6-sol",
        "codex/gpt-5.6-terra",
    ]
    assert options[0]["isDefault"] is True
