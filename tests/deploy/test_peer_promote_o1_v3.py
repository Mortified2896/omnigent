"""Regression tests for the generalized peer promotion v3 contract."""

from __future__ import annotations

import ast
from pathlib import Path

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
    assert body.index("transaction.cross_mutation_boundary") < body.index("_stop_target")


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
