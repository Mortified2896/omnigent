"""Authoritative transaction eligibility decisions for the Control Room deployer.

This module is the single place that decides whether historical transaction
state blocks a new deployment.  Callers must not scan ``phase`` directly.
Historical records are immutable: classification never rewrites them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from . import transaction

TERMINAL_PHASES = {"tx_committed", "rolled_back", "failure"}
CLASS_TERMINAL = "TERMINAL"
CLASS_ACTIVE_UNRESOLVED = "ACTIVE_UNRESOLVED"
CLASS_VALIDLY_RECONCILED = "VALIDLY_RECONCILED"
CLASS_INCONSISTENT_RECONCILIATION = "INCONSISTENT_RECONCILIATION"
CLASS_CORRUPT = "CORRUPT"


@dataclass(frozen=True)
class EligibilityDecision:
    tx_id: str
    classification: str
    blocks: bool
    phase: str = ""
    reason: str = ""


def _read_overlay(tx_dir: Path) -> dict[str, Any] | None:
    for name in ("reconciliation.json", "reconciliation-overlay.json", "RECONCILIATION.json"):
        p = tx_dir / name
        if p.is_file():
            blob = json.loads(p.read_text())
            if not isinstance(blob, dict):
                raise ValueError(f"{p} is not a JSON object")
            return blob
    return None


def classify_transaction(tx_dir: Path) -> EligibilityDecision:
    tx_id = tx_dir.name
    p = tx_dir / "transaction.json"
    if not p.is_file():
        return EligibilityDecision(tx_id, CLASS_CORRUPT, True, reason="missing transaction.json")
    before = p.read_bytes()
    try:
        blob = json.loads(before.decode())
        if not isinstance(blob, dict):
            raise ValueError("transaction is not an object")
        phase = str(blob.get("phase", ""))
    except Exception as exc:
        return EligibilityDecision(tx_id, CLASS_CORRUPT, True, reason=f"corrupt transaction: {exc}")

    if phase in TERMINAL_PHASES:
        return EligibilityDecision(tx_id, CLASS_TERMINAL, False, phase=phase)

    try:
        overlay = _read_overlay(tx_dir)
    except Exception as exc:
        return EligibilityDecision(tx_id, CLASS_INCONSISTENT_RECONCILIATION, True, phase=phase, reason=str(exc))

    if overlay:
        cls = str(overlay.get("classification") or overlay.get("class") or "")
        overlay_tx = str(overlay.get("tx_id") or tx_id)
        reconciled_phase = str(overlay.get("phase") or overlay.get("original_phase") or phase)
        if overlay_tx != tx_id:
            return EligibilityDecision(tx_id, CLASS_INCONSISTENT_RECONCILIATION, True, phase=phase, reason="overlay tx_id mismatch")
        if reconciled_phase != phase:
            return EligibilityDecision(tx_id, CLASS_INCONSISTENT_RECONCILIATION, True, phase=phase, reason="overlay phase mismatch")
        if cls == CLASS_VALIDLY_RECONCILED and overlay.get("blocks") is not True:
            if p.read_bytes() != before:
                return EligibilityDecision(tx_id, CLASS_INCONSISTENT_RECONCILIATION, True, phase=phase, reason="transaction mutated during classification")
            return EligibilityDecision(tx_id, CLASS_VALIDLY_RECONCILED, False, phase=phase, reason=str(overlay.get("reason", "")))
        return EligibilityDecision(tx_id, CLASS_INCONSISTENT_RECONCILIATION, True, phase=phase, reason=f"untrusted reconciliation class {cls!r}")

    return EligibilityDecision(tx_id, CLASS_ACTIVE_UNRESOLVED, True, phase=phase, reason="non-terminal without valid reconciliation overlay")


def _resolve_root(root: Path | None) -> Path:
    if root is None:
        return transaction.DEFAULT_TX_ROOT
    return root


def deployment_eligibility(root: Path = transaction.DEFAULT_TX_ROOT) -> list[EligibilityDecision]:
    if not root.is_dir():
        return []
    return [classify_transaction(d) for d in sorted(_resolve_root(root).iterdir()) if d.is_dir()]


def blocking_decisions(root: Path = transaction.DEFAULT_TX_ROOT) -> list[EligibilityDecision]:
    return [d for d in deployment_eligibility(root) if d.blocks]


def assert_no_blocking_transactions(root: Path = transaction.DEFAULT_TX_ROOT) -> None:
    bad = blocking_decisions(_resolve_root(root))
    if bad:
        detail = ", ".join(f"{d.tx_id}:{d.classification}:{d.phase}:{d.reason}" for d in bad)
        raise transaction.TransactionError("blocking transaction state: " + detail)
