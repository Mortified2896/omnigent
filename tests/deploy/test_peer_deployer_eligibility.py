"""Focused tests for the reconciliation-aware eligibility classifier."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from peer_deployer import eligibility, transaction


def _seed(tmp: Path, *, tx_id: str, phase: str, overlay: dict | None = None) -> Path:
    tx_dir = tmp / tx_id
    tx_dir.mkdir()
    (tx_dir / "transaction.json").write_text(json.dumps({"tx_id": tx_id, "phase": phase, "target": "O1", "supervisor": "O2"}))
    if overlay is not None:
        (tx_dir / "reconciliation.json").write_text(json.dumps(overlay))
    return tx_dir


def test_terminal_does_not_block(tmp_path: Path) -> None:
    _seed(tmp_path, tx_id="promotion-20260101T000000Z-00000000", phase="tx_committed")
    decisions = eligibility.deployment_eligibility(tmp_path)
    assert len(decisions) == 1
    assert decisions[0].classification == eligibility.CLASS_TERMINAL
    assert decisions[0].blocks is False
    eligibility.assert_no_blocking_transactions(tmp_path)


def test_active_unresolved_blocks(tmp_path: Path) -> None:
    _seed(tmp_path, tx_id="promotion-20260101T000000Z-00000001", phase="candidate_staging")
    with pytest.raises(transaction.TransactionError):
        eligibility.assert_no_blocking_transactions(tmp_path)


def test_validly_reconciled_does_not_block(tmp_path: Path) -> None:
    tx_id = "promotion-20260101T000000Z-00000002"
    _seed(tmp_path, tx_id=tx_id, phase="candidate_staging",
          overlay={"tx_id": tx_id, "phase": "candidate_staging", "classification": eligibility.CLASS_VALIDLY_RECONCILED,
                   "blocks": False, "reason": "host_crash; staging root cleaned"})
    decisions = eligibility.deployment_eligibility(tmp_path)
    assert decisions[0].classification == eligibility.CLASS_VALIDLY_RECONCILED
    assert decisions[0].blocks is False
    eligibility.assert_no_blocking_transactions(tmp_path)


def test_inconsistent_overlay_blocks(tmp_path: Path) -> None:
    tx_id = "promotion-20260101T000000Z-00000003"
    _seed(tmp_path, tx_id=tx_id, phase="candidate_staging",
          overlay={"tx_id": "different-id", "phase": "candidate_staging", "classification": eligibility.CLASS_VALIDLY_RECONCILED})
    with pytest.raises(transaction.TransactionError):
        eligibility.assert_no_blocking_transactions(tmp_path)


def test_corrupt_transaction_blocks(tmp_path: Path) -> None:
    (tmp_path / "promotion-20260101T000000Z-deadbeef").mkdir()
    (tmp_path / "promotion-20260101T000000Z-deadbeef" / "transaction.json").write_text("{not-json")
    with pytest.raises(transaction.TransactionError):
        eligibility.assert_no_blocking_transactions(tmp_path)


def test_classification_does_not_mutate_record(tmp_path: Path) -> None:
    tx_id = "promotion-20260101T000000Z-deadcafe"
    p = _seed(tmp_path, tx_id=tx_id, phase="candidate_staging",
              overlay={"tx_id": tx_id, "phase": "candidate_staging", "classification": eligibility.CLASS_VALIDLY_RECONCILED, "blocks": False})
    before = (p / "transaction.json").read_bytes()
    eligibility.classify_transaction(p)
    after = (p / "transaction.json").read_bytes()
    assert before == after


def test_untrusted_reconciliation_class_blocks(tmp_path: Path) -> None:
    tx_id = "promotion-20260101T000000Z-deadbead"
    _seed(tmp_path, tx_id=tx_id, phase="candidate_staging",
          overlay={"tx_id": tx_id, "phase": "candidate_staging", "classification": "MALICIOUS_OVERLAY_CLASS"})
    with pytest.raises(transaction.TransactionError):
        eligibility.assert_no_blocking_transactions(tmp_path)