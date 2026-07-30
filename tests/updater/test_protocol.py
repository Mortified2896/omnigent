"""Request serialization and validation tests (issue #38 §1)."""

from __future__ import annotations

import pytest

from omnigent.updater.protocol import (
    Authorization,
    RequestRecord,
    ResultRecord,
    is_valid_request_id,
    is_valid_sha,
    new_request_id,
)


def _authorization() -> Authorization:
    return Authorization(kind="operator", operator="tester")


def test_request_id_format_is_26_chars_crockford_base32() -> None:
    """The id format is the Crockford base32 ULID alphabet at 26 chars."""
    rid = new_request_id()
    assert len(rid) == 26
    assert is_valid_request_id(rid)
    assert all(c.isalnum() for c in rid)


def test_request_id_uniqueness_is_high() -> None:
    """Successive ids should not collide in a small batch."""
    ids = {new_request_id() for _ in range(100)}
    assert len(ids) == 100


def test_sha_validator_accepts_only_lowercase_hex_40() -> None:
    assert is_valid_sha("0" * 40)
    assert is_valid_sha("0123456789abcdef0123456789abcdef01234567")
    assert not is_valid_sha("0123456789abcdef0123456789abcdef0123456")  # 39 chars
    assert not is_valid_sha("0123456789ABCDEF0123456789ABCDEF01234567")  # uppercase
    assert not is_valid_sha("g" * 40)  # not hex


def test_request_record_rejects_malformed_shas() -> None:
    with pytest.raises(ValueError):
        RequestRecord(
            request_id=new_request_id(),
            target_sha="bad",
            expected_current_sha="0" * 40,
            origin_session_id=None,
            origin_conversation_id=None,
            requested_by="operator:tester",
            created_at="2026-01-01T00:00:00Z",
            authorization=_authorization(),
        )


def test_request_record_rejects_malformed_expected_current_sha() -> None:
    with pytest.raises(ValueError):
        RequestRecord(
            request_id=new_request_id(),
            target_sha="0" * 40,
            expected_current_sha="zzzz",
            origin_session_id=None,
            origin_conversation_id=None,
            requested_by="operator:tester",
            created_at="2026-01-01T00:00:00Z",
            authorization=_authorization(),
        )


def test_request_record_rejects_malformed_request_id() -> None:
    with pytest.raises(ValueError):
        RequestRecord(
            request_id="not-a-ulid",
            target_sha="0" * 40,
            expected_current_sha="0" * 40,
            origin_session_id=None,
            origin_conversation_id=None,
            requested_by="operator:tester",
            created_at="2026-01-01T00:00:00Z",
            authorization=_authorization(),
        )


def test_request_record_to_dict_round_trips() -> None:
    record = RequestRecord(
        request_id=new_request_id(),
        target_sha="0" * 40,
        expected_current_sha="0" * 40,
        origin_session_id="conv_abc",
        origin_conversation_id="conv_abc",
        requested_by="operator:tester",
        created_at="2026-01-01T00:00:00Z",
        authorization=_authorization(),
        notes="audit",
    )
    payload = record.to_dict()
    restored = RequestRecord.from_dict(payload)
    assert restored == record


def test_request_record_from_dict_rejects_unknown_fields() -> None:
    payload = {
        "request_id": new_request_id(),
        "target_sha": "0" * 40,
        "expected_current_sha": "0" * 40,
        "origin_session_id": None,
        "origin_conversation_id": None,
        "requested_by": "operator:tester",
        "created_at": "2026-01-01T00:00:00Z",
        "authorization": {"kind": "operator", "operator": "tester"},
        "shell_command": "rm -rf /",  # attempted privilege escalation
    }
    with pytest.raises(ValueError) as exc:
        RequestRecord.from_dict(payload)
    assert "unknown fields" in str(exc.value)
    assert "shell_command" in str(exc.value)


def test_result_record_rejects_unknown_status() -> None:
    with pytest.raises(ValueError):
        ResultRecord(
            request_id=new_request_id(),
            final_status="not_a_state",
            target_sha="0" * 40,
            previous_sha="0" * 40,
            deployed_sha="0" * 40,
            started_at="2026-01-01T00:00:00Z",
            finished_at="2026-01-01T00:00:00Z",
        )


def test_result_record_rejection_factory() -> None:
    rid = new_request_id()
    record = ResultRecord.rejection(
        request_id=rid,
        target_sha="0" * 40,
        reason="malformed sha",
    )
    assert record.final_status == "rejected"
    assert record.failure_reason == "malformed sha"
    assert record.failure_phase == "validating"
    assert record.request_id == rid
