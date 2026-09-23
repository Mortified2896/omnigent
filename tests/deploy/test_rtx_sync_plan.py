"""Portable planner tests: no server, credentials, model calls or host mutations."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[2] / "deploy/scripts/peer_deployer/sync_plan.py"
SPEC = importlib.util.spec_from_file_location("_rtx_sync_plan_under_test", MODULE)
assert SPEC is not None and SPEC.loader is not None
sync = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = sync
SPEC.loader.exec_module(sync)

NOW = 1_800_000_000.0
SHA1 = "a" * 40
SHA2 = "b" * 40
KEY = "070ac623-a939-4797-8d33-355747381444"


@pytest.fixture
def evidence():
    """Construct trusted synthetic evidence, never a real installation record."""
    acceptance = {
        "source_sha": SHA1,
        "runtime": f"/srv/omnigent/releases/{SHA1}",
        "schema": "revision-1",
        "schema_policy": "same-schema",
        "checks": dict.fromkeys(sync.ACCEPTANCE_CHECKS, True),
        "hashes": {"venv/build-info": "c" * 64},
    }
    digest = sync.acceptance_digest(acceptance)
    source = sync.PeerObservation(
        instance="O1", source_sha=SHA1, release_digest=digest,
        package_version="0.5.1", upstream_version="0.5.0", upstream_ref="d" * 40,
        generation="server-101:host-102:boot-1", database_id="1" * 32,
        schema="revision-1", observed_at=NOW, healthy=True, active_work=2,
        live_validated_digest=digest,
        live_validated_generation="server-101:host-102:boot-1",
    )
    target = sync.PeerObservation(
        instance="O2", source_sha=SHA2, release_digest="e" * 64,
        package_version="0.5.1", generation="server-201:host-202:boot-1",
        database_id="2" * 32, schema="revision-1", observed_at=NOW,
        healthy=True, active_work=0,
    )
    controller = sync.ControllerReadiness(
        observed_at=NOW, independent=True, idle_guard_ready=True,
        write_fence_ready=True, rollback_ready=True, transaction_idle=True,
        verified_release_digest=digest,
    )
    return source, target, acceptance, controller


def test_ready_and_no_ai_required(evidence):
    plan = sync.plan_sync(*evidence, now=NOW)
    assert plan.status == "ready"
    assert plan.blockers == ()
    assert plan.display()["can_sync"] is True
    assert evidence[0].active_work == 2  # O1 can serve ordinary work throughout.
    sync.validate_sync_request(plan.request(KEY), plan, now=NOW + 1)


def test_display_separates_provenance_from_package_and_custom_sha(evidence):
    source = evidence[0]
    shown = source.display()
    assert shown["official_version"] == "0.5.0"
    assert shown["package_version"] == "0.5.1"
    assert shown["custom_version"] == f"git-{SHA1[:12]}"
    assert shown["source_sha"] == SHA1
    assert "database_id" not in shown and "generation" not in shown
    unknown = replace(source, upstream_version=None, upstream_ref=None).display()
    assert unknown["official_version"] is None
    assert unknown["package_version"] == "0.5.1"


@pytest.mark.parametrize("bad", [None, "abc123", "A" * 40, "a" * 39, "a" * 41, 123])
def test_unknown_or_short_sha_is_not_a_release(evidence, bad):
    source, target, acceptance, controller = evidence
    source = replace(source, source_sha=bad)
    assert source.display()["custom_version"] is None
    plan = sync.plan_sync(source, target, acceptance, controller, now=NOW)
    assert "source_source_sha" in plan.blockers
    assert plan.status == "blocked"


@pytest.mark.parametrize("field", [
    "independent", "idle_guard_ready", "write_fence_ready", "rollback_ready", "transaction_idle"
])
@pytest.mark.parametrize("bad", [False, None, "true", 1])
def test_capabilities_fail_closed(evidence, field, bad):
    source, target, acceptance, controller = evidence
    plan = sync.plan_sync(source, target, acceptance, replace(controller, **{field: bad}), now=NOW)
    assert f"controller_{field}" in plan.blockers


@pytest.mark.parametrize("bad", [
    None, -1, NOW - 60, NOW - 61, NOW + 6, float("nan"), float("inf"), True, 10**400
])
def test_stale_or_invalid_evidence(evidence, bad):
    source, target, acceptance, controller = evidence
    plan = sync.plan_sync(replace(source, observed_at=bad), target, acceptance, controller, now=NOW)
    assert "source_freshness" in plan.blockers


@pytest.mark.parametrize("bad", [None, False, "true", 1])
def test_target_health_must_be_proven(evidence, bad):
    source, target, acceptance, controller = evidence
    plan = sync.plan_sync(source, replace(target, healthy=bad), acceptance, controller, now=NOW)
    assert "target_health" in plan.blockers


@pytest.mark.parametrize("bad", [None, 1, -1, False, "0", 0.0])
def test_target_idle_is_not_guessed(evidence, bad):
    source, target, acceptance, controller = evidence
    plan = sync.plan_sync(source, replace(target, active_work=bad), acceptance, controller, now=NOW)
    assert "target_not_idle" in plan.blockers


def test_no_implicit_capabilities(evidence):
    source, target, acceptance, _ = evidence
    plan = sync.plan_sync(source, target, acceptance, sync.ControllerReadiness(), now=NOW)
    assert plan.status == "blocked"
    assert "artifact_not_verified" in plan.blockers
    with pytest.raises(sync.SyncRefused):
        plan.request(KEY)


@pytest.mark.parametrize("field,value,reason", [
    ("instance", "O1", "target_instance"),
    ("database_id", "1" * 32, "database_identity_collision"),
    ("schema", "revision-2", "peer_schema_mismatch"),
    ("schema", None, "target_schema"),
    ("generation", None, "target_generation"),
])
def test_peer_boundaries(evidence, field, value, reason):
    source, target, acceptance, controller = evidence
    plan = sync.plan_sync(
        source, replace(target, **{field: value}), acceptance, controller, now=NOW
    )
    assert reason in plan.blockers


def test_same_package_version_does_not_mean_same_custom_build(evidence):
    assert evidence[0].package_version == evidence[1].package_version
    assert sync.plan_sync(*evidence, now=NOW).status == "ready"


def test_same_sha_different_artifact_is_not_already_current(evidence):
    source, target, acceptance, controller = evidence
    plan = sync.plan_sync(source, replace(target, source_sha=SHA1), acceptance, controller, now=NOW)
    assert plan.status == "ready"


def test_same_exact_release_is_noop_even_while_busy(evidence):
    source, target, acceptance, _ = evidence
    target = replace(target, source_sha=SHA1, release_digest=source.release_digest, active_work=4)
    plan = sync.plan_sync(source, target, acceptance, sync.ControllerReadiness(), now=NOW)
    assert plan.status == "already_current"
    assert plan.display()["can_sync"] is False
    with pytest.raises(sync.SyncRefused):
        plan.request(KEY)


def test_unknown_digests_never_compare_as_current(evidence):
    source, target, acceptance, controller = evidence
    plan = sync.plan_sync(
        replace(source, release_digest=None),
        replace(target, source_sha=SHA1, release_digest=None),
        acceptance, controller, now=NOW,
    )
    assert plan.status == "blocked"


def test_live_source_acceptance_is_bound_to_process_and_artifact(evidence):
    source, target, acceptance, controller = evidence
    plan = sync.plan_sync(
        replace(source, live_validated_digest="f" * 64, live_validated_generation="old"),
        target, acceptance, controller, now=NOW,
    )
    assert "source_live_validation_missing" in plan.blockers
    assert "source_live_validation_stale" in plan.blockers


@pytest.mark.parametrize("field,value,reason", [
    ("source_sha", SHA2, "acceptance_source_mismatch"),
    ("schema_policy", "forward-only-approved", "unsupported_schema_policy"),
    ("schema", "revision-2", "acceptance_schema_mismatch"),
    ("checks", {}, "acceptance_checks_incomplete"),
    ("checks", {name: 1 for name in sync.ACCEPTANCE_CHECKS}, "acceptance_checks_incomplete"),
    ("hashes", {}, "acceptance_hashes_incomplete"),
    ("hashes", {"file": "short"}, "acceptance_hashes_incomplete"),
])
def test_reject_incomplete_or_incompatible_acceptance(evidence, field, value, reason):
    source, target, acceptance, controller = evidence
    acceptance = {**acceptance, field: value}
    plan = sync.plan_sync(source, target, acceptance, controller, now=NOW)
    assert reason in plan.blockers
    assert "acceptance_digest_mismatch" in plan.blockers


def test_digest_matches_existing_rtx_canonical_format(evidence):
    acceptance = {**evidence[2], "notes": "Unicode: \u00fc"}
    canonical = json.dumps(acceptance, sort_keys=True, separators=(",", ":")).encode()
    expected = hashlib.sha256(canonical).hexdigest()
    assert sync.acceptance_digest(acceptance) == expected
    assert sync.acceptance_digest(dict(reversed(list(acceptance.items())))) == expected


@pytest.mark.parametrize("bad", [None, {}, {"notes": float("nan")}, {"notes": object()}])
def test_malformed_acceptance_blocks(evidence, bad):
    source, target, _, controller = evidence
    plan = sync.plan_sync(source, target, bad, controller, now=NOW)
    assert plan.status == "blocked"
    assert "acceptance_digest_mismatch" in plan.blockers


def test_no_mutation_of_acceptance(evidence):
    before = copy.deepcopy(evidence[2])
    sync.plan_sync(*evidence, now=NOW)
    assert evidence[2] == before


@pytest.mark.parametrize("field,value", [
    ("source_sha", "f" * 40), ("release_digest", "f" * 64),
    ("generation", "server-new:host-new"), ("database_id", "3" * 32),
])
def test_stale_click_rejects_target_drift(evidence, field, value):
    source, target, acceptance, controller = evidence
    request = sync.plan_sync(*evidence, now=NOW).request(KEY)
    fresh = sync.plan_sync(
        source, replace(target, **{field: value}), acceptance, controller, now=NOW
    )
    with pytest.raises(sync.SyncRefused, match="changed"):
        sync.validate_sync_request(request, fresh, now=NOW + 1)


def test_stale_click_rejects_source_restart_even_if_revalidated(evidence):
    source, target, acceptance, controller = evidence
    request = sync.plan_sync(*evidence, now=NOW).request(KEY)
    source = replace(source, generation="new", live_validated_generation="new")
    fresh = sync.plan_sync(source, target, acceptance, controller, now=NOW)
    with pytest.raises(sync.SyncRefused, match="changed"):
        sync.validate_sync_request(request, fresh, now=NOW + 1)


@pytest.mark.parametrize("extra", ["command", "url", "path", "target", "force"])
def test_no_arbitrary_operation_fields(evidence, extra):
    plan = sync.plan_sync(*evidence, now=NOW)
    request = {**plan.request(KEY), extra: "anything"}
    with pytest.raises(sync.SyncRefused, match="fields"):
        sync.validate_sync_request(request, plan, now=NOW + 1)


@pytest.mark.parametrize("bad", [NOW, NOW - 1, NOW + 1000, None, True, float("nan"), float("inf")])
def test_invalid_request_expiry(evidence, bad):
    plan = sync.plan_sync(*evidence, now=NOW)
    request = {**plan.request(KEY), "expires_at": bad}
    with pytest.raises(sync.SyncRefused):
        sync.validate_sync_request(request, plan, now=NOW + 1)


@pytest.mark.parametrize("bad", [
    None, "", "latest", "../O1", KEY.upper(), "00000000-0000-0000-0000-000000000000"
])
def test_bad_idempotency_key(evidence, bad):
    plan = sync.plan_sync(*evidence, now=NOW)
    with pytest.raises(sync.SyncRefused):
        plan.request(bad)


def test_expiry_uses_oldest_observation(evidence):
    source, target, acceptance, controller = evidence
    plan = sync.plan_sync(
        source, replace(target, observed_at=NOW - 50), acceptance, controller, now=NOW
    )
    assert plan.expires_at == NOW + 10
    with pytest.raises(sync.SyncRefused):
        sync.validate_sync_request(plan.request(KEY), plan, now=NOW + 11)


@pytest.mark.parametrize("bad", [None, True, -1, float("nan"), float("inf"), 10**400])
def test_bad_clock(evidence, bad):
    with pytest.raises(ValueError):
        sync.plan_sync(*evidence, now=bad)


def test_wrong_operation_and_blocked_fresh_plan(evidence):
    plan = sync.plan_sync(*evidence, now=NOW)
    request = plan.request(KEY)
    with pytest.raises(sync.SyncRefused, match="not ready"):
        sync.validate_sync_request({**request, "operation": "restart-o1"}, plan, now=NOW + 1)
    blocked = replace(plan, status="blocked")
    with pytest.raises(sync.SyncRefused, match="not ready"):
        sync.validate_sync_request(request, blocked, now=NOW + 1)


def test_expired_fresh_plan_is_rejected_even_with_extended_request(evidence):
    plan = sync.plan_sync(*evidence, now=NOW)
    request = {**plan.request(KEY), "expires_at": NOW + 100}
    with pytest.raises(sync.SyncRefused, match="evidence expired"):
        sync.validate_sync_request(request, plan, now=NOW + 61)
