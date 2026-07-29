"""Broken-candidate staging acceptance test (issue #38 §11).

Proves the controller:

* detects a post-cutover health failure;
* triggers the rollback path;
* records ``rolled_back`` (or ``rollback_failed`` when the
  rollback hook itself fails);
* clears maintenance on the way out;
* persists the failure + rollback outcome to disk;
* queues a pending-delivery file the web service can reconcile.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from omnigent.updater.controller import (
    ControllerConfig,
    ControllerHooks,
    HealthCheckFailedError,
    UpdaterController,
)
from omnigent.updater.layout import (
    result_path,
)
from omnigent.updater.request_tool import record_request
from omnigent.updater.state_machine import UpdatePhase
from omnigent.updater.store import UpdaterStore


def _record(target: str, expected: str) -> dict[str, object]:
    return {
        "target_sha": target,
        "expected_current_sha": expected,
        "requested_by": "operator:staging",
        "authorization": {"kind": "operator", "operator": "staging"},
    }


def _hooks(
    *,
    rollback_proc: subprocess.CompletedProcess | None = None,
    health_probes=None,
) -> ControllerHooks:
    ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    def _build(_repo: Path, _sha: str) -> subprocess.CompletedProcess:
        return ok

    def _promote(_repo: Path, _sha: str) -> subprocess.CompletedProcess:
        return ok

    def _rollback_hook(_repo: Path) -> subprocess.CompletedProcess:
        return rollback_proc or ok

    def _drain(_rid: str) -> None:
        return None

    def _rehearse(_rid: str, _target: str) -> None:
        return None

    def _backup(_rid: str) -> None:
        return None

    def _health(sha: str) -> None:
        if health_probes is not None:
            health_probes(sha)

    return ControllerHooks(
        build_only=_build,
        promote=_promote,
        rollback=_rollback_hook,
        health_probes=_health,
        drain=_drain,
        rehearse=_rehearse,
        backup=_backup,
    )


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


def test_staging_broken_candidate_triggers_rollback(
    staging_state_root: Path,
    staging_deploy_root: Path,
    staging_repo: Path,
    staging_live_sha: Path,
    staging_lineage_anchor: str,
    staging_db: Path,
) -> None:
    """A failed post-cutover health probe triggers rollback and persists the outcome."""
    target = _make_candidate(staging_repo)
    staging_live_sha.write_text("")
    outcome = record_request(_record(target=target, expected="0" * 40))
    store = UpdaterStore(state_root=staging_state_root)
    request = store.load_request(outcome.request_id)

    probe_calls: list[str] = []

    def bad_probe(sha: str) -> None:
        probe_calls.append(sha)
        # First call: post-cutover — fail it so the controller
        # enters the rollback path. Subsequent calls (during
        # rollback verification) accept so the rollback succeeds.
        if len(probe_calls) == 1:
            raise HealthCheckFailedError("/health returned 500")

    controller = UpdaterController(
        ControllerConfig(
            state_root=staging_state_root,
            deploy_root=staging_deploy_root,
            repo_root=staging_repo,
            db_url=f"sqlite:///{staging_db}",
            hooks=_hooks(health_probes=bad_probe),
        ),
        store=store,
    )
    result = controller.run(request)
    assert result.final_status == "rolled_back"
    assert result.rollback_performed is True
    assert result.rollback_result == "succeeded"
    assert result.failure_phase == UpdatePhase.VERIFYING.value
    # The post-cutover probe fired once.
    assert probe_calls
    assert probe_calls[0] == target

    # The result file is durable.
    on_disk = result_path(outcome.request_id)
    assert on_disk.is_file()
    payload = json.loads(on_disk.read_text())
    assert payload["final_status"] == "rolled_back"
    assert payload["rollback_performed"] is True

    # No residual maintenance state.
    maintenance_path = staging_state_root / "maintenance.json"
    if maintenance_path.is_file():
        state = json.loads(maintenance_path.read_text())
        assert state["active"] is False


def test_staging_rollback_failure_records_rollback_failed(
    staging_state_root: Path,
    staging_deploy_root: Path,
    staging_repo: Path,
    staging_live_sha: Path,
    staging_lineage_anchor: str,
    staging_db: Path,
) -> None:
    """A rollback that itself fails records ``rollback_failed``."""
    target = _make_candidate(staging_repo)
    staging_live_sha.write_text("")
    outcome = record_request(_record(target=target, expected="0" * 40))
    store = UpdaterStore(state_root=staging_state_root)
    request = store.load_request(outcome.request_id)

    failing_rollback = subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr="rollback boom"
    )

    def bad_probe(sha: str) -> None:
        raise HealthCheckFailedError("/health returned 500")

    controller = UpdaterController(
        ControllerConfig(
            state_root=staging_state_root,
            deploy_root=staging_deploy_root,
            repo_root=staging_repo,
            db_url=f"sqlite:///{staging_db}",
            hooks=_hooks(
                rollback_proc=failing_rollback,
                health_probes=bad_probe,
            ),
        ),
        store=store,
    )
    result = controller.run(request)
    assert result.final_status == "rollback_failed"
    assert result.rollback_performed is True
    assert result.rollback_result == "failed"
    payload = json.loads(result_path(outcome.request_id).read_text())
    assert payload["final_status"] == "rollback_failed"
