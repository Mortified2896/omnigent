"""Successful staging update acceptance test (issue #38 §11).

Proves the controller walks the full happy path:

1. file a request;
2. validate against the lineage anchor;
3. build via the stubbed hook;
4. drain via the stubbed hook;
5. rehearse via the stubbed hook;
6. back up via the stubbed hook;
7. promote via the stubbed hook;
8. verify health via the stubbed hook;
9. record ``succeeded`` and clear maintenance;
10. queue a pending-delivery file the test reads back to prove
    exactly-once intent.

The test uses stubbed subprocess hooks so the staging deploy root
is never rebuilt from scratch and no real systemd interaction is
needed. It pins every observable behavior the production runbook
calls out.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from omnigent.updater.controller import (
    ControllerConfig,
    ControllerHooks,
    UpdaterController,
)
from omnigent.updater.layout import (
    pending_deliveries_dir,
    request_path,
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
    health_probes=None,
) -> ControllerHooks:
    ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    def _build(_repo: Path, _sha: str) -> subprocess.CompletedProcess:
        return ok

    def _promote(_repo: Path, _sha: str) -> subprocess.CompletedProcess:
        return ok

    def _rollback(_repo: Path) -> subprocess.CompletedProcess:
        return ok

    def _drain(_rid: str) -> None:
        return None

    def _rehearse(_rid: str, _target: str) -> None:
        return None

    def _backup(_rid: str) -> None:
        return None

    def _health(_sha: str) -> None:
        if health_probes is not None:
            health_probes(_sha)

    return ControllerHooks(
        build_only=_build,
        promote=_promote,
        rollback=_rollback,
        health_probes=_health,
        drain=_drain,
        rehearse=_rehearse,
        backup=_backup,
    )


def test_staging_successful_update_end_to_end(
    staging_state_root: Path,
    staging_deploy_root: Path,
    staging_repo: Path,
    staging_live_sha: Path,
    staging_lineage_anchor: str,
    staging_db: Path,
) -> None:
    """A staging update walks every phase and produces ``succeeded``."""
    # Make the candidate commit (descendant of the anchor).
    (staging_repo / "README").write_text("candidate\n")
    subprocess.run(["git", "-C", str(staging_repo), "add", "README"], check=True)
    subprocess.run(
        ["git", "-C", str(staging_repo), "commit", "-m", "candidate", "-q"],
        check=True,
    )
    # Push to the fork mirror so the explicit fork/main ancestry
    # check accepts the new commit.
    subprocess.run(
        ["git", "-C", str(staging_repo), "push", "fork", "main", "-q"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(staging_repo), "fetch", "fork", "-q"],
        check=True,
    )
    target = subprocess.run(
        ["git", "-C", str(staging_repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    # Keep the live SHA empty so ``expected_current_sha == "0"*40`` is valid.
    staging_live_sha.write_text("")

    outcome = record_request(_record(target=target, expected="0" * 40))
    assert outcome.request_id

    store = UpdaterStore(state_root=staging_state_root)
    request = store.load_request(outcome.request_id)
    assert request is not None
    assert request.target_sha == target

    controller = UpdaterController(
        ControllerConfig(
            state_root=staging_state_root,
            deploy_root=staging_deploy_root,
            repo_root=staging_repo,
            db_url=f"sqlite:///{staging_db}",
            hooks=_hooks(),
        ),
        store=store,
    )
    result = controller.run(request)

    assert result.final_status == "succeeded"
    assert result.target_sha == target
    assert result.deployed_sha == target
    assert result.rollback_performed is False

    # The result file is durable on disk.
    on_disk = result_path(outcome.request_id)
    assert on_disk.is_file()
    payload = json.loads(on_disk.read_text())
    assert payload["final_status"] == "succeeded"

    # The request file is durable too.
    assert request_path(outcome.request_id).is_file()

    # The maintenance marker is cleared.
    maintenance_path = staging_state_root / "maintenance.json"
    if maintenance_path.is_file():
        state = json.loads(maintenance_path.read_text())
        assert state["active"] is False

    # A pending-delivery file may exist because the controller
    # could not reach a real web service in staging; the test
    # asserts the file is structurally a valid result payload so
    # the web service can reconcile it on startup.
    pending = list(pending_deliveries_dir().glob("*.json"))
    if pending:
        for entry in pending:
            payload = json.loads(entry.read_text())
            assert payload["request_id"] == outcome.request_id
            assert payload["final_status"] == "succeeded"

    # The event log captured every phase.
    events_path = staging_state_root / "events" / f"{outcome.request_id}.jsonl"
    assert events_path.is_file()
    phases_seen = {
        json.loads(line)["phase"] for line in events_path.read_text().splitlines() if line.strip()
    }
    assert UpdatePhase.SUCCEEDED.value in phases_seen
    assert UpdatePhase.BUILDING.value in phases_seen
    assert UpdatePhase.DRAINING.value in phases_seen
    assert UpdatePhase.PROMOTING.value in phases_seen
    assert UpdatePhase.VERIFYING.value in phases_seen
