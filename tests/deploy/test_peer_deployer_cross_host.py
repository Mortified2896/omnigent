"""Cross-host evidence cannot bypass the canonical promotion safety boundary."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from deploy.scripts.peer_deployer import cross_host, identity


@pytest.fixture
def evidence():
    supervisor = {
        "instance": "O2",
        "artifact_sha": "a" * 40,
        "artifact_version": "1.0.0",
        "server": {
            "unit": "omnigent-production.service",
            "active_state": "active",
            "main_pid": 492,
            "active_enter_timestamp_monotonic": 100,
        },
        "host": {
            "unit": "omnigent-production-host.service",
            "active_state": "active",
            "main_pid": 493,
            "active_enter_timestamp_monotonic": 101,
        },
    }
    source = {
        "hostname": "old-host",
        "machine_id": "1" * 32,
        "observed_at": 1000,
        "role": "source",
        "target": "O1",
        "supervisor": "O2",
        "supervisor_baseline": supervisor,
        "supervisor_health": True,
        "target_health": True,
        "storage_guard": {"active": True, "latched": False},
        "root_free_bytes": 100 * 1024**3,
        "unresolved_transactions": [],
    }
    candidate = {
        "hostname": "new-host",
        "machine_id": "2" * 32,
        "observed_at": 1000,
        "role": "isolated-candidate",
        "source_sha": "b" * 40,
        "acceptance_sha256": "c" * 64,
        "acceptance_failures": [],
        "target_db_schema": "revision",
        "database_schemas": ["revision"],
        "database_integrity": True,
        "data_mount": {"target": "/srv", "uuid": "data-uuid"},
        "data_free_bytes": 100 * 1024**3,
        "candidate_server_active": True,
        "candidate_host_active": True,
        "unresolved_transactions": [],
        "canonical_units": dict.fromkeys(
            (
                "omnigent.service",
                "omnigent-host.service",
                "omnigent-production.service",
                "omnigent-production-host.service",
            ),
            "unknown",
        ),
    }
    expected = {
        "source": {"hostname": "old-host", "machine_id": "1" * 32},
        "candidate": {"hostname": "new-host", "machine_id": "2" * 32},
        "source_sha": "b" * 40,
        "acceptance_sha256": "c" * 64,
        "supervisor_baseline": copy.deepcopy(supervisor),
        "data_mount": "/srv",
        "data_uuid": "data-uuid",
    }
    return source, candidate, expected


def report(evidence):
    return cross_host.assess(*evidence, target="O1", supervisor="O2", now=1010)


def test_successful_observations_never_authorize_mutation(evidence):
    result = report(evidence)
    assert result["observations_passed"]
    assert result["ready_for_mutation"] is False
    assert any("writer fencing" in gate for gate in result["remaining_gates"])
    assert any("paired recovery" in gate for gate in result["remaining_gates"])


def test_self_upgrade_refused_before_reading_evidence():
    with pytest.raises(identity.IdentityError, match="target == supervisor"):
        cross_host.assess({}, {}, {}, target="O1", supervisor="O1")


def test_reversed_migration_is_not_silently_accepted(evidence):
    with pytest.raises(cross_host.CrossHostError, match="TARGET=O1"):
        cross_host.assess(*evidence, target="O2", supervisor="O1")


@pytest.mark.parametrize("role", [0, 1])
@pytest.mark.parametrize(
    "field,value",
    [
        ("hostname", "unrelated"),
        ("machine_id", "3" * 32),
        ("observed_at", 800),
        ("observed_at", 1020),
        ("unresolved_transactions", ["promotion-unresolved"]),
    ],
)
def test_host_identity_freshness_and_transaction_failures(evidence, role, field, value):
    evidence[role][field] = value
    assert not report(evidence)["observations_passed"]


@pytest.mark.parametrize("field,value", [("active", False), ("latched", True)])
def test_source_guard_is_not_replaced_by_candidate_headroom(evidence, field, value):
    evidence[0]["storage_guard"][field] = value
    failures = [item["name"] for item in report(evidence)["checks"] if not item["ok"]]
    assert "source.storage_guard" in failures


def test_supervisor_process_drift_refused(evidence):
    evidence[0]["supervisor_baseline"]["host"]["main_pid"] += 1
    assert not report(evidence)["observations_passed"]


def test_mac_or_candidate_cannot_impersonate_o2(evidence):
    evidence[0]["supervisor_baseline"]["instance"] = "Mac"
    assert not report(evidence)["observations_passed"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_sha", "d" * 40),
        ("acceptance_sha256", "d" * 64),
        ("acceptance_failures", ["wheel hash mismatch"]),
        ("database_schemas", ["wrong"]),
        ("database_integrity", False),
        ("data_mount", {"target": "/srv", "uuid": "root-disk"}),
        ("data_free_bytes", 1024),
        ("candidate_host_active", False),
        ("canonical_units", {}),
        ("role", "O1"),
    ],
)
def test_candidate_artifact_storage_and_writer_gates(evidence, field, value):
    evidence[1][field] = value
    assert not report(evidence)["observations_passed"]


def test_active_canonical_writer_refused(evidence):
    evidence[1]["canonical_units"]["omnigent.service"] = "active"
    assert not report(evidence)["observations_passed"]


def test_assessment_does_not_write_or_change_input(evidence, tmp_path: Path):
    before = copy.deepcopy(evidence)
    report(evidence)
    assert evidence == before
    assert list(tmp_path.iterdir()) == []


def test_no_promotion_interface_is_exposed():
    with pytest.raises(SystemExit) as exit_info:
        cross_host.main(["promote"])
    assert exit_info.value.code == 2


def test_transaction_observation_uses_canonical_reconciliation(monkeypatch, tmp_path):
    monkeypatch.setattr(cross_host.transaction, "DEFAULT_TX_ROOT", tmp_path)
    calls = []
    monkeypatch.setattr(
        cross_host.host_promotion, "_no_live_transactions", lambda: calls.append(True)
    )
    assert cross_host._transactions() == []
    assert calls == [True]


def test_transaction_observation_preserves_canonical_refusal(monkeypatch, tmp_path):
    monkeypatch.setattr(cross_host.transaction, "DEFAULT_TX_ROOT", tmp_path)

    def refuse():
        raise cross_host.host_promotion.PromotionError("unresolved transaction")

    monkeypatch.setattr(cross_host.host_promotion, "_no_live_transactions", refuse)
    assert cross_host._transactions() == ["unresolved transaction"]
