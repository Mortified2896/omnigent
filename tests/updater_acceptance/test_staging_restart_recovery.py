"""Updater-restart survival acceptance test (issue #38 §11).

Proves the controller can resume a request that crashed mid-update
by reading the durable checkpoint + result file and never blindly
repeating promotion.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from omnigent.updater.controller import (
    ControllerConfig,
    UpdaterController,
)
from omnigent.updater.layout import (
    running_path,
)
from omnigent.updater.request_tool import record_request
from omnigent.updater.state_machine import UpdatePhase
from omnigent.updater.store import UpdaterStore


def _payload(target: str, expected: str) -> dict[str, object]:
    return {
        "target_sha": target,
        "expected_current_sha": expected,
        "requested_by": "operator:staging",
        "authorization": {"kind": "operator", "operator": "staging"},
    }


def _make_candidate(repo: Path) -> str:
    (repo / "README").write_text("candidate\n")
    subprocess.run(["git", "-C", str(repo), "add", "README"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "candidate", "-q"], check=True)
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def test_staging_restart_recovery_after_validation_resume(
    staging_state_root: Path,
    staging_deploy_root: Path,
    staging_repo: Path,
    staging_live_sha: Path,
    staging_lineage_anchor: str,
    staging_db: Path,
) -> None:
    """A checkpoint in ``validating`` is resumed by the controller on restart."""
    target = _make_candidate(staging_repo)
    staging_live_sha.write_text(target + "\n")
    outcome = record_request(_payload(target=target, expected="0" * 40))
    store = UpdaterStore(state_root=staging_state_root)
    # Simulate a crash: a checkpoint exists but no result file.
    store.write_checkpoint(outcome.request_id, UpdatePhase.VALIDATING)
    assert running_path(outcome.request_id).is_file()

    controller = UpdaterController(
        ControllerConfig(
            state_root=staging_state_root,
            deploy_root=staging_deploy_root,
            repo_root=staging_repo,
            db_url=f"sqlite:///{staging_db}",
        ),
        store=store,
    )
    decisions = controller.recover_non_terminal()
    assert any(d.request_id == outcome.request_id for d in decisions)
    decision = next(d for d in decisions if d.request_id == outcome.request_id)
    assert decision.action in {"resume", "resume_idle"}


def test_staging_restart_recovery_after_promoting_triggers_verify(
    staging_state_root: Path,
    staging_deploy_root: Path,
    staging_repo: Path,
    staging_live_sha: Path,
    staging_lineage_anchor: str,
    staging_db: Path,
) -> None:
    """A checkpoint in ``promoting`` causes a verify (no blind re-promotion)."""
    target = _make_candidate(staging_repo)
    staging_live_sha.write_text(target + "\n")
    outcome = record_request(_payload(target=target, expected="0" * 40))
    store = UpdaterStore(state_root=staging_state_root)
    store.write_checkpoint(outcome.request_id, UpdatePhase.PROMOTING)
    controller = UpdaterController(
        ControllerConfig(
            state_root=staging_state_root,
            deploy_root=staging_deploy_root,
            repo_root=staging_repo,
            db_url=f"sqlite:///{staging_db}",
        ),
        store=store,
    )
    decisions = controller.recover_non_terminal()
    assert any(d.request_id == outcome.request_id and d.action == "verify" for d in decisions)


def test_staging_restart_recovery_after_rollback_in_progress(
    staging_state_root: Path,
    staging_deploy_root: Path,
    staging_repo: Path,
    staging_live_sha: Path,
    staging_lineage_anchor: str,
    staging_db: Path,
) -> None:
    """A checkpoint in ``rolling_back`` requires operator-driven recording."""
    target = _make_candidate(staging_repo)
    staging_live_sha.write_text(target + "\n")
    outcome = record_request(_payload(target=target, expected="0" * 40))
    store = UpdaterStore(state_root=staging_state_root)
    store.write_checkpoint(outcome.request_id, UpdatePhase.ROLLING_BACK)
    controller = UpdaterController(
        ControllerConfig(
            state_root=staging_state_root,
            deploy_root=staging_deploy_root,
            repo_root=staging_repo,
            db_url=f"sqlite:///{staging_db}",
        ),
        store=store,
    )
    decisions = controller.recover_non_terminal()
    assert any(
        d.request_id == outcome.request_id and d.action == "record_rollback" for d in decisions
    )
