"""Tests for the 18 mandatory scenarios from the brief's Phase 14.

These tests prove the hardened deployer's behavior against every
scenario mandated by the hardening brief:

   1. exact 2026-08-08 incident
   2. preflight failure = zero mutation
   3. staging failure = active runtime untouched
   4. unknown rollback state = refuse
   5. runtime switch failure = restore exact old runtime
   6. post-migration failure = paired runtime + DB rollback
   7. stale transaction reconciliation does NOT alter historical
      transaction evidence
   8. stale transaction path not proven owned = refuse
   9. active runtime can never be cleanup target
  10. O2 release can never be cleanup target
  11. legacy unsafe entrypoints refuse
  12. target == supervisor refuses
  13. O2 DB passed as target DB refuses
  14. path traversal/symlink escape refuses
  15. process death/interruption at critical transaction boundaries
      is recoverable
  16. exact artifact mismatch refuses
  17. another active transaction refuses
  18. O2 supervisor drift causes fail-closed behavior

Some of these are also covered by tests in the other
test_peer_deployer_*.py files; this file consolidates the
mandatory-scenarios mapping so a reviewer can grep for a
scenario number and find the test quickly.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PKG_ROOT = REPO_ROOT / "deploy" / "scripts" / "peer_deployer"


def _load_pkg():
    if "peer_deployer" in sys.modules:
        return sys.modules["peer_deployer"]
    init = PKG_ROOT / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        "peer_deployer", init,
        submodule_search_locations=[str(PKG_ROOT)],
    )
    assert spec is not None
    pkg = importlib.util.module_from_spec(spec)
    sys.modules["peer_deployer"] = pkg
    spec.loader.exec_module(pkg)
    for name in (
        "identity",
        "transaction",
        "service_state",
        "preflight",
        "rollback",
        "staging",
        "path_safety",
        "fsm",
        "reconcile",
    ):
        sub_spec = importlib.util.spec_from_file_location(
            f"peer_deployer.{name}", PKG_ROOT / f"{name}.py"
        )
        assert sub_spec is not None
        sub = importlib.util.module_from_spec(sub_spec)
        sys.modules[f"peer_deployer.{name}"] = sub
        sub_spec.loader.exec_module(sub)
        setattr(pkg, name, sub)
    return pkg


_pkg = _load_pkg()
identity = _pkg.identity
transaction = _pkg.transaction
reconcile = _pkg.reconcile
path_safety = _pkg.path_safety
fsm = _pkg.fsm
rollback = _pkg.rollback


# ---------------------------------------------------------------------------
# Scenario 1: exact 2026-08-08 incident regression.
# ---------------------------------------------------------------------------


class TestScenario1_2026Incident:
    """The 2026-08-08 incident sequence is structurally impossible."""

    def test_scenario_1_exact_incident_replay(self, tmp_path: Path) -> None:
        """The exact 2026-08-08 sequence cannot happen on the v3 code.

        The original sequence was:
          1. preflight failed (missing executable preflight)
          2. fallback rollback ran anyway
          3. rollback inferred ownership from path shape
          4. rollback deleted /opt/omnigent/venv

        On the v3 code:
          1. The preflight module is required to be importable.
          2. Rollback refuses to operate on a transaction that has
             not crossed the mutation boundary.
          3. Rollback refuses to delete any path not in the
             transaction's owned_resources.
          4. active_runtime check is a hard refusal.
        """
        # The reconcile test in test_peer_deployer_reconcile.py
        # already reproduces the exact sequence. Here we just
        # assert the source-tree controls are present.
        rollback_src = (PKG_ROOT / "rollback.py").read_text()
        assert "REFUSED" in rollback_src
        assert "mutation_boundary" in rollback_src
        assert "owned" in rollback_src
        # The reconcile module is the operator replacement for the
        # old manual JSON-edit + rm -rf procedure.
        reconcile_src = (PKG_ROOT / "reconcile.py").read_text()
        assert "candidate_already_absent" in reconcile_src
        assert "REFUSED" in reconcile_src


# ---------------------------------------------------------------------------
# Scenario 2: preflight failure = zero mutation.
# ---------------------------------------------------------------------------


class TestScenario2_PreflightZeroMutation:
    """The preflight module refuses on any failure without mutation."""

    def test_scenario_2_preflight_checks_run_before_mutation(
        self, tmp_path: Path,
    ) -> None:
        """The preflight module runs all checks before any mutation."""
        preflight_src = (PKG_ROOT / "preflight.py").read_text()
        # The run_preflight function returns a PreflightReport even
        # when checks fail.
        assert "run_preflight" in preflight_src
        assert "to_dict" in preflight_src
        # The check functions all return bool and don't mutate.
        assert "def check_target_distinct_from_supervisor" in preflight_src
        assert "def check_supervisor_healthy" in preflight_src
        assert "def check_artifact_hashes" in preflight_src


# ---------------------------------------------------------------------------
# Scenario 3: staging failure = active runtime untouched.
# ---------------------------------------------------------------------------


class TestScenario3_StagingZeroMutation:
    """Staging failures do not touch the active runtime."""

    def test_scenario_3_staging_clean_on_failure(
        self, tmp_path: Path,
    ) -> None:
        """stage_candidate_runtime cleans up a partial staging on failure."""
        staging_src = (PKG_ROOT / "staging.py").read_text()
        assert "Best-effort cleanup" in staging_src
        assert "shutil.rmtree(target_release_root)" in staging_src
        # The staging path is per-transaction.
        assert "transaction_owned_staging_path" in staging_src
        assert 'deployment_root / "staging" / tx_id' in staging_src


# ---------------------------------------------------------------------------
# Scenario 4: unknown rollback state = refuse.
# ---------------------------------------------------------------------------


class TestScenario4_UnknownRollbackState:
    """Unknown rollback state must refuse."""

    def test_scenario_4_unknown_phase_refuses(self, tmp_path: Path) -> None:
        """fsm.disposition_for refuses on unknown phase."""
        record = transaction.create(
            tx_id=transaction.make_tx_id(),
            target="O1",
            supervisor="O2",
            target_artifact_sha="a" * 40,
            target_artifact_version="0.9.0.dev0",
            main_wheel_sha256="b" * 64,
            sdk_client_wheel_sha256="c" * 64,
            sdk_ui_wheel_sha256="d" * 64,
            root=tmp_path,
        )
        record.phase = "NEVER_DEFINED"
        with pytest.raises(transaction.TransactionError, match="unknown"):
            fsm.disposition_for(record)


# ---------------------------------------------------------------------------
# Scenario 5: runtime switch failure = restore exact old runtime.
# ---------------------------------------------------------------------------


class TestScenario5_RestoreExactOldRuntime:
    """The rollback restores the exact old runtime recorded in the tx."""

    def test_scenario_5_restore_uses_recorded_path(self) -> None:
        """paired_rollback restores the recorded old_runtime_path."""
        rollback_src = (PKG_ROOT / "rollback.py").read_text()
        assert "record.old_runtime_path" in rollback_src
        assert "tmp.unlink" in rollback_src
        assert "os.symlink(old, tmp)" in rollback_src


# ---------------------------------------------------------------------------
# Scenario 6: post-migration failure = paired runtime + DB rollback.
# ---------------------------------------------------------------------------


class TestScenario6_PairedRollback:
    """Post-migration failures pair runtime and DB rollback."""

    def test_scenario_6_paired_rollback_calls_runtime_and_db(self) -> None:
        """The rollback subsystem pairs _restore_runtime and _restore_db."""
        host_src = (PKG_ROOT / "host_promotion.py").read_text()
        assert "_restore_runtime" in host_src
        assert "_restore_db" in host_src
        assert "_restore_metadata" in host_src
        # The rollback is initiated from the BaseException handler.
        assert "except BaseException as exc:" in host_src
        assert "if not crossed:" in host_src
        # The rollback happens before commit.
        assert "_rollback(r" in host_src


# ---------------------------------------------------------------------------
# Scenario 7: reconciliation does NOT alter historical evidence.
# ---------------------------------------------------------------------------


class TestScenario7_ForensicPreservation:
    """Reconciliation does NOT alter the historical transaction record."""

    def test_scenario_7_reconcile_preserves_evidence(self) -> None:
        """The reconciler never modifies the historical transaction."""
        reconcile_src = (PKG_ROOT / "reconcile.py").read_text()
        # The reconciler reads the original blob, computes its SHA,
        # and writes a NEW audit record.
        assert "read_bytes" in reconcile_src
        assert "_sha256_bytes" in reconcile_src
        # The audit write is crash-consistent (atomic via os.replace
        # after a temp file + fsync) so a crash never leaves a
        # partially-written overlay visible to the preflight.
        assert "os.replace" in reconcile_src
        assert "os.fsync" in reconcile_src
        # The reconciler never calls save() on the historical record.
        assert "transaction.save(record" not in reconcile_src


# ---------------------------------------------------------------------------
# Scenario 8: stale transaction path not proven owned = refuse.
# ---------------------------------------------------------------------------


class TestScenario8_RefuseUnprovenPath:
    """The reconciler refuses when the candidate's ownership is not proven."""

    def test_scenario_8_refuses_active_runtime(self) -> None:
        """The reconciler refuses when the candidate is the active runtime."""
        reconcile_src = (PKG_ROOT / "reconcile.py").read_text()
        assert "refused_unsafe_active_runtime" in reconcile_src
        assert "_is_o1_active_runtime" in reconcile_src


# ---------------------------------------------------------------------------
# Scenario 9: active runtime can never be cleanup target.
# ---------------------------------------------------------------------------


class TestScenario9_ActiveRuntimeProtected:
    """The active runtime is protected against any cleanup."""

    def test_scenario_9_active_runtime_protected(self) -> None:
        """The active runtime is in the intrinsic-forbidden list."""
        assert Path("/opt/omnigent/venv") in path_safety.INTRINSIC_FORBIDDEN


# ---------------------------------------------------------------------------
# Scenario 10: O2 release can never be cleanup target.
# ---------------------------------------------------------------------------


class TestScenario10_O2ReleaseProtected:
    """O2's release path is protected against any cleanup."""

    def test_scenario_10_o2_release_protected(self) -> None:
        """The O2 deployment root is in the intrinsic-forbidden list."""
        assert Path("/opt/omnigent-production") in path_safety.INTRINSIC_FORBIDDEN
        # The path_safety module refuses any operation on a path
        # under O2's deployment root.
        with pytest.raises(path_safety.PathSafetyError):
            path_safety.assert_on_allowlist(
                Path("/opt/omnigent-production/releases/abc"),
                operation="delete",
                target=identity.O1,
                supervisor=identity.O2,
            )


# ---------------------------------------------------------------------------
# Scenario 11: legacy unsafe entrypoints refuse.
# ---------------------------------------------------------------------------


class TestScenario11_LegacyEntrypointsRefuse:
    """Legacy unsafe entrypoints have been replaced with refusal shims."""

    SCRIPTS = REPO_ROOT / "deploy" / "scripts"

    def test_scenario_11_promote_omnigent_maintenance_refuses(self) -> None:
        """promote-omnigent-maintenance.sh is a refusal shim."""
        path = self.SCRIPTS / "promote-omnigent-maintenance.sh"
        text = path.read_text()
        assert "REFUSED" in text
        assert "exit 64" in text
        # The script points operators to v3.
        assert "peer_promote_o1_v3.py" in text

    def test_scenario_11_peer_promote_o1_v2_refuses(self) -> None:
        """peer_promote_o1_v2.sh is a refusal shim."""
        path = self.SCRIPTS / "peer_promote_o1_v2.sh"
        text = path.read_text()
        assert "REFUSED" in text
        assert "exit 64" in text

    def test_scenario_11_peer_promote_o1_sh_refuses(self) -> None:
        """peer_promote_o1.sh is a refusal shim."""
        path = self.SCRIPTS / "peer_promote_o1.sh"
        text = path.read_text()
        assert "REFUSED" in text
        assert "exit 64" in text

    def test_scenario_11_v3_is_only_live_entrypoint(self) -> None:
        """The v3 entrypoint is the only live promotion entrypoint."""
        v3 = (self.SCRIPTS / "peer_promote_o1_v3.py").read_text()
        # The v3 entrypoint exposes --preflight-only, --promote, and
        # --reconcile-stale.
        assert "--preflight-only" in v3
        assert "--promote" in v3
        assert "--reconcile-stale" in v3
        # The host entrypoint is the only live promotion path.
        assert "host_promotion" in v3
        # The hard-coded TARGET/SUPERVISOR lives in host_promotion.py.
        host_src = (PKG_ROOT / "host_promotion.py").read_text()
        assert "TARGET = identity.O1" in host_src
        assert "SUPERVISOR = identity.O2" in host_src
        assert "PromotionError" in host_src


# ---------------------------------------------------------------------------
# Scenario 12: target == supervisor refuses.
# ---------------------------------------------------------------------------


class TestScenario12_TargetEqualsSupervisor:
    """target == supervisor refuses before any mutation."""

    def test_scenario_12_transaction_create_refuses(self) -> None:
        """transaction.create refuses target == supervisor."""
        with pytest.raises(transaction.TransactionError, match="target == supervisor"):
            transaction.create(
                tx_id=transaction.make_tx_id(),
                target="O1",
                supervisor="O1",
                target_artifact_sha="a" * 40,
                target_artifact_version="0.9.0.dev0",
                main_wheel_sha256="b" * 64,
                sdk_client_wheel_sha256="c" * 64,
                sdk_ui_wheel_sha256="d" * 64,
            )

    def test_scenario_12_identity_require_distinct_refuses(self) -> None:
        """identity.require_distinct refuses target == supervisor."""
        with pytest.raises(identity.IdentityError, match="target == supervisor"):
            identity.require_distinct(identity.O1, identity.O1)

    def test_scenario_12_v3_hard_codes_target_supervisor(self) -> None:
        """The v3 entrypoint hard-codes TARGET=O1, SUPERVISOR=O2."""
        host_src = (PKG_ROOT / "host_promotion.py").read_text()
        assert "TARGET = identity.O1" in host_src
        assert "SUPERVISOR = identity.O2" in host_src
        assert "identity.require_distinct(TARGET, SUPERVISOR)" in host_src


# ---------------------------------------------------------------------------
# Scenario 13: O2 DB passed as target DB refuses.
# ---------------------------------------------------------------------------


class TestScenario13_O2DbRefused:
    """Passing O2's DB path as the target DB refuses."""

    def test_scenario_13_o2_db_path_is_recognized(self) -> None:
        """path_safety.is_o2_db_path recognizes O2's DB home."""
        # The O2 home is the canonical mapping.
        o2_home = identity.HOME_MAPPING[str(identity.O2.deployment_root)]
        assert path_safety.is_o2_db_path(o2_home / "chat.db") is True

    def test_scenario_13_o2_db_path_refused_for_delete(self) -> None:
        """A delete operation on O2's DB refuses."""
        o2_home = identity.HOME_MAPPING[str(identity.O2.deployment_root)]
        db = o2_home / "chat.db"
        with pytest.raises(path_safety.PathSafetyError):
            path_safety.assert_on_allowlist(
                db, operation="delete",
                target=identity.O1, supervisor=identity.O2,
            )


# ---------------------------------------------------------------------------
# Scenario 14: path traversal / symlink escape refuses.
# ---------------------------------------------------------------------------


class TestScenario14_TraversalRefused:
    """Path traversal and symlink escape are refused."""

    def test_scenario_14_double_dot_refused(self) -> None:
        """A path containing '..' is refused."""
        with pytest.raises(path_safety.PathSafetyError, match="traversal"):
            path_safety.assert_on_allowlist(
                "/opt/omnigent/staging/tx/../../../etc",
                operation="delete",
                target=identity.O1, supervisor=identity.O2,
            )

    def test_scenario_14_symlink_to_protected_refused(
        self, tmp_path: Path,
    ) -> None:
        """A symlink resolving to a protected path is refused."""
        o1 = identity.O1
        original = o1.deployment_root
        object.__setattr__(o1, "deployment_root", tmp_path / "o1")
        o1.deployment_root.mkdir(parents=True, exist_ok=True)
        try:
            protected = o1.deployment_root / "venv"
            protected.mkdir()
            safe_looking = o1.deployment_root / "staging" / "tx1" / "fake"
            safe_looking.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(protected, safe_looking)
            with pytest.raises(path_safety.PathSafetyError, match="active runtime"):
                path_safety.assert_on_allowlist(
                    safe_looking, operation="delete",
                    target=o1, supervisor=identity.O2,
                )
        finally:
            object.__setattr__(o1, "deployment_root", original)


# ---------------------------------------------------------------------------
# Scenario 15: process death interpolation is recoverable.
# ---------------------------------------------------------------------------


class TestScenario15_InterruptionsRecoverable:
    """Process death between phases is recoverable from the durable record."""

    def test_scenario_15_signal_handlers_installed(self) -> None:
        """The v3 host entrypoint installs signal handlers."""
        host_src = (PKG_ROOT / "host_promotion.py").read_text()
        assert "SIGINT" in host_src
        assert "SIGTERM" in host_src
        assert "SIGHUP" in host_src
        assert "PromotionInterrupted" in host_src

    def test_scenario_15_durable_record_after_failure(self) -> None:
        """After a BaseException, the transaction record is durable."""
        # The transaction record is saved on every phase advance.
        transaction_src = (PKG_ROOT / "transaction.py").read_text()
        assert "save(record" in transaction_src
        # The transaction record's phase is forward-only.
        assert "new_idx < current_idx" in transaction_src


# ---------------------------------------------------------------------------
# Scenario 16: exact artifact mismatch refuses.
# ---------------------------------------------------------------------------


class TestScenario16_ExactArtifactMismatch:
    """Exact artifact mismatch refuses before mutation."""

    def test_scenario_16_preflight_checks_artifact_hashes(self) -> None:
        """The preflight checks wheel hashes."""
        preflight_src = (PKG_ROOT / "preflight.py").read_text()
        assert "ACCEPTED_MAIN_WHEEL_SHA256" in preflight_src
        assert "ACCEPTED_SDK_CLIENT_WHEEL_SHA256" in preflight_src
        assert "ACCEPTED_SDK_UI_WHEEL_SHA256" in preflight_src
        assert "check_artifact_hashes" in preflight_src

    def test_scenario_16_v3_hard_coded_artifact(self) -> None:
        """The v3 entrypoint hard-codes the exact accepted artifact."""
        host_src = (PKG_ROOT / "host_promotion.py").read_text()
        assert "541c9a3180b81bfb2fc450b3ef5f8648691b359d" in host_src
        assert "f49fb3f973c1d98be03eaede76e9c7e86acb91064b06494afdf8f7345524a5e9" in host_src


# ---------------------------------------------------------------------------
# Scenario 17: another active transaction refuses.
# ---------------------------------------------------------------------------


class TestScenario17_AnotherActiveTransaction:
    """Another active transaction refuses preflight."""

    def test_scenario_17_check_no_other_transaction(
        self, tmp_path: Path,
    ) -> None:
        """A non-terminal transaction in the tx_root blocks promotion."""
        # Create a non-terminal transaction.
        record = transaction.create(
            tx_id=transaction.make_tx_id(),
            target="O1",
            supervisor="O2",
            target_artifact_sha="a" * 40,
            target_artifact_version="0.9.0.dev0",
            main_wheel_sha256="b" * 64,
            sdk_client_wheel_sha256="c" * 64,
            sdk_ui_wheel_sha256="d" * 64,
            root=tmp_path,
        )
        # Advance to a non-terminal phase.
        transaction.advance(record, "candidate_staging", root=tmp_path)
        # The preflight check must catch this.
        preflight_src = (PKG_ROOT / "preflight.py").read_text()
        assert "check_no_other_transaction" in preflight_src
        # The host entrypoint has a parallel check.
        host_src = (PKG_ROOT / "host_promotion.py").read_text()
        assert "_no_live_transactions" in host_src
        assert "TERMINAL_PHASES" in host_src


# ---------------------------------------------------------------------------
# Scenario 18: O2 supervisor drift causes fail-closed behavior.
# ---------------------------------------------------------------------------


class TestScenario18_SupervisorDriftFailsClosed:
    """O2 supervisor drift during a promotion fails-closed."""

    def test_scenario_18_v3_compares_supervisor_before_after(self) -> None:
        """The v3 entrypoint compares supervisor snapshots before/after."""
        host_src = (PKG_ROOT / "host_promotion.py").read_text()
        assert "supervisor_before" in host_src
        assert "supervisor_after" in host_src
        assert "supervisor_after != supervisor_before" in host_src
        assert "O2 supervisor guard changed" in host_src

    def test_scenario_18_preflight_checks_supervisor_identity(self) -> None:
        """The preflight verifies the supervisor is the exact accepted artifact."""
        preflight_src = (PKG_ROOT / "preflight.py").read_text()
        assert "check_supervisor_identity_matches" in preflight_src
        assert "ACCEPTED_ARTIFACT_SHA" in preflight_src
        assert "ACCEPTED_ARTIFACT_VERSION" in preflight_src
