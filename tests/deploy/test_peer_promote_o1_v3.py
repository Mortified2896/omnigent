"""Regression tests for the generalized peer promotion v3 contract."""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from deploy.scripts.peer_deployer import host_promotion, staging

REPO_ROOT = Path(__file__).resolve().parents[2]
HOST = REPO_ROOT / "deploy" / "scripts" / "peer_deployer" / "host_promotion.py"
WRAPPER = REPO_ROOT / "deploy" / "scripts" / "peer_promote_o1_v3.py"
V2 = REPO_ROOT / "deploy" / "scripts" / "peer_promote_o1_v2.sh"


def _src() -> str:
    return HOST.read_text()


def test_v3_requires_explicit_direction_mode_and_acceptance() -> None:
    wrapper = WRAPPER.read_text()
    for argument in (
        '"--target"',
        '"--supervisor"',
        '"--mode"',
        '"--acceptance-record"',
        '"--evidence-dir"',
    ):
        assert argument in wrapper
    source = _src()
    assert "TARGET = identity.O1" not in source
    assert "SUPERVISOR = identity.O2" not in source
    assert "identity.require_distinct(target, supervisor)" in source


def test_v2_live_promotion_path_is_hard_disabled() -> None:
    source = V2.read_text()
    assert "REFUSED: peer_promote_o1_v2.sh is permanently disabled" in source
    assert "exit 64" in source


def test_v3_uses_transaction_specific_legacy_runtime_not_glob() -> None:
    source = _src()
    assert 'f"venv.legacy-{tx_id}"' in source
    assert "venv.legacy-*" not in source
    assert "head -1" not in source


def test_v3_verifies_acceptance_before_mutation_boundary() -> None:
    tree = ast.parse(_src())
    run_fn = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "run"
    )
    body = ast.unparse(run_fn)
    assert body.index("_stage_or_reference") < body.index("transaction.cross_mutation_boundary")
    assert body.index("acceptance.load") < body.index("transaction.cross_mutation_boundary")
    assert body.index("_finalize_candidate") < body.index("transaction.cross_mutation_boundary")
    assert body.index("transaction.cross_mutation_boundary") < body.index("_stop_target")


def test_peer_candidate_is_finalized_before_active_switch() -> None:
    tree = ast.parse(_src())
    finalize_fn = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_finalize_candidate"
    )
    finalize_body = ast.unparse(finalize_fn)
    assert finalize_body.index("os.rename") < finalize_body.index("finalize_relocated_runtime")
    assert finalize_body.index("finalize_relocated_runtime") < finalize_body.index(
        "verify_release"
    )
    switch_fn = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_switch"
    )
    assert "os.rename(candidate, final)" not in ast.unparse(switch_fn)


def test_finalization_failure_leaves_active_runtime_untouched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staging_root = tmp_path / "staging" / "promotion-test"
    final_root = tmp_path / "releases" / ("a" * 40)
    active_runtime = tmp_path / "active-runtime"
    staging_root.mkdir(parents=True)
    final_root.parent.mkdir(parents=True)
    active_runtime.mkdir()
    current = tmp_path / "current"
    current.symlink_to(active_runtime)
    record = SimpleNamespace(new_runtime_path=str(final_root))
    accepted = SimpleNamespace(wheel_map=lambda root: {"main": root / "artifacts/main.whl"})
    monkeypatch.setattr(host_promotion.transaction, "register_owned", lambda *args: None)

    def fail_finalization(*args: object, **kwargs: object) -> None:
        raise staging.StagingError("invalid relocated launcher")

    monkeypatch.setattr(staging, "finalize_relocated_runtime", fail_finalization)
    with pytest.raises(staging.StagingError, match="invalid relocated launcher"):
        host_promotion._finalize_candidate(
            record,
            candidate=staging_root,
            candidate_preexisting=False,
            accepted=accepted,
        )

    assert current.resolve() == active_runtime
    assert final_root.is_dir()


def test_v3_catches_post_mutation_failures_and_pairs_rollback() -> None:
    source = _src()
    assert "except BaseException as exc:" in source
    assert "if not crossed:" in source
    rollback = source[source.index("def _rollback(") : source.index("def _signal(")]
    assert "_restore_runtime(" in rollback
    assert "_restore_db(" in rollback
    assert "record.rollback_completed = True" in rollback


def test_v3_preserves_referenced_preexisting_candidate() -> None:
    source = _src()
    assert "transaction.register_referenced(record, str(final))" in source
    failure = source[source.index("except BaseException as exc:") :]
    assert "transaction.is_owned(record, str(candidate))" in failure


def test_v3_rechecks_guard_and_supervisor_at_mutation_boundary() -> None:
    source = _src()
    boundary = source.index("transaction.cross_mutation_boundary(record)")
    guard = source.index("guard_at_boundary = storage_guard_probe()")
    supervisor = source.index("supervisor_at_boundary = baseline.capture(supervisor)")
    assert guard < boundary
    assert supervisor < boundary


def test_v3_records_and_compares_supervisor_baseline() -> None:
    source = _src()
    assert "baseline.capture(supervisor)" in source
    assert "supervisor_baseline=supervisor_before" in source
    assert "baseline.compare(supervisor_before, supervisor_at_boundary)" in source
    assert "supervisor drift before mutation" in source
    assert "baseline.compare(supervisor_before, supervisor_after)" in source


def test_v3_restricts_evidence_and_verifies_backup_before_rollback() -> None:
    source = _src()
    assert 'DEFAULT_EVIDENCE_ROOT = Path("/var/lib/omnigent-control-room/evidence")' in source
    assert "evidence directory must be beneath" in source
    rollback = source[source.index("def _rollback(") : source.index("def recover(")]
    assert rollback.index("_verify_rollback_backup(") < rollback.index("_stop_target(target)")
    assert "rollback already attempted" in rollback


def test_v3_recovery_restores_runtime_and_db_before_commit() -> None:
    source = _src()
    recovery = source[source.index("def recover(") : source.index("def _signal(")]
    assert recovery.index("_stop_target(target)") < recovery.index("_restore_runtime(")
    assert recovery.index("_restore_runtime(") < recovery.index("_restore_db(")
    assert recovery.index("_restore_db(") < recovery.index("_start_target(target)")
    assert "record.rollback_completed = True" in recovery


def test_v3_has_no_active_artifact_constants_or_force_skip() -> None:
    source = _src()
    assert "ACCEPTED_SHA" not in source
    assert "OLD_SHA" not in source
    assert "NEW_SCHEMA" not in source
    wrapper = WRAPPER.read_text()
    assert '"--force"' not in wrapper
    assert '"--skip"' not in wrapper


def test_v3_interrupts_enter_same_failure_path() -> None:
    source = _src()
    assert "signal.SIGINT" in source
    assert "signal.SIGTERM" in source
    assert "signal.SIGHUP" in source
    assert "raise PromotionInterrupted" in source
