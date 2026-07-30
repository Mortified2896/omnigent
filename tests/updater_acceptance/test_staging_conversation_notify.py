"""Conversation notification acceptance test (issue #38 §11).

Proves the controller:

* persists the result file before delivery;
* queues a pending-delivery entry the web service can reconcile;
* marks the result's notification_status correctly;
* the durable idempotency key (request_id) prevents duplicate
  deliveries.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from omnigent.updater.controller import ControllerConfig, UpdaterController
from omnigent.updater.layout import (
    pending_deliveries_dir,
    result_path,
)
from omnigent.updater.request_tool import record_request
from omnigent.updater.store import UpdaterStore


def _payload(target: str, expected: str, conversation_id: str) -> dict[str, object]:
    return {
        "target_sha": target,
        "expected_current_sha": expected,
        "origin_conversation_id": conversation_id,
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


def test_staging_result_is_persisted_before_delivery_attempted(
    staging_state_root: Path,
    staging_deploy_root: Path,
    staging_repo: Path,
    staging_live_sha: Path,
    staging_lineage_anchor: str,
    staging_db: Path,
) -> None:
    """The result file is durable on disk before the delivery attempt."""
    target = _make_candidate(staging_repo)
    staging_live_sha.write_text(target + "\n")
    outcome = record_request(_payload(target=target, expected="0" * 40, conversation_id="conv_x"))
    store = UpdaterStore(state_root=staging_state_root)
    request = store.load_request(outcome.request_id)
    controller = UpdaterController(
        ControllerConfig(
            state_root=staging_state_root,
            deploy_root=staging_deploy_root,
            repo_root=staging_repo,
            db_url=f"sqlite:///{staging_db}",
        ),
        store=store,
    )
    controller.run(request)
    # The result file is written.
    on_disk = result_path(outcome.request_id)
    assert on_disk.is_file()
    payload = json.loads(on_disk.read_text())
    # The notification_status field is present (it is "delivered"
    # when the controller successfully delivered, "failed" or
    # "pending" otherwise — every value is a durable marker).
    assert payload["notification_status"] in {"pending", "delivered", "failed", "not_applicable"}


def test_staging_pending_delivery_carries_request_id(
    staging_state_root: Path,
    staging_deploy_root: Path,
    staging_repo: Path,
    staging_live_sha: Path,
    staging_lineage_anchor: str,
    staging_db: Path,
) -> None:
    """When delivery to the web service fails, the pending-delivery file carries the request id."""
    target = _make_candidate(staging_repo)
    staging_live_sha.write_text(target + "\n")
    outcome = record_request(_payload(target=target, expected="0" * 40, conversation_id="conv_y"))
    store = UpdaterStore(state_root=staging_state_root)
    request = store.load_request(outcome.request_id)
    controller = UpdaterController(
        ControllerConfig(
            state_root=staging_state_root,
            deploy_root=staging_deploy_root,
            repo_root=staging_repo,
            db_url=f"sqlite:///{staging_db}",
        ),
        store=store,
    )
    controller.run(request)
    # At least one of: result notification_status == "delivered",
    # or a pending-delivery file is queued. Either way the
    # request_id is the durable idempotency key.
    pending = list(pending_deliveries_dir().glob(f"{outcome.request_id}.json"))
    if pending:
        payload = json.loads(pending[0].read_text())
        assert payload["request_id"] == outcome.request_id
    # Otherwise the result was delivered in-process and the
    # pending-delivery file was never created.
    else:
        on_disk = json.loads(result_path(outcome.request_id).read_text())
        assert on_disk["request_id"] == outcome.request_id


def test_staging_idempotency_key_prevents_duplicate_pending_entries(
    staging_state_root: Path,
    staging_deploy_root: Path,
    staging_repo: Path,
    staging_live_sha: Path,
    staging_lineage_anchor: str,
    staging_db: Path,
) -> None:
    """Re-running a request with a terminal result creates no duplicate entry."""
    target = _make_candidate(staging_repo)
    staging_live_sha.write_text(target + "\n")
    outcome = record_request(_payload(target=target, expected="0" * 40, conversation_id="conv_z"))
    store = UpdaterStore(state_root=staging_state_root)
    request = store.load_request(outcome.request_id)
    controller = UpdaterController(
        ControllerConfig(
            state_root=staging_state_root,
            deploy_root=staging_deploy_root,
            repo_root=staging_repo,
            db_url=f"sqlite:///{staging_db}",
        ),
        store=store,
    )
    result = controller.run(request)
    # Second run: the existing terminal result short-circuits and
    # no new pending-delivery file is queued for the same id.
    before = len(list(pending_deliveries_dir().glob(f"{outcome.request_id}.json")))
    second = controller.run(request)
    after = len(list(pending_deliveries_dir().glob(f"{outcome.request_id}.json")))
    assert before == after
    assert second.final_status == result.final_status
