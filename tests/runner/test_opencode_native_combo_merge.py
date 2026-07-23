"""Focused tests for the fixed-route V1 opencode combo-merge helper.

The fixed-route V1 wiring declares a locked OmniRoute combo id
(e.g. ``custom/best-coding``) under ``executor.model`` directly on the
spec, with the routing recommender turned off at the session level.
In that path no ``omniroute_route_id`` is persisted, so the historical
``merge_omniroute_combo_catalog`` branch (which only ran for explicit
routes) skipped the live catalog merge and the spawned opencode
subprocess got an empty provider.models dict — opencode then rejected
the dispatch with ``ProviderModelNotFoundError: Model not found:
custom/best-coding``.

This module locks the ``_is_omniroute_combo_model_id`` predicate in:
the gate is intentionally narrow so unrelated model ids never slip
through to the catalog merge.
"""

from __future__ import annotations

import pytest

from omnigent.runner.app import _is_omniroute_combo_model_id


@pytest.mark.parametrize(
    "model_id",
    [
        # Bare combo ids the fixed-route V1 spec is expected to declare.
        "custom/best-coding",
        "custom/outcome-scoring",
        "auto/coding",
        "auto/best-coding",
        "auto/best-reasoning",
        "auto/best-fast",
        # Already qualified with the omniroute provider id — also valid.
        "omniroute/custom/best-coding",
    ],
)
def test_combo_id_predicate_accepts_omniroute_combos(model_id: str) -> None:
    assert _is_omniroute_combo_model_id(model_id) is True


@pytest.mark.parametrize(
    "model_id",
    [
        # Physical vendor/model ids — these contain a dot, dash, or colon
        # in the model half and must NOT trigger the catalog merge.
        "anthropic/claude-sonnet-4-6",
        "anthropic/databricks-claude-sonnet-4-6",
        "openai/gpt-5.5",
        "google/gemini-2.5-pro",
        "opencode/minimax-m3",
        # Databricks / Anthropic style sub-models with multi-segment slugs.
        "databricks/databricks-claude-sonnet-4-6",
    ],
)
def test_combo_id_predicate_rejects_physical_provider_models(model_id: str) -> None:
    """Physical provider/model ids do not look like OmniRoute combos."""
    assert _is_omniroute_combo_model_id(model_id) is False


@pytest.mark.parametrize(
    "model_id",
    [
        None,
        "",
        "   ",
        "single-segment-no-slash",
        # Triple-slash — physically nonsense AND not a combo.
        "auto/coding/fast",
        # A bare id with an extra slash mid-combo is not a combo either.
        "custom/foo/bar",
    ],
)
def test_combo_id_predicate_handles_unrecognized_shapes(model_id: object) -> None:
    """The gate returns ``False`` for any non-combo-shaped string."""
    assert _is_omniroute_combo_model_id(model_id) is False  # type: ignore[arg-type]
