"""Validation rejection acceptance tests (issue #38 §11).

Proves every validation rule rejects the request **before** any
build is started, so a malformed / stale / out-of-lineage request
never reaches the deployment pipeline.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from omnigent.updater.controller import ControllerConfig, UpdaterController
from omnigent.updater.layout import result_path
from omnigent.updater.request_tool import (
    RequestInterfaceError,
    record_request,
)
from omnigent.updater.store import UpdaterStore


def _ok_payload(target: str, expected: str = "0" * 40) -> dict[str, object]:
    return {
        "target_sha": target,
        "expected_current_sha": expected,
        "requested_by": "operator:staging",
        "authorization": {"kind": "operator", "operator": "staging"},
    }


def test_staging_stale_expected_current_is_rejected(
    staging_state_root: Path,
    staging_deploy_root: Path,
    staging_repo: Path,
    staging_live_sha: Path,
    staging_lineage_anchor: str,
    staging_db: Path,
) -> None:
    """A request whose ``expected_current_sha`` does not match the live SHA is rejected."""
    # Live SHA is set to a real value the operator did not anticipate.
    stale_live = "0" * 38 + "ab"
    staging_live_sha.write_text(stale_live + "\n")
    # Build a real commit so the target-format check passes.
    (staging_repo / "README").write_text("v\n")
    subprocess.run(["git", "-C", str(staging_repo), "add", "README"], check=True)
    subprocess.run(["git", "-C", str(staging_repo), "commit", "-m", "v", "-q"], check=True)
    target = subprocess.run(
        ["git", "-C", str(staging_repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    outcome = record_request(_ok_payload(target=target, expected="0" * 40))
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
    assert result.final_status == "rejected"
    assert "live" in result.failure_reason.lower() or "stale" in result.failure_reason.lower()


def test_staging_malformed_sha_is_rejected_by_request_interface(
    staging_state_root: Path,
) -> None:
    """The request interface rejects a malformed ``target_sha`` at submission time."""
    payload = _ok_payload(target="not-a-sha")
    with pytest.raises(RequestInterfaceError):
        record_request(payload)
    # No request file was written.
    assert list(staging_state_root.glob("requests/*.json")) == []


def test_staging_unknown_target_is_rejected(
    staging_state_root: Path,
    staging_deploy_root: Path,
    staging_repo: Path,
    staging_live_sha: Path,
    staging_lineage_anchor: str,
    staging_db: Path,
) -> None:
    """A target SHA that does not exist on the staging fork is rejected."""
    staging_live_sha.write_text("0" * 40 + "\n")
    fake_target = "1" * 40  # well-formed but unknown to the repo
    outcome = record_request(_ok_payload(target=fake_target, expected="0" * 40))
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
    assert result.final_status == "rejected"


def test_staging_out_of_lineage_target_is_rejected(
    staging_state_root: Path,
    staging_deploy_root: Path,
    staging_repo: Path,
    staging_live_sha: Path,
    staging_lineage_anchor: str,
    staging_db: Path,
    tmp_path: Path,
) -> None:
    """A target outside the approved lineage anchor is rejected before any build."""
    staging_live_sha.write_text("0" * 40 + "\n")
    # Build a separate, unrelated repo where a commit is created
    # that is not a descendant of the lineage anchor.
    other = tmp_path / "staging_other"
    other.mkdir()
    subprocess.run(
        ["git", "-C", str(other), "init", "--initial-branch=main", "-q"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(other), "config", "user.email", "t@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(other), "config", "user.name", "T"],
        check=True,
    )
    (other / "X").write_text("x")
    subprocess.run(["git", "-C", str(other), "add", "X"], check=True)
    subprocess.run(["git", "-C", str(other), "commit", "-m", "out", "-q"], check=True)
    orphan_sha = subprocess.run(
        ["git", "-C", str(other), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    # Add the unrelated repo as a remote so the orphan is reachable
    # but still outside the lineage anchor's ancestry.
    subprocess.run(
        ["git", "-C", str(staging_repo), "remote", "add", "staging_other", str(other)],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(staging_repo), "fetch", "staging_other", "-q"],
        check=True,
    )
    outcome = record_request(_ok_payload(target=orphan_sha, expected="0" * 40))
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
    assert result.final_status == "rejected"
    payload = json.loads(result_path(outcome.request_id).read_text())
    assert payload["failure_reason"]
