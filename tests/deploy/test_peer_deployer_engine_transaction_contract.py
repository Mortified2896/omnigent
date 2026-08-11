"""Tests for the generic engine transaction contract.

These tests prove that the generic engine in
``deploy/scripts/peer_deployer/engine.py`` honors the transaction
contract:

  1. The transaction record carries the old/new runtime path, old/new
     SHA/version, and the DB backup path BEFORE the first target
     mutation.
  2. ``cross_mutation_boundary`` is recorded BEFORE the first target
     mutation, not after.
  3. A failure injected between the boundary crossing and the first
     mutation is NOT reported as ``PRE-MUTATION FAILURE; TARGET
     UNTOUCHED``; the engine treats it as a post-mutation failure and
     delegates to the rollback subsystem.
  4. Rollback restores the venv symlink to the OLD release.

The tests use small synthetic fixtures stored under ``tmp_path`` and
do not touch the host's ``/opt/`` or ``/var/lib/`` trees.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PKG_ROOT = REPO_ROOT / "deploy" / "scripts" / "peer_deployer"


def _load_pkg():
    if "peer_deployer" in sys.modules:
        pkg = sys.modules["peer_deployer"]
    else:
        init = PKG_ROOT / "__init__.py"
        spec = importlib.util.spec_from_file_location(
            "peer_deployer", init,
            submodule_search_locations=[str(PKG_ROOT)],
        )
        assert spec is not None
        pkg = importlib.util.module_from_spec(spec)
        sys.modules["peer_deployer"] = pkg
        spec.loader.exec_module(pkg)
    # Always ensure all submodules are loaded (other test files may
    # have loaded the package but not every submodule).
    for name in [
        "identity",
        "transaction",
        "service_state",
        "staging",
        "plan",
        "registry",
        "rollback",
        "engine",
    ]:
        mod_name = f"peer_deployer.{name}"
        if mod_name in sys.modules:
            sub = sys.modules[mod_name]
        else:
            sub_spec = importlib.util.spec_from_file_location(
                mod_name, PKG_ROOT / f"{name}.py"
            )
            assert sub_spec is not None
            sub = importlib.util.module_from_spec(sub_spec)
            sys.modules[mod_name] = sub
            sub_spec.loader.exec_module(sub)
        setattr(pkg, name, sub)
    return pkg


_pkg = _load_pkg()
engine = _pkg.engine
transaction = _pkg.transaction
identity = _pkg.identity
plan = _pkg.plan
registry = _pkg.registry
rollback = _pkg.rollback


# ---------------------------------------------------------------------------
# Synthetic fixtures.
# ---------------------------------------------------------------------------


def _make_layout(tmp_path: Path, *, sha: str, version: str) -> Path:
    """Build a minimal release directory with a venv/bin/python shim.

    Returns the release directory (parent of ``venv``).
    """
    release = tmp_path / "releases" / sha
    (release / "venv" / "bin").mkdir(parents=True)
    python = release / "venv" / "bin" / "python"
    python.write_text("#!/bin/sh\necho stub\n")
    python.chmod(0o755)
    provenance = release / "PROVENANCE.txt"
    provenance.write_text(
        f"sha={sha}\npackage_version={version}\n"
    )
    return release


def _make_supervisor_runtime(sup_root_path: Path, *, sha: str, version: str) -> Path:
    """Build a minimal supervisor release directory with site-packages."""
    sup_root = sup_root_path / "releases" / sha
    (sup_root / "venv" / "lib" / "python3.12" / "site-packages").mkdir(parents=True)
    (sup_root / "venv" / "lib" / "python3.12" / "site-packages" / "omnigent-0.0.0.dist-info").mkdir()
    (sup_root / "venv" / "bin").mkdir(parents=True)
    (sup_root / "venv" / "bin" / "python").write_text("#!/bin/sh\necho stub\n")
    (sup_root / "venv" / "bin" / "python").chmod(0o755)
    provenance = sup_root / "PROVENANCE.txt"
    provenance.write_text(
        f"sha={sha}\npackage_version={version}\n"
    )
    return sup_root


def _make_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE alembic_version(version_num VARCHAR(32))")
    conn.execute("INSERT INTO alembic_version VALUES ('c4d5e6f7a8b9')")
    conn.execute("CREATE TABLE conversations(id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE items(id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE labels(id INTEGER PRIMARY KEY)")
    conn.execute("INSERT INTO conversations VALUES (1), (2)")
    conn.execute("INSERT INTO items VALUES (1), (2), (3)")
    conn.execute("INSERT INTO labels VALUES (1)")
    conn.commit()
    conn.close()


def _make_registry_and_plan(
    tmp_path: Path,
    *,
    old_release: Path,
    new_release: Path,
    sup_release: Path,
    target_home: Path,
    supervisor_home: Path,
    old_sha: str,
    new_sha: str,
    old_version: str,
    new_version: str,
) -> tuple[Any, Any]:
    """Build a minimal registry + plan for ``target = O1``."""
    artifact = registry.ArtifactEntry(
        artifact_sha=new_sha,
        version=new_version,
        wheels={
            "omnigent-0.9.0.dev0-py3-none-any.whl": {
                "path": "/tmp/dummy-main.whl",
                "sha256": "0" * 64,
            },
            "omnigent_client-0.9.0.dev0-py3-none-any.whl": {
                "path": "/tmp/dummy-client.whl",
                "sha256": "0" * 64,
            },
            "omnigent_ui_sdk-0.9.0.dev0-py3-none-any.whl": {
                "path": "/tmp/dummy-ui.whl",
                "sha256": "0" * 64,
            },
        },
        release_root=str(new_release),
        provenance=str(new_release / "PROVENANCE.txt"),
    )
    trusted_registry = registry.TrustedRegistry(
        release_digest="test-digest",
        supervisor_python="/usr/bin/python3",
        artifacts={new_sha: artifact},
        raw={},
    )
    plan_blob = {
        "schema": "control-room-peer-deployer.promotion-plan.v1",
        "allowed_topology": {"supervisor": "O2", "target": "O1"},
        "service_units": {
            "target": ["omnigent.service", "omnigent-host.service"],
            "supervisor": ["omnigent-production.service", "omnigent-production-host.service"],
        },
        "deployment_roots": {
            "target": str(old_release.parent.parent),
            "supervisor": str(sup_release.parent.parent),
        },
        "state_roots": {
            "target": str(target_home),
            "supervisor": str(supervisor_home),
        },
        "health_urls": {
            "target": "http://127.0.0.1:4097/health",
            "supervisor": "http://127.0.0.1:4197/health",
        },
        "expected_pre_state": {
            "target": {
                "commit_sha": old_sha,
                "version": old_version,
                "schema": "c4d5e6f7a8b9",
            },
            "supervisor": {
                "commit_sha": new_sha,
                "version": new_version,
            },
        },
        "accepted_artifact_sha": new_sha,
        "accepted_artifact_version": new_version,
        "rollback": {"paired_runtime_db": True, "supervisor_zero_drift": True},
    }
    promotion_plan = plan.PromotionPlan(
        name="o2_supervises_o1",
        supervisor_name="O2",
        target_name="O1",
        target_service_units=("omnigent.service", "omnigent-host.service"),
        supervisor_service_units=("omnigent-production.service", "omnigent-production-host.service"),
        target_deployment_root=str(old_release.parent.parent),
        supervisor_deployment_root=str(sup_release.parent.parent),
        target_state_root=str(target_home),
        supervisor_state_root=str(supervisor_home),
        target_health_url="http://127.0.0.1:4097/health",
        supervisor_health_url="http://127.0.0.1:4197/health",
        accepted_artifact_sha=new_sha,
        accepted_artifact_version=new_version,
        expected_target_pre_state=plan.InstanceState(
            commit_sha=old_sha, version=old_version, schema="c4d5e6f7a8b9",
        ),
        expected_supervisor_pre_state=plan.InstanceState(
            commit_sha=new_sha, version=new_version,
        ),
        paired_rollback=True,
        supervisor_zero_drift=True,
        raw=plan_blob,
    )
    return trusted_registry, promotion_plan


@pytest.fixture
def tx_root(tmp_path: Path) -> Path:
    root = tmp_path / "tx"
    root.mkdir()
    object.__setattr__(transaction, "DEFAULT_TX_ROOT", root)
    object.__setattr__(engine, "RUNTIME_TRANSACTION_ROOT", lambda: root)
    return root


@pytest.fixture(autouse=True)
def _stub_require_distinct(monkeypatch: pytest.MonkeyPatch) -> None:
    """The engine calls ``identity.require_distinct`` with port=0 instances
    which the real ``Instance.__post_init__`` rejects. These tests use
    synthetic instances so we stub the topology check."""
    monkeypatch.setattr(_pkg.identity, "require_distinct", lambda *a, **kw: None)


# ---------------------------------------------------------------------------
# Direct tests of the engine's _resolve_old_runtime_path helper.
# ---------------------------------------------------------------------------


def test_resolve_old_runtime_path_through_current_symlink(tmp_path: Path) -> None:
    """When the O2-style ``current`` symlink exists, ``_resolve_old_runtime_path``
    returns the release directory (parent of venv)."""
    release = _make_layout(tmp_path, sha="a" * 40, version="0.8.1")
    target_root = tmp_path / "deploy"
    target_root.mkdir()
    (target_root / "current").symlink_to(release)
    resolved = engine._resolve_old_runtime_path(target_root)
    assert resolved == release


def test_resolve_old_runtime_path_through_venv_symlink(tmp_path: Path) -> None:
    """When the O1-style ``venv`` symlink exists, ``_resolve_old_runtime_path``
    returns the release directory (parent of venv)."""
    release = _make_layout(tmp_path, sha="b" * 40, version="0.8.1")
    target_root = tmp_path / "deploy"
    target_root.mkdir()
    (target_root / "venv").symlink_to(release / "venv")
    resolved = engine._resolve_old_runtime_path(target_root)
    assert resolved == release


def test_resolve_old_runtime_path_missing_raises(tmp_path: Path) -> None:
    """No symlink at all -> PromotionError."""
    target_root = tmp_path / "deploy"
    target_root.mkdir()
    with pytest.raises(engine.PromotionError):
        engine._resolve_old_runtime_path(target_root)


# ---------------------------------------------------------------------------
# End-to-end tests of the run_promotion transaction contract.
# ---------------------------------------------------------------------------


def _wire_dry_environment(
    tmp_path: Path,
    *,
    old_sha: str = "e" * 40,
    new_sha: str = "f" * 40,
) -> dict[str, Any]:
    """Build a fully-populated synthetic environment for run_promotion.

    The environment redirects identity paths and registry paths into
    ``tmp_path`` so the engine never touches the host's real /opt/ or
    /var/lib/ trees.
    """
    target_root = tmp_path / "deploy"
    target_root.mkdir()
    supervisor_root = tmp_path / "supervisor"
    supervisor_root.mkdir()
    target_home = tmp_path / "home"
    target_home.mkdir()
    supervisor_home = tmp_path / "supervisor_home"
    supervisor_home.mkdir()

    # Build the OLD release (target currently points here).
    old_release = _make_layout(target_root, sha=old_sha, version="0.8.1")
    (target_root / "venv").symlink_to(old_release / "venv")

    # Build the NEW release (the candidate).
    new_release_parent = target_root / "releases"
    new_release_parent.mkdir(parents=True, exist_ok=True)
    new_release = new_release_parent / new_sha
    new_release.mkdir()
    (new_release / "venv").mkdir()
    (new_release / "PROVENANCE.txt").write_text(
        f"sha={new_sha}\npackage_version=0.9.0.dev0\n"
    )

    # Build the supervisor release.
    sup_release = _make_supervisor_runtime(
        supervisor_root, sha=new_sha, version="0.9.0.dev0",
    )

    # Build the target DB.
    db_path = target_home / "chat.db"
    _make_db(db_path)

    # Build registry + plan.
    trusted_registry, promotion_plan = _make_registry_and_plan(
        tmp_path,
        old_release=old_release,
        new_release=new_release,
        sup_release=sup_release,
        target_home=target_home,
        supervisor_home=supervisor_home,
        old_sha=old_sha,
        new_sha=new_sha,
        old_version="0.8.1",
        new_version="0.9.0.dev0",
    )

    # Redirect identity so the engine resolves /opt/ -> tmp_path.
    identity_module = _pkg.identity
    real_o1 = identity_module.O1
    real_o2 = identity_module.O2
    object.__setattr__(identity_module.O1, "deployment_root", target_root)
    object.__setattr__(identity_module.O2, "deployment_root", supervisor_root)
    real_homing = dict(identity_module.HOME_MAPPING)
    identity_module.HOME_MAPPING[str(target_root)] = target_home
    identity_module.HOME_MAPPING[str(supervisor_root)] = supervisor_home

    evidence_root = tmp_path / "evidence"
    return {
        "identity_module": identity_module,
        "real_o1": real_o1,
        "real_o2": real_o2,
        "real_homing": real_homing,
        "target_root": target_root,
        "supervisor_root": supervisor_root,
        "target_home": target_home,
        "supervisor_home": supervisor_home,
        "old_release": old_release,
        "new_release": new_release,
        "promotion_plan": promotion_plan,
        "registry": trusted_registry,
        "evidence_root": evidence_root,
        "db_path": db_path,
    }


def _restore_identity(env: dict[str, Any]) -> None:
    identity_module = env["identity_module"]
    object.__setattr__(identity_module.O1, "deployment_root", env["real_o1"].deployment_root)
    object.__setattr__(identity_module.O2, "deployment_root", env["real_o2"].deployment_root)
    # Restore from snapshot.
    identity_module.HOME_MAPPING.clear()
    identity_module.HOME_MAPPING.update(env["real_homing"])


def test_engine_persists_paths_before_first_mutation(tx_root: Path, tmp_path: Path) -> None:
    """After run_promotion creates the transaction record (still in
    preflight stage), the transaction record on disk must contain
    old/new runtime paths and SHAs."""
    env = _wire_dry_environment(tmp_path)
    try:
        # Patch the engine's supervisor helpers to be safe no-ops.
        identity_module = env["identity_module"]
        promotion_plan = env["promotion_plan"]
        trusted_registry = env["registry"]
        evidence_root = env["evidence_root"]

        # Bypass identity/runtime identity reads by monkeypatching.
        monkey = pytest.MonkeyPatch()
        monkey.setattr(
            engine, "_supervisor_guard",
            lambda plan: {"service_units": {}, "runtime": {"commit_sha": "f" * 40, "version": "0.9.0.dev0"}, "health": "ok"},
        )
        monkey.setattr(
            engine, "_target_pre_state",
            lambda plan: {
                "service_units": {},
                "runtime": {"commit_sha": "e" * 40, "version": "0.8.1"},
                "schema": "c4d5e6f7a8b9",
                "integrity": "ok",
                "counts": {"conversations": 2, "items": 3, "labels": 1},
                "health": "ok",
            },
        )
        monkey.setattr(
            engine, "_verify_artifacts",
            lambda artifact: {"main": Path("/tmp/dummy"), "sdk_client": Path("/tmp/dummy"), "sdk_ui": Path("/tmp/dummy")},
        )
        monkey.setattr(
            engine, "_backup_db",
            lambda plan, dst: "0" * 64,
        )
        # Catch the failure at the staging step (before any mutation).
        def _fail_staging(*a, **kw):
            raise RuntimeError("injected staging failure")
        monkey.setattr(engine, "_stage_candidate", _fail_staging)

        result = engine.run_promotion(
            plan=promotion_plan,
            registry=trusted_registry,
            evidence_root=evidence_root,
            promote=True,
        )
        monkey.undo()

        assert result.verdict == "PRE-MUTATION FAILURE; TARGET UNTOUCHED"
        # The transaction record on disk must carry the full state.
        record = transaction.load(result.tx_id, root=tx_root)
        assert record.old_runtime_path == str(env["old_release"])
        assert record.new_runtime_path == str(env["new_release"])
        assert record.old_runtime_sha == "e" * 40
        assert record.old_runtime_version == "0.8.1"
        assert record.new_runtime_sha == "f" * 40
        assert record.new_runtime_version == "0.9.0.dev0"
        assert record.db_backup_path.endswith("chat.db")
        assert record.db_backup_sha256 == "0" * 64
        assert record.db_backup_integrity == "ok"
        assert record.mutation_boundary_crossed is False
        assert record.target == "O1"
        assert record.supervisor == "O2"
    finally:
        _restore_identity(env)


def test_engine_does_not_report_pre_mutation_when_failure_is_after_boundary(
    tx_root: Path, tmp_path: Path,
) -> None:
    """If a failure is injected BETWEEN the boundary crossing and the
    first mutation (i.e. inside ``_switch_target_symlink``), the
    engine MUST NOT report ``PRE-MUTATION FAILURE; TARGET UNTOUCHED``.

    The report must be either ``ROLLED BACK`` or ``CRITICAL``."""
    env = _wire_dry_environment(tmp_path)
    try:
        identity_module = env["identity_module"]
        promotion_plan = env["promotion_plan"]
        trusted_registry = env["registry"]
        evidence_root = env["evidence_root"]

        monkey = pytest.MonkeyPatch()
        monkey.setattr(
            engine, "_supervisor_guard",
            lambda plan: {"service_units": {}, "runtime": {"commit_sha": "f" * 40, "version": "0.9.0.dev0"}, "health": "ok"},
        )
        monkey.setattr(
            engine, "_target_pre_state",
            lambda plan: {
                "service_units": {},
                "runtime": {"commit_sha": "e" * 40, "version": "0.8.1"},
                "schema": "c4d5e6f7a8b9",
                "integrity": "ok",
                "counts": {"conversations": 2, "items": 3, "labels": 1},
                "health": "ok",
            },
        )
        monkey.setattr(
            engine, "_verify_artifacts",
            lambda artifact: {"main": Path("/tmp/dummy"), "sdk_client": Path("/tmp/dummy"), "sdk_ui": Path("/tmp/dummy")},
        )
        monkey.setattr(
            engine, "_backup_db",
            lambda plan, dst: "0" * 64,
        )
        monkey.setattr(
            engine, "_stage_candidate",
            lambda plan, record, wheels, artifact, final_release: tmp_path / "staging_root",
        )
        # The symlink swap fails: this is the FIRST target mutation.
        def _fail_switch(plan, record, staging_root, final_release, artifact):
            raise RuntimeError("injected switch failure")
        monkey.setattr(engine, "_switch_target_symlink", _fail_switch)

        result = engine.run_promotion(
            plan=promotion_plan,
            registry=trusted_registry,
            evidence_root=evidence_root,
            promote=True,
        )
        monkey.undo()

        # The verdict must NOT be the misleading pre-mutation label.
        assert "PRE-MUTATION" not in result.verdict, (
            f"engine reported pre-mutation failure after actual mutation: {result.verdict}"
        )
        assert result.verdict in ("ROLLED BACK", "CRITICAL: PROMOTION AND ROLLBACK FAILED")
        # The record on disk must show the boundary was crossed.
        record = transaction.load(result.tx_id, root=tx_root)
        assert record.mutation_boundary_crossed is True
    finally:
        _restore_identity(env)


def test_engine_persists_full_record_before_first_mutation_in_dry_run(
    tx_root: Path, tmp_path: Path,
) -> None:
    """A preflight-only invocation must persist the full record BEFORE
    any mutation. Promote=False must not mutate. The record must
    carry old/new runtime paths and SHAs."""
    env = _wire_dry_environment(tmp_path)
    try:
        promotion_plan = env["promotion_plan"]
        trusted_registry = env["registry"]
        evidence_root = env["evidence_root"]

        monkey = pytest.MonkeyPatch()
        monkey.setattr(
            engine, "_supervisor_guard",
            lambda plan: {"service_units": {}, "runtime": {"commit_sha": "f" * 40, "version": "0.9.0.dev0"}, "health": "ok"},
        )
        monkey.setattr(
            engine, "_target_pre_state",
            lambda plan: {
                "service_units": {},
                "runtime": {"commit_sha": "e" * 40, "version": "0.8.1"},
                "schema": "c4d5e6f7a8b9",
                "integrity": "ok",
                "counts": {"conversations": 2, "items": 3, "labels": 1},
                "health": "ok",
            },
        )
        monkey.setattr(
            engine, "_verify_artifacts",
            lambda artifact: {"main": Path("/tmp/dummy"), "sdk_client": Path("/tmp/dummy"), "sdk_ui": Path("/tmp/dummy")},
        )

        result = engine.run_promotion(
            plan=promotion_plan,
            registry=trusted_registry,
            evidence_root=evidence_root,
            promote=False,
        )
        monkey.undo()

        assert result.verdict == "PREFLIGHT PASSED; NO MUTATION"
        record = transaction.load(result.tx_id, root=tx_root)
        assert record.old_runtime_path == str(env["old_release"])
        assert record.new_runtime_path == str(env["new_release"])
        assert record.old_runtime_sha == "e" * 40
        assert record.new_runtime_sha == "f" * 40
        assert record.mutation_boundary_crossed is False
    finally:
        _restore_identity(env)


def test_engine_rollback_restores_venv_symlink_after_switch_failure(
    tx_root: Path, tmp_path: Path,
) -> None:
    """End-to-end: a failure during the first target mutation triggers
    rollback that restores the venv symlink to the old release.

    The test sets up a real symlink swap (no monkey-patching of
    ``_switch_target_symlink``) and injects a failure during
    ``_stop_target`` (which runs after the swap). Before the failure,
    the venv symlink has been swapped to the new release. After
    rollback, the symlink must point back to the old release."""
    env = _wire_dry_environment(tmp_path)
    try:
        promotion_plan = env["promotion_plan"]
        trusted_registry = env["registry"]
        evidence_root = env["evidence_root"]
        target_root = env["target_root"]
        old_release = env["old_release"]
        new_release = env["new_release"]

        monkey = pytest.MonkeyPatch()
        monkey.setattr(
            engine, "_supervisor_guard",
            lambda plan: {"service_units": {}, "runtime": {"commit_sha": "f" * 40, "version": "0.9.0.dev0"}, "health": "ok"},
        )
        monkey.setattr(
            engine, "_target_pre_state",
            lambda plan: {
                "service_units": {},
                "runtime": {"commit_sha": "e" * 40, "version": "0.8.1"},
                "schema": "c4d5e6f7a8b9",
                "integrity": "ok",
                "counts": {"conversations": 2, "items": 3, "labels": 1},
                "health": "ok",
            },
        )
        monkey.setattr(
            engine, "_verify_artifacts",
            lambda artifact: {"main": Path("/tmp/dummy"), "sdk_client": Path("/tmp/dummy"), "sdk_ui": Path("/tmp/dummy")},
        )
        monkey.setattr(
            engine, "_backup_db",
            lambda plan, dst: "0" * 64,
        )

        # DON'T stub _stage_candidate / _switch_target_symlink: let them
        # actually run so the venv symlink swap is observable.
        # We create the staging root on demand to make the test
        # self-contained.
        real_stage_candidate = engine._stage_candidate

        def _stage_with_real_staging(
            plan, record, wheels, artifact, final_release,
        ):
            """Build a stageable directory inside the test fixture and
            return its path. This is the minimal amount of work needed
            for ``_switch_target_symlink`` to succeed."""
            staging_root = target_root / "staging" / record.tx_id
            (staging_root / "venv").mkdir(parents=True)
            # Match the structure expected by the engine.
            (final_release / "venv").mkdir(parents=True, exist_ok=True)
            return staging_root

        def _stop_target_fails(plan):
            raise RuntimeError("injected stop failure")

        monkey.setattr(engine, "_stage_candidate", _stage_with_real_staging)
        monkey.setattr(engine, "_stop_target", _stop_target_fails)

        # Pre-condition: venv symlink points to old release.
        assert (target_root / "venv").resolve() == (old_release / "venv").resolve()

        result = engine.run_promotion(
            plan=promotion_plan,
            registry=trusted_registry,
            evidence_root=evidence_root,
            promote=True,
        )
        monkey.undo()

        assert "PRE-MUTATION" not in result.verdict
        assert result.verdict in ("ROLLED BACK", "CRITICAL: PROMOTION AND ROLLBACK FAILED")
        # The venv symlink MUST be restored to old release.
        current = (target_root / "venv").resolve()
        assert current == (old_release / "venv").resolve(), (
            f"venv symlink not restored: current={current} expected={old_release / 'venv'}"
        )
        # The record on disk reflects the rollback.
        record = transaction.load(result.tx_id, root=tx_root)
        assert record.mutation_boundary_crossed is True
    finally:
        _restore_identity(env)

