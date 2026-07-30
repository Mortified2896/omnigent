"""Single-active-update locking tests (issue #38 §1, §9)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from omnigent.updater import layout
from omnigent.updater.locking import (
    LockHeldError,
    LockHolder,
    UpdateLock,
    global_lock,
    make_holder,
)


def test_global_lock_blocks_second_acquirer(state_root: Path) -> None:
    """A second ``global_lock`` acquisition raises ``LockHeldError``."""
    a = UpdateLock.global_lock()
    a.acquire()
    try:
        b = UpdateLock.global_lock()
        with pytest.raises(LockHeldError):
            b.acquire()
    finally:
        a.release()


def test_release_allows_next_acquirer(state_root: Path) -> None:
    """Releasing the global lock lets a second acquirer succeed."""
    a = UpdateLock.global_lock()
    a.acquire()
    a.release()
    b = UpdateLock.global_lock()
    b.acquire()
    try:
        assert b.path.is_file()
    finally:
        b.release()


def test_global_lock_context_manager_releases_on_exit(state_root: Path) -> None:
    """The ``global_lock`` context manager releases on exit, even on exception."""
    with pytest.raises(RuntimeError):
        with global_lock():
            assert (layout.locks_dir() / "__global__.lock").is_file()
            raise RuntimeError("boom")
    # The lock file was released.
    assert not (layout.locks_dir() / "__global__.lock").is_file()


def test_per_request_locks_are_independent(state_root: Path) -> None:
    """Two request ids can hold their per-request locks simultaneously."""
    a = UpdateLock.for_request("AAAAAAAAAAAAAAAAAAAAAAAAAA")
    b = UpdateLock.for_request("BBBBBBBBBBBBBBBBBBBBBBBBBB")
    a.acquire()
    try:
        b.acquire()
        try:
            assert a.path.is_file()
            assert b.path.is_file()
        finally:
            b.release()
    finally:
        a.release()


def test_stale_lock_sweep_removes_dead_holder(state_root: Path) -> None:
    """Locks held by a dead pid are swept on the next acquire."""
    a = UpdateLock.global_lock()
    a.acquire()
    # Simulate a dead holder by rewriting the lock file with a pid
    # that we know is not alive.
    fake_holder = LockHolder(pid=999_999, hostname="ghost", started_at="2000-01-01T00:00:00Z")
    import json

    a.path.write_text(json.dumps(fake_holder.to_dict(), indent=2, sort_keys=True) + "\n")
    # Re-acquire should sweep and succeed.
    b = UpdateLock.global_lock()
    b.acquire()
    try:
        holder = LockHolder.from_dict(json.loads(a.path.read_text()))
        assert holder.pid == os.getpid()
    finally:
        b.release()


def test_make_holder_records_current_pid() -> None:
    """``make_holder`` records the calling pid so the lock is observable."""
    holder = make_holder(note="test")
    assert holder.pid == os.getpid()
    assert holder.note == "test"


def test_lock_path_uses_request_id(state_root: Path) -> None:
    """Per-request lock paths include the request id."""
    lock = UpdateLock.for_request("ZZZZZZZZZZZZZZZZZZZZZZZZ")
    assert "ZZZZZZZZZZZZZZZZZZZZZZZZ" in lock.path.name
