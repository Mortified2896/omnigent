"""Durable store tests (issue #38 §1).

Covers atomic request creation, duplicate-request handling,
checkpoint writes, append-only event logging, and result
persistence.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from omnigent.updater.protocol import (
    Authorization,
    RequestRecord,
    ResultRecord,
    new_request_id,
)
from omnigent.updater.state_machine import UpdatePhase
from omnigent.updater.store import (
    DuplicateRequestError,
    UpdaterStore,
)


def _authorization() -> Authorization:
    return Authorization(kind="operator", operator="tester")


def _make_request(target: str = "0" * 40, expected: str = "0" * 40) -> RequestRecord:
    return RequestRecord(
        request_id=new_request_id(),
        target_sha=target,
        expected_current_sha=expected,
        origin_session_id=None,
        origin_conversation_id=None,
        requested_by="operator:tester",
        created_at="2026-01-01T00:00:00Z",
        authorization=_authorization(),
    )


def test_create_request_writes_atomically(state_root: Path) -> None:
    """Atomic write leaves a fully-formed JSON file on disk."""
    store = UpdaterStore(state_root=state_root)
    record = _make_request()
    path = store.create_request(record)
    assert path.is_file()
    payload = json.loads(path.read_text())
    assert payload["request_id"] == record.request_id
    assert payload["target_sha"] == record.target_sha


def test_create_request_rejects_duplicate_id(state_root: Path) -> None:
    """The second create with the same id raises ``DuplicateRequestError``."""
    store = UpdaterStore(state_root=state_root)
    record = _make_request()
    store.create_request(record)
    with pytest.raises(DuplicateRequestError):
        store.create_request(record)


def test_create_request_does_not_leave_tmp_files(state_root: Path) -> None:
    """A successful atomic write removes its tempfile."""
    store = UpdaterStore(state_root=state_root)
    record = _make_request()
    store.create_request(record)
    request_dir = state_root / "requests"
    tmp_files = [f for f in os.listdir(request_dir) if f.startswith(".") and f.endswith(".tmp")]
    assert tmp_files == []


def test_write_checkpoint_persists_phase_and_context(state_root: Path) -> None:
    """Checkpoints record phase, timestamp, and context."""
    store = UpdaterStore(state_root=state_root)
    rid = new_request_id()
    store.create_request(_make_request())
    store.write_checkpoint(rid, UpdatePhase.BUILDING, context={"attempt": 1})
    loaded = store.load_checkpoint(rid)
    assert loaded is not None
    assert loaded.phase == UpdatePhase.BUILDING
    assert loaded.context == {"attempt": 1}
    assert loaded.request_id == rid


def test_write_checkpoint_is_atomic(state_root: Path) -> None:
    """A second checkpoint replaces the first atomically."""
    store = UpdaterStore(state_root=state_root)
    rid = new_request_id()
    store.create_request(_make_request())
    store.write_checkpoint(rid, UpdatePhase.BUILDING)
    store.write_checkpoint(rid, UpdatePhase.DRAINING, context={"active": 2})
    running_dir = state_root / "running"
    tmp_files = [f for f in os.listdir(running_dir) if f.startswith(".") and f.endswith(".tmp")]
    assert tmp_files == []
    loaded = store.load_checkpoint(rid)
    assert loaded.phase == UpdatePhase.DRAINING


def test_append_event_writes_one_jsonl_line(state_root: Path) -> None:
    """Events are appended as single JSONL lines, fsync-ed."""
    store = UpdaterStore(state_root=state_root)
    rid = new_request_id()
    store.create_request(_make_request())
    store.append_event(
        rid,
        UpdatePhase.BUILDING,
        message="build started",
        context={"sha": "abc"},
    )
    events = store.read_events(rid)
    assert len(events) == 1
    assert events[0]["phase"] == UpdatePhase.BUILDING.value
    assert events[0]["message"] == "build started"
    assert events[0]["context"]["sha"] == "abc"


def test_append_event_preserves_order(state_root: Path) -> None:
    """Two appended events appear in insertion order."""
    store = UpdaterStore(state_root=state_root)
    rid = new_request_id()
    store.create_request(_make_request())
    store.append_event(rid, UpdatePhase.QUEUED, message="first")
    store.append_event(rid, UpdatePhase.VALIDATING, message="second")
    store.append_event(rid, UpdatePhase.BUILDING, message="third")
    messages = [event["message"] for event in store.read_events(rid)]
    assert messages == ["first", "second", "third"]


def test_write_result_persists_terminal_record(state_root: Path) -> None:
    """Result records are written atomically to ``results/``."""
    store = UpdaterStore(state_root=state_root)
    rid = new_request_id()
    store.create_request(_make_request())
    record = ResultRecord.rejection(request_id=rid, target_sha="0" * 40, reason="test")
    path = store.write_result(record)
    assert path.is_file()
    payload = json.loads(path.read_text())
    assert payload["final_status"] == "rejected"
    assert payload["request_id"] == rid


def test_pending_delivery_round_trip(state_root: Path) -> None:
    """Pending-delivery files are listed, removed, and cleared by id."""
    store = UpdaterStore(state_root=state_root)
    rid = new_request_id()
    store.create_request(_make_request())
    record = ResultRecord.rejection(request_id=rid, target_sha="0" * 40)
    store.queue_pending_delivery(record)
    pending = store.list_pending_deliveries()
    assert any(p.stem == rid for p in pending)
    store.remove_pending_delivery(rid)
    pending = store.list_pending_deliveries()
    assert all(p.stem != rid for p in pending)


def test_list_request_ids_returns_sorted(state_root: Path) -> None:
    """``list_request_ids`` returns ids in deterministic order."""
    store = UpdaterStore(state_root=state_root)
    for _ in range(5):
        store.create_request(_make_request())
    listed = store.list_request_ids()
    assert listed == sorted(listed)


def test_non_terminal_requests_filters_terminal(state_root: Path) -> None:
    """Terminal-result requests do not appear in ``non_terminal_requests``."""
    store = UpdaterStore(state_root=state_root)
    pending_record = _make_request()
    store.create_request(pending_record)
    done_record = _make_request()
    store.create_request(done_record)
    store.write_result(
        ResultRecord.rejection(request_id=done_record.request_id, target_sha="0" * 40)
    )
    pending = store.non_terminal_requests()
    assert pending_record.request_id in pending
    assert done_record.request_id not in pending


def test_clear_checkpoint_removes_running_file(state_root: Path) -> None:
    """``clear_checkpoint`` removes the running file idempotently."""
    store = UpdaterStore(state_root=state_root)
    rid = new_request_id()
    store.create_request(_make_request())
    store.write_checkpoint(rid, UpdatePhase.BUILDING)
    store.clear_checkpoint(rid)
    assert store.load_checkpoint(rid) is None
    # Idempotent: clearing again does not raise.
    store.clear_checkpoint(rid)
