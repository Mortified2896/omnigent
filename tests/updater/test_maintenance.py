"""Maintenance mode and drain tests (issue #38 §4)."""

from __future__ import annotations

from pathlib import Path

from omnigent.updater import layout, maintenance


def test_marker_defaults_to_inactive(state_root: Path) -> None:
    """A missing marker reads as inactive."""
    state = maintenance.read_marker()
    assert state.active is False
    assert state.request_id == ""


def test_write_marker_persists_state(state_root: Path) -> None:
    """``write_marker`` atomically persists the maintenance state."""
    state = maintenance.MaintenanceState(
        active=True, request_id="AAAAAAAAAAAAAAAAAAAAAAAAAA", set_at="2026-01-01T00:00:00Z"
    )
    maintenance.write_marker(state)
    loaded = maintenance.read_marker()
    assert loaded == state


def test_write_marker_creates_parent_directory(state_root: Path) -> None:
    """The marker write tolerates a missing state root (parent is created)."""
    state = maintenance.MaintenanceState(
        active=True, request_id="BBBBBBBBBBBBBBBBBBBBBBBBBB", set_at="2026-01-01T00:00:00Z"
    )
    maintenance.write_marker(state)
    assert layout.maintenance_marker_path().is_file()


def test_clear_marker_is_idempotent(state_root: Path) -> None:
    """``clear_marker`` is a no-op when the marker is absent."""
    maintenance.clear_marker()
    maintenance.clear_marker()
    assert not layout.maintenance_marker_path().exists()


def test_engage_maintenance_writes_active_marker(state_root: Path) -> None:
    """``engage_maintenance`` writes an active marker."""
    state = maintenance.engage_maintenance(
        request_id="CCCCCCCCCCCCCCCCCCCCCCCCCC",
        reason="update",
    )
    assert state.active is True
    assert state.request_id == "CCCCCCCCCCCCCCCCCCCCCCCCCC"
    loaded = maintenance.read_marker()
    assert loaded.active is True


def test_disengage_maintenance_clears_marker(state_root: Path) -> None:
    """``disengage_maintenance`` removes the marker."""
    maintenance.engage_maintenance(request_id="DDDDDDDDDDDDDDDDDDDDDDDDDD")
    maintenance.disengage_maintenance()
    loaded = maintenance.read_marker()
    assert loaded.active is False


def test_reconcile_marker_keeps_active_for_live_owner(state_root: Path) -> None:
    """An active marker stays when the owner pid is alive."""
    import os

    maintenance.engage_maintenance(request_id="EEEEEEEEEEEEEEEEEEEEEEEEEE")
    state = maintenance.reconcile_marker(owner_pid=os.getpid())
    assert state.active is True


def test_reconcile_marker_clears_for_dead_owner(state_root: Path) -> None:
    """An active marker is cleared when the owner pid is dead."""
    maintenance.engage_maintenance(request_id="FFFFFFFFFFFFFFFFFFFFFFFFFFFF")
    state = maintenance.reconcile_marker(owner_pid=999_999)
    assert state.active is False


def test_reconcile_marker_no_owner_leaves_state_alone(state_root: Path) -> None:
    """An unknown owner pid leaves the marker untouched (operator decides)."""
    maintenance.engage_maintenance(request_id="GGGGGGGGGGGGGGGGGGGGGGGGGG")
    state = maintenance.reconcile_marker(owner_pid=None)
    assert state.active is True


def test_drain_status_parses_response() -> None:
    """``DrainStatus.from_dict`` parses the documented response shape."""
    status = maintenance.DrainStatus.from_dict(
        {"draining": True, "active_sessions": ["s1", "s2"], "active_runners": []}
    )
    assert status.draining is True
    assert status.active_sessions == ["s1", "s2"]
    assert status.is_idle is False


def test_drain_status_idle_requires_empty_active_lists() -> None:
    """``is_idle`` requires both lists to be empty."""
    assert maintenance.DrainStatus(draining=True, active_sessions=[], active_runners=[]).is_idle
    assert not maintenance.DrainStatus(
        draining=True, active_sessions=["s"], active_runners=[]
    ).is_idle
    assert not maintenance.DrainStatus(
        draining=True, active_sessions=[], active_runners=["r"]
    ).is_idle
    assert not maintenance.DrainStatus(draining=False).is_idle
