"""OpenCode bridge-state persistence tests.

Regression coverage for the second-prompt failure mode: an OpenCode
session must accept multiple consecutive prompts without the runner
exiting, the bridge state being lost, or the runner becoming
unavailable. The state file is the runner's only contract with the
harness executor (which reads it on every web-injected turn), so
round-tripping each field — including ``permission_mode`` — across
turn boundaries is the property under test.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from omnigent.opencode_native_bridge import (
    OpenCodeNativeBridgeState,
    read_bridge_state,
    write_bridge_state,
)


def test_bridge_state_round_trips_permission_mode(tmp_path: Path) -> None:
    """The runner-owned state file carries the user's permission_mode pick.

    The first prompt writes ``permission_mode='auto'``; a second prompt
    must see the same value come back from disk. Otherwise the runner
    would silently drop the mode on the next turn.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    state = OpenCodeNativeBridgeState(
        session_id="conv_1",
        server_base_url="http://127.0.0.1:49231",
        opencode_session_id="ses_abc",
        auth_secret="secret",
        xdg_data_home=str(bridge_dir / "xdg-data"),
        xdg_config_home=str(bridge_dir / "xdg-config"),
        model_override="anthropic/claude-opus-4",
        reasoning_effort="high",
        permission_mode="auto",
        workspace="/tmp/repo",
    )
    write_bridge_state(bridge_dir, state)

    # Re-read on what would be the second-prompt path. The executor reads
    # this file fresh every turn, so a regression here manifests as the
    # second prompt losing its mode.
    again = read_bridge_state(bridge_dir)
    assert again is not None
    assert again.permission_mode == "auto"
    assert again.model_override == "anthropic/claude-opus-4"
    assert again.reasoning_effort == "high"


@pytest.mark.parametrize(
    "mode",
    ["default", "auto", "accept_edits", "plan", "dont_ask", "bypass"],
)
def test_bridge_state_round_trips_every_permission_mode(
    tmp_path: Path,
    mode: str,
) -> None:
    """Every documented mode must survive the bridge-state round trip."""
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    state = OpenCodeNativeBridgeState(
        session_id="conv_1",
        server_base_url="http://127.0.0.1:49231",
        opencode_session_id="ses_abc",
        permission_mode=mode,
    )
    write_bridge_state(bridge_dir, state)
    again = read_bridge_state(bridge_dir)
    assert again is not None
    assert again.permission_mode == mode


def test_bridge_state_handles_missing_permission_mode(tmp_path: Path) -> None:
    """An older session (no permission_mode field) must round-trip cleanly."""
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    state = OpenCodeNativeBridgeState(
        session_id="conv_1",
        server_base_url="http://127.0.0.1:49231",
        opencode_session_id="ses_abc",
        # no permission_mode — default ``None``
    )
    write_bridge_state(bridge_dir, state)
    again = read_bridge_state(bridge_dir)
    assert again is not None
    assert again.permission_mode is None


def test_bridge_state_survives_partial_corruption(tmp_path: Path) -> None:
    """A half-written state file must NOT crash the runner (returns None)."""
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    state_path = bridge_dir / "state.json"
    state_path.write_text('{"version": 1, "session_id":', encoding="utf-8")
    # The executor relies on ``read_bridge_state`` returning ``None`` (not
    # raising) so it can detect the absent-state path and let the runner
    # boot the server fresh — corruption is non-fatal, never a 500.
    assert read_bridge_state(bridge_dir) is None


def test_bridge_state_replace_keeps_all_other_fields(tmp_path: Path) -> None:
    """Updating one field via ``dataclasses.replace`` must not drop the mode."""
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    initial = OpenCodeNativeBridgeState(
        session_id="conv_1",
        server_base_url="http://127.0.0.1:49231",
        opencode_session_id="ses_abc",
        permission_mode="plan",
    )
    write_bridge_state(bridge_dir, initial)

    # Simulate a second-turn path that updates ``active_message_id`` — the
    # permission_mode must round-trip, since it's the runner-owned config,
    # not a per-turn ephemeral.
    again = read_bridge_state(bridge_dir)
    assert again is not None
    replaced = dataclasses.replace(again, active_message_id="msg_1", status="busy")
    write_bridge_state(bridge_dir, replaced)

    third = read_bridge_state(bridge_dir)
    assert third is not None
    assert third.permission_mode == "plan"
    assert third.active_message_id == "msg_1"
    assert third.status == "busy"
