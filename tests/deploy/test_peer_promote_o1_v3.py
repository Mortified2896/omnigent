"""Regression tests for the Control Room peer promotion v3 safety contract."""
from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HOST = REPO_ROOT / "deploy" / "scripts" / "peer_deployer" / "host_promotion.py"
WRAPPER = REPO_ROOT / "deploy" / "scripts" / "peer_promote_o1_v3.py"


def _src() -> str:
    return HOST.read_text()


def test_v3_direction_is_fixed_o2_to_o1() -> None:
    s = _src()
    assert "TARGET = identity.O1" in s
    assert "SUPERVISOR = identity.O2" in s
    assert "identity.require_distinct(TARGET, SUPERVISOR)" in s
    assert 'add_argument("--target"' not in WRAPPER.read_text()
    assert 'add_argument("--supervisor"' not in WRAPPER.read_text()


def test_v3_uses_transaction_specific_legacy_runtime_not_glob() -> None:
    s = _src()
    assert 'f"venv.legacy-{tx_id}"' in s
    assert "venv.legacy-*" not in s
    assert "glob(" not in s
    assert "head -1" not in s


def test_v3_stages_and_verifies_before_mutation_boundary() -> None:
    tree = ast.parse(_src())
    run_fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "run")
    body = ast.unparse(run_fn)
    assert body.index("_stage(r, wheels)") < body.index("transaction.cross_mutation_boundary(r)")
    assert body.index("transaction.cross_mutation_boundary(r)") < body.index("_stop_target()")
    assert body.index("_stop_target()") < body.index("_switch(r, staged, mode)")


def test_v3_catches_all_post_mutation_failures_and_pairs_rollback() -> None:
    s = _src()
    assert "except BaseException as exc:" in s
    assert "if not crossed:" in s
    assert "_rollback(r, reason, backup, backup_digest, mode, meta, evidence)" in s
    start = s.index("def _rollback(")
    end = s.index("\n\n__all__", start)
    rb = s[start:end]
    assert "_restore_runtime(r, mode)" in rb
    assert "_restore_db(backup, digest)" in rb
    assert "r.rollback_completed = True" in rb
    assert rb.index("_restore_runtime(r, mode)") < rb.index("r.rollback_completed = True")
    assert rb.index("_restore_db(backup, digest)") < rb.index("r.rollback_completed = True")


def test_v3_rollback_refuses_unknown_runtime_and_does_not_delete_candidate() -> None:
    s = _src()
    start = s.index("def _restore_runtime(")
    end = s.index("\ndef _restore_db", start)
    restore = s[start:end]
    assert "rollback refuses unknown runtime" in restore
    rb_start = s.index("def _rollback(")
    rb_end = s.index("\n\n__all__", rb_start)
    rb = s[rb_start:rb_end]
    assert "rmtree" not in rb


def test_v3_interrupts_enter_same_failure_path() -> None:
    s = _src()
    assert "signal.SIGINT" in s
    assert "signal.SIGTERM" in s
    assert "signal.SIGHUP" in s
    assert "raise PromotionInterrupted" in s


def test_v3_does_not_use_peer_deployer_cli_argument_order() -> None:
    s = _src()
    assert "staging.stage_candidate_runtime(" in s
    assert "PEER_DEPLOYER_RUNNER" not in s
    assert "--tx-id" not in s


def test_v3_hard_codes_exact_current_handoff_identities() -> None:
    s = _src()
    assert 'ACCEPTED_SHA = "541c9a3180b81bfb2fc450b3ef5f8648691b359d"' in s
    assert 'OLD_SHA = "e5f4249667a1602916d44ac62d10b921a299f05d"' in s
    assert 'OLD_SCHEMA = "c4d5e6f7a8b9"' in s
    assert 'NEW_SCHEMA = "f7a8b9c0d1e2"' in s


def test_v3_preflight_only_cleans_only_tx_staging() -> None:
    s = _src()
    assert "staging.safe_cleanup_staging(TARGET, tx_id)" in s
    assert "final release already exists; cleanup must prove ownership first" in s
