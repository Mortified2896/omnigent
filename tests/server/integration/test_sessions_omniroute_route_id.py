"""Routing-field schema validation tests for SessionCreate / Update.

These tests pin the canonical-route contract:

* The catalog contains built-in ``auto/*`` virtual Auto Combos and the
  persisted ``custom/best-coding`` interactive-execution combo.
* ``custom/outcome-scoring`` is **deliberately** NOT in the catalog:
  it is the M3-only background Task Outcome evaluator route.
* The display label (e.g. ``"OmniRoute Coding Best"``) is rejected as
  a route id.
* The ``omniroute/`` transport prefix is normalized back to the bare
  canonical id.
* Unknown ``custom/*`` ids are rejected.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnigent.server.omniroute_routes import (
    CUSTOM_BEST_CODING_DISPLAY_NAME,
    NATIVE_OMNIROUTE_ROUTE_IDS,
    RESERVED_NON_EXECUTABLE_ROUTE_IDS,
    executable_route_ids,
    is_executable_route_id,
    is_known_route_id,
    normalize_route_id,
)
from omnigent.server.schemas import SessionCreateRequest, UpdateSessionRequest


def test_native_route_ids_catalog_complete():
    expected = {
        "auto",
        "auto/cheap",
        "auto/best-free",
        "auto/best-coding",
        "auto/coding",
        "auto/coding:fast",
        "auto/coding:cheap",
        "auto/coding:free",
        "auto/coding:pro",
        "auto/coding:reliable",
        "auto/smart",
        "auto/fast",
        "auto/reasoning",
        "auto/reasoning:pro",
        "auto/vision",
        "auto/multimodal",
        "custom/best-coding",
    }
    assert set(NATIVE_OMNIROUTE_ROUTE_IDS) == expected


def test_reserved_background_routes_excluded_from_executable_set():
    """``custom/outcome-scoring`` must NOT be in ``executable_route_ids()``."""
    assert "custom/outcome-scoring" in RESERVED_NON_EXECUTABLE_ROUTE_IDS
    assert "custom/outcome-scoring" not in set(executable_route_ids())


def test_curated_coding_combos_accepted_in_create():
    """The three curated coding combos must be persisted end-to-end."""
    for combo in ("auto/best-coding", "auto/coding:fast", "auto/coding:reliable"):
        body = SessionCreateRequest.model_validate(
            {"agent_id": "ag_1", "omniroute_route_id": combo, "reasoning_effort": "medium"}
        )
        assert body.omniroute_route_id == combo


def test_custom_best_coding_is_accepted_in_create():
    """``custom/best-coding`` is the canonical interactive execution route."""
    body = SessionCreateRequest.model_validate(
        {
            "agent_id": "ag_1",
            "omniroute_route_id": "custom/best-coding",
            "reasoning_effort": "medium",
        }
    )
    assert body.omniroute_route_id == "custom/best-coding"


def test_custom_outcome_scoring_is_rejected_for_interactive_execution():
    """The M3-only background evaluator route is NOT user-selectable."""
    with pytest.raises(ValidationError, match="custom/outcome-scoring"):
        SessionCreateRequest.model_validate(
            {"agent_id": "ag_1", "omniroute_route_id": "custom/outcome-scoring"}
        )


def test_display_label_is_rejected_as_route_id():
    """The display label ``OmniRoute Coding Best`` is not a route id."""
    with pytest.raises(ValidationError, match=CUSTOM_BEST_CODING_DISPLAY_NAME):
        SessionCreateRequest.model_validate(
            {"agent_id": "ag_1", "omniroute_route_id": CUSTOM_BEST_CODING_DISPLAY_NAME}
        )


def test_unknown_custom_route_is_rejected():
    """Unknown ``custom/*`` ids do not get blanket-allowed."""
    with pytest.raises(ValidationError, match="custom/secret"):
        SessionCreateRequest.model_validate(
            {"agent_id": "ag_1", "omniroute_route_id": "custom/secret"}
        )


def test_omniroute_transport_prefix_is_normalized_to_canonical():
    """``omniroute/custom/best-coding`` is normalized to ``custom/best-coding``."""
    body = SessionCreateRequest.model_validate(
        {"agent_id": "ag_1", "omniroute_route_id": "omniroute/custom/best-coding"}
    )
    assert body.omniroute_route_id == "custom/best-coding"


def test_omniroute_transport_prefix_normalization_persists_to_patch():
    body = UpdateSessionRequest.model_validate(
        {"omniroute_route_id": "omniroute/custom/best-coding"}
    )
    assert body.omniroute_route_id == "custom/best-coding"


def test_unknown_route_id_is_rejected_in_create():
    with pytest.raises(ValidationError):
        SessionCreateRequest.model_validate(
            {
                "agent_id": "ag_1",
                "omniroute_route_id": "auto/fake",
            }
        )


def test_known_route_id_is_accepted_in_create():
    body = SessionCreateRequest.model_validate(
        {
            "agent_id": "ag_1",
            "omniroute_route_id": "auto/coding",
            "reasoning_effort": "medium",
            "permission_mode": "ask_before_edits",
        }
    )
    assert body.omniroute_route_id == "auto/coding"


def test_unknown_permission_mode_rejected_in_create():
    with pytest.raises(ValidationError):
        SessionCreateRequest.model_validate({"agent_id": "ag_1", "permission_mode": "root"})


def test_unknown_route_id_is_rejected_in_patch():
    with pytest.raises(ValidationError):
        UpdateSessionRequest.model_validate({"omniroute_route_id": "auto/fake"})


def test_known_route_id_is_accepted_in_patch():
    body = UpdateSessionRequest.model_validate({"omniroute_route_id": "auto/reasoning"})
    assert body.omniroute_route_id == "auto/reasoning"


def test_is_known_route_id_helper():
    assert is_known_route_id("auto/coding") is True
    assert is_known_route_id("auto/coding:pro") is True
    assert is_known_route_id("auto/coding:free") is True
    assert is_known_route_id("custom/best-coding") is True
    assert is_known_route_id("auto/fake") is False
    assert is_known_route_id(None) is False


def test_is_executable_route_id_helper():
    assert is_executable_route_id("custom/best-coding") is True
    assert is_executable_route_id("auto/coding") is True
    assert is_executable_route_id("omniroute/custom/best-coding") is True
    # Background-only combo must be rejected even though it round-trips
    # via normalize_route_id.
    assert is_executable_route_id("custom/outcome-scoring") is False
    assert is_executable_route_id("omniroute/custom/outcome-scoring") is False
    # Display labels are not route ids.
    assert is_executable_route_id(CUSTOM_BEST_CODING_DISPLAY_NAME) is False
    # Unknown custom routes must be rejected.
    assert is_executable_route_id("custom/secret") is False
    assert is_executable_route_id(None) is False
    assert is_executable_route_id("") is False
    assert is_executable_route_id("   ") is False


def test_normalize_route_id_helper():
    assert normalize_route_id("custom/best-coding") == "custom/best-coding"
    assert normalize_route_id("omniroute/custom/best-coding") == "custom/best-coding"
    assert normalize_route_id("  custom/best-coding  ") == "custom/best-coding"
    assert normalize_route_id(CUSTOM_BEST_CODING_DISPLAY_NAME) == "OmniRoute Coding Best"
    assert normalize_route_id(None) is None
    assert normalize_route_id("") is None
    assert normalize_route_id("   ") is None
    assert normalize_route_id("omniroute/") is None
