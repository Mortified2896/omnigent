"""Tests for the peer-deployer transaction model.

These tests prove:

  * transaction IDs follow the canonical format
  * transactions record target/supervisor identity
  * target == supervisor is refused at creation
  * owned resources are tracked correctly
  * phases advance forward-only
  * mutation_boundary_crossed is permanent
  * is_owned matches by realpath
"""

from __future__ import annotations

import importlib.util
import sys
import json
import os
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PKG_ROOT = REPO_ROOT / "deploy" / "scripts" / "peer_deployer"

def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        f"peer_deployer_{name}", PKG_ROOT / f"{name}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module

transaction = _load("transaction")


@pytest.fixture
def tx_root(tmp_path: Path) -> Path:
    root = tmp_path / "transactions"
    root.mkdir()
    return root


def test_tx_id_format() -> None:
    tx_id = transaction.make_tx_id()
    assert re.match(r"^promotion-\d{8}T\d{6}Z-[0-9a-f]{8}$", tx_id)


def test_assert_tx_id_accepts_canonical() -> None:
    transaction.assert_tx_id("promotion-20260808T182145Z-0123abcd")


def test_assert_tx_id_rejects_garbage() -> None:
    for bad in ["", "garbage", "promotion-not-a-tx", "promotion-20260808T182145Z-XXXX"]:
        with pytest.raises(transaction.TransactionError):
            transaction.assert_tx_id(bad)


def test_create_records_target_and_supervisor(tx_root: Path) -> None:
    tx_id = transaction.make_tx_id()
    record = transaction.create(
        tx_id=tx_id,
        target="O1",
        supervisor="O2",
        target_artifact_sha="a" * 40,
        target_artifact_version="0.9.0.dev0",
        main_wheel_sha256="b" * 64,
        sdk_client_wheel_sha256="c" * 64,
        sdk_ui_wheel_sha256="d" * 64,
        root=tx_root,
    )
    assert record.target == "O1"
    assert record.supervisor == "O2"
    assert record.phase == "init"
    assert record.mutation_boundary_crossed is False
    assert record.owned_resources == []


def test_create_refuses_target_equal_supervisor(tx_root: Path) -> None:
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
            root=tx_root,
        )


def test_create_refuses_existing_record(tx_root: Path) -> None:
    tx_id = transaction.make_tx_id()
    transaction.create(
        tx_id=tx_id,
        target="O1",
        supervisor="O2",
        target_artifact_sha="a" * 40,
        target_artifact_version="0.9.0.dev0",
        main_wheel_sha256="b" * 64,
        sdk_client_wheel_sha256="c" * 64,
        sdk_ui_wheel_sha256="d" * 64,
        root=tx_root,
    )
    with pytest.raises(transaction.TransactionError):
        transaction.create(
            tx_id=tx_id,
            target="O1",
            supervisor="O2",
            target_artifact_sha="a" * 40,
            target_artifact_version="0.9.0.dev0",
            main_wheel_sha256="b" * 64,
            sdk_client_wheel_sha256="c" * 64,
            sdk_ui_wheel_sha256="d" * 64,
            root=tx_root,
        )


def test_load_round_trip(tx_root: Path) -> None:
    tx_id = transaction.make_tx_id()
    record = transaction.create(
        tx_id=tx_id,
        target="O1",
        supervisor="O2",
        target_artifact_sha="a" * 40,
        target_artifact_version="0.9.0.dev0",
        main_wheel_sha256="b" * 64,
        sdk_client_wheel_sha256="c" * 64,
        sdk_ui_wheel_sha256="d" * 64,
        root=tx_root,
    )
    loaded = transaction.load(tx_id, root=tx_root)
    assert loaded.to_dict() == record.to_dict()


def test_load_missing_raises(tx_root: Path) -> None:
    with pytest.raises(transaction.TransactionError):
        transaction.load("promotion-20260808T000000Z-00000000", root=tx_root)


def test_advance_moves_phases_forward(tx_root: Path) -> None:
    tx_id = transaction.make_tx_id()
    record = transaction.create(
        tx_id=tx_id,
        target="O1",
        supervisor="O2",
        target_artifact_sha="a" * 40,
        target_artifact_version="0.9.0.dev0",
        main_wheel_sha256="b" * 64,
        sdk_client_wheel_sha256="c" * 64,
        sdk_ui_wheel_sha256="d" * 64,
        root=tx_root,
    )
    transaction.advance(record, "preflight", root=tx_root)
    assert record.phase == "preflight"
    transaction.advance(record, "candidate_staging", root=tx_root)
    assert record.phase == "candidate_staging"


def test_advance_refuses_backward(tx_root: Path) -> None:
    tx_id = transaction.make_tx_id()
    record = transaction.create(
        tx_id=tx_id,
        target="O1",
        supervisor="O2",
        target_artifact_sha="a" * 40,
        target_artifact_version="0.9.0.dev0",
        main_wheel_sha256="b" * 64,
        sdk_client_wheel_sha256="c" * 64,
        sdk_ui_wheel_sha256="d" * 64,
        root=tx_root,
    )
    transaction.advance(record, "candidate_staging", root=tx_root)
    with pytest.raises(transaction.TransactionError, match="backward"):
        transaction.advance(record, "init", root=tx_root)


def test_advance_allows_terminal_phases_from_anywhere(tx_root: Path) -> None:
    tx_id = transaction.make_tx_id()
    record = transaction.create(
        tx_id=tx_id,
        target="O1",
        supervisor="O2",
        target_artifact_sha="a" * 40,
        target_artifact_version="0.9.0.dev0",
        main_wheel_sha256="b" * 64,
        sdk_client_wheel_sha256="c" * 64,
        sdk_ui_wheel_sha256="d" * 64,
        root=tx_root,
    )
    # failure and rolled_back can be entered from any phase.
    transaction.advance(record, "failure", root=tx_root)
    assert record.phase == "failure"


def test_cross_mutation_boundary_marks_permanent(tx_root: Path) -> None:
    tx_id = transaction.make_tx_id()
    record = transaction.create(
        tx_id=tx_id,
        target="O1",
        supervisor="O2",
        target_artifact_sha="a" * 40,
        target_artifact_version="0.9.0.dev0",
        main_wheel_sha256="b" * 64,
        sdk_client_wheel_sha256="c" * 64,
        sdk_ui_wheel_sha256="d" * 64,
        root=tx_root,
    )
    assert record.mutation_boundary_crossed is False
    transaction.cross_mutation_boundary(record, root=tx_root)
    assert record.mutation_boundary_crossed is True
    # Calling it again is a no-op.
    transaction.cross_mutation_boundary(record, root=tx_root)
    assert record.mutation_boundary_crossed is True


def test_register_owned_tracks_resources(tx_root: Path) -> None:
    tx_id = transaction.make_tx_id()
    record = transaction.create(
        tx_id=tx_id,
        target="O1",
        supervisor="O2",
        target_artifact_sha="a" * 40,
        target_artifact_version="0.9.0.dev0",
        main_wheel_sha256="b" * 64,
        sdk_client_wheel_sha256="c" * 64,
        sdk_ui_wheel_sha256="d" * 64,
        root=tx_root,
    )
    transaction.register_owned(record, "/opt/thing", root=tx_root)
    assert transaction.is_owned(record, "/opt/thing") is True


def test_register_owned_idempotent(tx_root: Path) -> None:
    tx_id = transaction.make_tx_id()
    record = transaction.create(
        tx_id=tx_id,
        target="O1",
        supervisor="O2",
        target_artifact_sha="a" * 40,
        target_artifact_version="0.9.0.dev0",
        main_wheel_sha256="b" * 64,
        sdk_client_wheel_sha256="c" * 64,
        sdk_ui_wheel_sha256="d" * 64,
        root=tx_root,
    )
    transaction.register_owned(record, "/opt/thing", root=tx_root)
    transaction.register_owned(record, "/opt/thing", root=tx_root)
    assert record.owned_resources.count("/opt/thing") == 1


def test_register_owned_rejects_empty(tx_root: Path) -> None:
    tx_id = transaction.make_tx_id()
    record = transaction.create(
        tx_id=tx_id,
        target="O1",
        supervisor="O2",
        target_artifact_sha="a" * 40,
        target_artifact_version="0.9.0.dev0",
        main_wheel_sha256="b" * 64,
        sdk_client_wheel_sha256="c" * 64,
        sdk_ui_wheel_sha256="d" * 64,
        root=tx_root,
    )
    with pytest.raises(transaction.TransactionError):
        transaction.register_owned(record, "", root=tx_root)


def test_is_owned_matches_by_realpath(tx_root: Path, tmp_path: Path) -> None:
    """is_owned must resolve symlinks before comparing."""
    tx_id = transaction.make_tx_id()
    record = transaction.create(
        tx_id=tx_id,
        target="O1",
        supervisor="O2",
        target_artifact_sha="a" * 40,
        target_artifact_version="0.9.0.dev0",
        main_wheel_sha256="b" * 64,
        sdk_client_wheel_sha256="c" * 64,
        sdk_ui_wheel_sha256="d" * 64,
        root=tx_root,
    )
    target = tmp_path / "candidate"
    target.mkdir()
    link = tmp_path / "current"
    os.symlink(target, link)
    transaction.register_owned(record, str(target), root=tx_root)
    # is_owned via the symlink should still return True.
    assert transaction.is_owned(record, str(link)) is True


def test_complete_marks_committed(tx_root: Path) -> None:
    tx_id = transaction.make_tx_id()
    record = transaction.create(
        tx_id=tx_id,
        target="O1",
        supervisor="O2",
        target_artifact_sha="a" * 40,
        target_artifact_version="0.9.0.dev0",
        main_wheel_sha256="b" * 64,
        sdk_client_wheel_sha256="c" * 64,
        sdk_ui_wheel_sha256="d" * 64,
        root=tx_root,
    )
    transaction.complete(record, root=tx_root)
    assert record.phase == "tx_committed"


def test_fail_record_marks_failure(tx_root: Path) -> None:
    tx_id = transaction.make_tx_id()
    record = transaction.create(
        tx_id=tx_id,
        target="O1",
        supervisor="O2",
        target_artifact_sha="a" * 40,
        target_artifact_version="0.9.0.dev0",
        main_wheel_sha256="b" * 64,
        sdk_client_wheel_sha256="c" * 64,
        sdk_ui_wheel_sha256="d" * 64,
        root=tx_root,
    )
    transaction.fail_record(record, "test failure", root=tx_root)
    assert record.phase == "failure"
    assert record.rollback_reason == "test failure"


def test_record_persists_to_disk(tx_root: Path) -> None:
    """The record is written to disk atomically and readable as JSON."""
    tx_id = transaction.make_tx_id()
    record = transaction.create(
        tx_id=tx_id,
        target="O1",
        supervisor="O2",
        target_artifact_sha="a" * 40,
        target_artifact_version="0.9.0.dev0",
        main_wheel_sha256="b" * 64,
        sdk_client_wheel_sha256="c" * 64,
        sdk_ui_wheel_sha256="d" * 64,
        root=tx_root,
    )
    path = transaction.transaction_path(tx_root, tx_id)
    blob = json.loads(path.read_text())
    assert blob["target"] == "O1"
    assert blob["supervisor"] == "O2"
    assert blob["tx_id"] == tx_id
    assert blob["phase"] == "init"
    assert blob["mutation_boundary_crossed"] is False


def test_unknown_keys_in_record_are_rejected(tmp_path: Path) -> None:
    """A record with unknown keys must not be loadable."""
    tx_id = transaction.make_tx_id()
    path = tmp_path / "transactions" / tx_id / "transaction.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"tx_id": tx_id, "anomaly": "evil"}))
    with pytest.raises(transaction.TransactionError):
        transaction.load(tx_id, root=tmp_path / "transactions")
