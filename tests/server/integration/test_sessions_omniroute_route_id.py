"""Routing-field schema validation tests for SessionCreate / Update."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnigent.server.omniroute_routes import NATIVE_OMNIROUTE_ROUTE_IDS, is_known_route_id
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
    }
    assert set(NATIVE_OMNIROUTE_ROUTE_IDS) == expected


def test_curated_coding_combos_accepted_in_create():
    """The three curated coding combos must be persisted end-to-end."""
    for combo in ("auto/best-coding", "auto/coding:fast", "auto/coding:reliable"):
        body = SessionCreateRequest.model_validate(
            {"agent_id": "ag_1", "omniroute_route_id": combo, "reasoning_effort": "medium"}
        )
        assert body.omniroute_route_id == combo


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
    assert is_known_route_id("auto/fake") is False
    assert is_known_route_id(None) is False
