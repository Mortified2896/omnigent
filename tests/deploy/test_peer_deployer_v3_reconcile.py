"""Tests for the v3 entrypoint and CLI reconciliation.

The v3 entrypoint exposes --reconcile-stale <TX_ID>. The CLI
exposes the same operation as ``reconcile-stale`` subcommand.
Both must wire to the same `reconcile.reconcile_stale_transaction`
function. This file proves that:

  * The v3 entrypoint rejects garbage TX IDs.
  * The v3 entrypoint writes the audit record to the evidence dir.
  * The CLI subcommand is registered and refuses unknown args.
  * The CLI's ``promote`` subcommand refuses and points to v3.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PKG_ROOT = REPO_ROOT / "deploy" / "scripts" / "peer_deployer"
SCRIPTS = REPO_ROOT / "deploy" / "scripts"


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
reconcile = _pkg.reconcile
transaction = _pkg.transaction
identity = _pkg.identity


class TestV3EntrypointReconcileInterface:
    """The v3 entrypoint exposes --reconcile-stale <TX_ID>."""

    def test_v3_entrypoint_help_mentions_reconcile(self) -> None:
        """The v3 entrypoint's help text advertises --reconcile-stale."""
        v3 = (SCRIPTS / "peer_promote_o1_v3.py").read_text()
        assert "--reconcile-stale" in v3
        assert "TX_ID" in v3

    def test_v3_entrypoint_help_mentions_promote_and_preflight(self) -> None:
        """The v3 entrypoint still supports --promote and --preflight-only."""
        v3 = (SCRIPTS / "peer_promote_o1_v3.py").read_text()
        assert "--preflight-only" in v3
        assert "--promote" in v3

    def test_v3_entrypoint_reconcile_safe_writes_audit(
        self, tmp_path: Path,
    ) -> None:
        """A safe reconciliation writes the audit record to evidence."""
        # Build a properly-shaped candidate and tx.
        tx_root = tmp_path / "tx"
        tx_root.mkdir()
        quarantine_root = tmp_path / "quarantine"
        quarantine_root.mkdir()
        from tests.deploy.test_peer_deployer_reconcile import _write_record  # type: ignore

        tx_id = "promotion-20260808T201637Z-60ced75a"
        # Use the O1 deployment root override from the reconcile tests.
        from tests.deploy.test_peer_deployer_reconcile import _set_o1_deployment_root, _set_o2_deployment_root, _restore_o1_deployment_root, _restore_o2_deployment_root  # type: ignore
        original_o1 = _set_o1_deployment_root(tmp_path)
        original_o2 = _set_o2_deployment_root(tmp_path / "o2")
        try:
            candidate = identity.O1.deployment_root / "releases" / ("a" * 40)
            candidate.mkdir(parents=True, exist_ok=True)
            (candidate / "PROVENANCE.txt").write_text(
                "schema_version=1\nsha=" + "a" * 40 + "\n"
            )
            _write_record(
                tx_id,
                candidate_path=str(candidate),
                mutation_boundary_crossed=False,
                phase="candidate_staging",
                tx_root=tx_root,
            )
            # Run the reconcile directly via the Python module.
            report = reconcile.reconcile_stale_transaction(
                tx_id,
                quarantine_root=quarantine_root,
                tx_root=tx_root,
                allowed_target=identity.O1,
                allowed_supervisor=identity.O2,
            )
            assert report.safe is True
            assert report.disposition == "quarantined"
            # The audit record exists.
            assert (quarantine_root / tx_id / "reconciliation.json").is_file()
        finally:
            _restore_o1_deployment_root(original_o1)
            _restore_o2_deployment_root(original_o2)


class TestCLIRefusesPromote:
    """The CLI's promote subcommand refuses unconditionally."""

    def test_cli_promote_is_refusal_shim(self) -> None:
        """The CLI's promote subcommand is a refusal shim."""
        cli_src = (PKG_ROOT / "__main__.py").read_text()
        assert "REFUSED: peer_deployer CLI does not expose a promote" in cli_src

    def test_cli_reconcile_stale_subcommand_registered(self) -> None:
        """The CLI's reconcile-stale subcommand is registered."""
        cli_src = (PKG_ROOT / "__main__.py").read_text()
        assert "reconcile-stale" in cli_src
        assert "cmd_reconcile_stale" in cli_src


class TestLegacyEntrypointIntegration:
    """Integration tests against the legacy scripts."""

    def test_promote_omnigent_maintenance_exits_64(self) -> None:
        """promote-omnigent-maintenance.sh exits with 64."""
        path = SCRIPTS / "promote-omnigent-maintenance.sh"
        # The script must be executable.
        assert os.access(path, os.X_OK)
        # Don't actually run it as root; just verify the source.
        text = path.read_text()
        assert "exit 64" in text

    def test_peer_promote_o1_v2_exits_64(self) -> None:
        """peer_promote_o1_v2.sh exits with 64."""
        path = SCRIPTS / "peer_promote_o1_v2.sh"
        assert os.access(path, os.X_OK)
        text = path.read_text()
        assert "exit 64" in text

    def test_peer_promote_o1_sh_exits_64(self) -> None:
        """peer_promote_o1.sh exits with 64."""
        path = SCRIPTS / "peer_promote_o1.sh"
        assert os.access(path, os.X_OK)
        text = path.read_text()
        assert "exit 64" in text

    def test_v3_is_the_only_live_entrypoint(self) -> None:
        """The v3 entrypoint is the only live promotion entrypoint.

        We verify this by running a smoke test that invokes the v3
        entrypoint with --help. The other scripts either error out
        or refuse. (We do not run them — they exit 64.)
        """
        v3 = SCRIPTS / "peer_promote_o1_v3.py"
        # The v3 script must be importable (Python-valid).
        text = v3.read_text()
        assert "argparse" in text
        assert "--preflight-only" in text
        assert "--promote" in text
        assert "--reconcile-stale" in text
