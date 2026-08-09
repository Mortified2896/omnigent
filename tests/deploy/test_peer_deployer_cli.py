"""Tests for the peer-deployer CLI entrypoint.

These tests prove the CLI exposes the required knobs, refuses
self-upgrade, and exercises the preflight + transaction + rollback
plumbing end-to-end.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PKG_ROOT = REPO_ROOT / "deploy" / "scripts" / "peer_deployer"
SCRIPTS_DIR = REPO_ROOT / "deploy" / "scripts"


def _load_pkg():
    spec = importlib.util.spec_from_file_location(
        "peer_deployer_main", PKG_ROOT / "__main__.py",
        submodule_search_locations=[str(PKG_ROOT)],
    )
    assert spec is not None
    pkg = importlib.util.module_from_spec(spec)
    sys.modules["peer_deployer_main"] = pkg
    spec.loader.exec_module(pkg)
    return pkg


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    """Run the peer_deployer CLI as a subprocess."""
    return subprocess.run(
        [sys.executable, "-m", "peer_deployer", *args],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env={**os.environ, "PYTHONPATH": str(SCRIPTS_DIR)},
    )


def test_cli_preflight_fails_for_target_equal_supervisor() -> None:
    proc = _run_cli("preflight", "--target", "O1", "--supervisor", "O1")
    assert proc.returncode != 0
    # The error message must contain the refusal.
    assert "target == supervisor" in proc.stderr


def test_cli_preflight_runs_on_real_host() -> None:
    proc = _run_cli("preflight", "--target", "O1", "--supervisor", "O2")
    # On the well-configured host, the preflight either passes (O1
    # healthy, O2 verified, artifact hashes match) or fails closed
    # with a structured report. Either way, the output is JSON.
    blob = json.loads(proc.stdout)
    assert blob["target"] == "O1"
    assert blob["supervisor"] == "O2"
    assert "checks" in blob
    assert isinstance(blob["checks"], list)
    # The CLI exit code is 0 iff passed, 1 otherwise.
    assert proc.returncode == (0 if blob["passed"] else 1)


def test_cli_list_returns_empty_default() -> None:
    proc = _run_cli("list")
    # list succeeds regardless of state.
    assert proc.returncode == 0
    blob = json.loads(proc.stdout)
    assert "transactions" in blob
    assert "root" in blob


def test_cli_load_missing_tx_fails() -> None:
    proc = _run_cli("load", "--tx-id", "promotion-20260808T000000Z-00000000")
    assert proc.returncode != 0


def test_cli_rollback_requires_target_and_supervisor() -> None:
    proc = _run_cli(
        "rollback",
        "--tx-id", "promotion-20260808T000000Z-00000000",
        "--target", "O1",
        "--supervisor", "O1",  # self-upgrade refused
    )
    assert proc.returncode != 0
    assert "target == supervisor" in proc.stderr


def test_cli_complete_requires_existing_tx() -> None:
    proc = _run_cli("complete", "--tx-id", "promotion-20260808T000000Z-00000000")
    assert proc.returncode != 0


def test_cli_unknown_subcommand_fails() -> None:
    proc = _run_cli("nonexistent")
    assert proc.returncode != 0


def test_pythonpath_is_local() -> None:
    """The package is importable from the repo root."""
    importlib.invalidate_caches()
    pkg = _load_pkg()
    assert hasattr(pkg, "main")


def test_pkg_exposes_subcommands() -> None:
    """The CLI exposes promote, rollback, load, complete, list, preflight."""
    pkg = _load_pkg()
    parser = pkg._parser()
    # Inspect the subparsers.
    sub_actions = [
        action for action in parser._actions
        if action.__class__.__name__ == "_SubParsersAction"
    ]
    assert sub_actions
    choices = sub_actions[0].choices
    for cmd in ["promote", "rollback", "load", "complete", "list", "preflight"]:
        assert cmd in choices
