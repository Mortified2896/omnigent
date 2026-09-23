from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from deploy.scripts.release_controller.core import (
    ActivationPhase,
    ActivationRefused,
    CandidateEvidence,
    ControllerReadiness,
    ReleaseIdentity,
    RuntimeObservation,
    automatic_rollback_allowed,
    plan_activation,
    transition_allowed,
    validate_activation_request,
)

NOW = 1000.0
CURRENT_SHA = "a" * 40
CANDIDATE_SHA = "b" * 40
PREVIOUS_SHA = "c" * 40
CURRENT_DIGEST = "1" * 64
CANDIDATE_DIGEST = "2" * 64
PREVIOUS_DIGEST = "3" * 64
SCHEMA = "schema-1"
KEY = "123e4567-e89b-42d3-a456-426614174000"


def release(sha: str, digest: str, *, schema: str = SCHEMA) -> ReleaseIdentity:
    return ReleaseIdentity(
        source_sha=sha,
        acceptance_digest=digest,
        package_version="0.17.4",
        upstream_version="0.17.0",
        upstream_ref="omnigent-ai/omnigent@" + "d" * 40,
        schema_revision=schema,
    )


def current(**changes: Any) -> RuntimeObservation:
    value = RuntimeObservation(
        release=release(CURRENT_SHA, CURRENT_DIGEST),
        process_generation="server-101:host-201",
        state_identity="state-identity",
        state_generation="state-generation",
        observed_at=NOW,
        healthy=True,
        active_work=0,
        writes_fenced=False,
    )
    return replace(value, **changes)


def candidate(**changes: Any) -> CandidateEvidence:
    value = CandidateEvidence(
        release=release(CANDIDATE_SHA, CANDIDATE_DIGEST),
        observed_at=NOW,
        bytes_verified=True,
        isolated_boot_ok=True,
        health_ok=True,
        build_identity_ok=True,
        frontend_ok=True,
        rollback_from_current_ok=True,
    )
    return replace(value, **changes)


def controller(**changes: Any) -> ControllerReadiness:
    value = ControllerReadiness(
        observed_at=NOW,
        independent=True,
        serialized=True,
        drain_ready=True,
        write_fence_ready=True,
        backup_ready=True,
        restart_ready=True,
        rollback_ready=True,
        previous_release_retained=True,
    )
    return replace(value, **changes)


def ready_plan():
    return plan_activation(
        current(),
        candidate(),
        release(PREVIOUS_SHA, PREVIOUS_DIGEST),
        controller(),
        now=NOW,
    )


def test_ready_plan_pins_current_candidate_and_previous() -> None:
    plan = ready_plan()
    assert plan.status == "ready"
    assert not plan.blockers
    assert plan.expected()["current"]["release"]["source_sha"] == CURRENT_SHA
    assert plan.expected()["candidate"]["source_sha"] == CANDIDATE_SHA
    assert plan.expected()["previous"]["source_sha"] == PREVIOUS_SHA


def test_display_keeps_upstream_and_custom_identity_separate() -> None:
    shown = release(CANDIDATE_SHA, CANDIDATE_DIGEST).display()
    assert shown["official_version"] == "0.17.0"
    assert shown["package_version"] == "0.17.4"
    assert shown["custom_version"] == "git-" + CANDIDATE_SHA[:12]
    assert shown["source_sha"] == CANDIDATE_SHA


def test_unknown_provenance_does_not_invent_official_version() -> None:
    shown = replace(
        release(CANDIDATE_SHA, CANDIDATE_DIGEST),
        upstream_version=None,
        upstream_ref=None,
    ).display()
    assert shown["official_version"] is None
    assert shown["upstream_ref"] is None
    assert shown["custom_version"] is not None


@pytest.mark.parametrize(
    ("mutation", "blocker"),
    [
        (lambda: current(release=ReleaseIdentity()), "current_release_identity"),
        (lambda: current(process_generation=None), "process_generation"),
        (lambda: current(state_identity=None), "state_identity"),
        (lambda: current(state_generation=None), "state_generation"),
        (lambda: current(observed_at=NOW - 61), "current_freshness"),
        (lambda: current(healthy=False), "current_health"),
        (lambda: current(writes_fenced=True), "current_writes_fenced_or_unknown"),
        (lambda: current(writes_fenced=None), "current_writes_fenced_or_unknown"),
        (lambda: current(active_work=None), "active_work_unknown"),
    ],
)
def test_current_evidence_fails_closed(mutation, blocker: str) -> None:
    plan = plan_activation(
        mutation(),
        candidate(),
        release(PREVIOUS_SHA, PREVIOUS_DIGEST),
        controller(),
        now=NOW,
    )
    assert plan.status == "blocked"
    assert blocker in plan.blockers


@pytest.mark.parametrize(
    ("field", "blocker"),
    [
        ("bytes_verified", "candidate_bytes_verified"),
        ("isolated_boot_ok", "candidate_isolated_boot_ok"),
        ("health_ok", "candidate_health_ok"),
        ("build_identity_ok", "candidate_build_identity_ok"),
        ("frontend_ok", "candidate_frontend_ok"),
        ("rollback_from_current_ok", "candidate_rollback_from_current_ok"),
    ],
)
def test_candidate_checks_are_required(field: str, blocker: str) -> None:
    plan = plan_activation(
        current(),
        candidate(**{field: False}),
        release(PREVIOUS_SHA, PREVIOUS_DIGEST),
        controller(),
        now=NOW,
    )
    assert blocker in plan.blockers


@pytest.mark.parametrize(
    "field",
    [
        "independent",
        "serialized",
        "drain_ready",
        "write_fence_ready",
        "backup_ready",
        "restart_ready",
        "rollback_ready",
        "previous_release_retained",
    ],
)
def test_controller_capabilities_default_to_blocking(field: str) -> None:
    plan = plan_activation(
        current(),
        candidate(),
        release(PREVIOUS_SHA, PREVIOUS_DIGEST),
        controller(**{field: False}),
        now=NOW,
    )
    assert f"controller_{field}" in plan.blockers


def test_schema_change_is_not_automatically_activatable() -> None:
    plan = plan_activation(
        current(),
        candidate(release=release(CANDIDATE_SHA, CANDIDATE_DIGEST, schema="schema-2")),
        release(PREVIOUS_SHA, PREVIOUS_DIGEST),
        controller(),
        now=NOW,
    )
    assert "schema_mismatch" in plan.blockers


def test_previous_release_schema_must_match_current() -> None:
    plan = plan_activation(
        current(),
        candidate(),
        release(PREVIOUS_SHA, PREVIOUS_DIGEST, schema="schema-old"),
        controller(),
        now=NOW,
    )
    assert "previous_schema_mismatch" in plan.blockers


def test_already_current_requires_same_acceptance_not_only_same_sha() -> None:
    same = candidate(release=release(CURRENT_SHA, CURRENT_DIGEST))
    plan = plan_activation(
        current(),
        same,
        release(PREVIOUS_SHA, PREVIOUS_DIGEST),
        controller(),
        now=NOW,
    )
    assert plan.status == "already_current"

    changed_acceptance = candidate(release=release(CURRENT_SHA, CANDIDATE_DIGEST))
    plan = plan_activation(
        current(),
        changed_acceptance,
        release(PREVIOUS_SHA, PREVIOUS_DIGEST),
        controller(),
        now=NOW,
    )
    assert plan.status == "ready"


def test_plan_can_be_ready_while_work_exists_because_activation_must_drain() -> None:
    plan = plan_activation(
        current(active_work=3),
        candidate(),
        release(PREVIOUS_SHA, PREVIOUS_DIGEST),
        controller(drain_ready=True),
        now=NOW,
    )
    assert plan.status == "ready"


def test_request_is_compare_and_swap_pinned_and_expires() -> None:
    plan = ready_plan()
    request = plan.request(KEY)
    assert request["expected"] == plan.expected()
    assert request["operation"] == "activate-release"

    validate_activation_request(request, ready_plan(), now=NOW + 1)

    drifted = plan_activation(
        current(state_generation="new-state-generation"),
        candidate(),
        release(PREVIOUS_SHA, PREVIOUS_DIGEST),
        controller(),
        now=NOW + 1,
    )
    with pytest.raises(ActivationRefused, match="changed"):
        validate_activation_request(request, drifted, now=NOW + 1)


def test_request_rejects_browser_selected_fields() -> None:
    request = ready_plan().request(KEY) | {"force": True}
    with pytest.raises(ActivationRefused, match="unsupported"):
        validate_activation_request(request, ready_plan(), now=NOW + 1)


def test_request_rejects_non_uuid4_key() -> None:
    with pytest.raises(ActivationRefused, match="idempotency"):
        ready_plan().request("not-a-key")


@pytest.mark.parametrize(
    ("current_phase", "target_phase", "allowed"),
    [
        (ActivationPhase.PLANNED, ActivationPhase.FENCING, True),
        (ActivationPhase.FENCING, ActivationPhase.DRAINING, True),
        (ActivationPhase.DRAINING, ActivationPhase.QUIESCED, True),
        (ActivationPhase.QUIESCED, ActivationPhase.BACKED_UP, True),
        (ActivationPhase.BACKED_UP, ActivationPhase.SWITCHED, True),
        (ActivationPhase.SWITCHED, ActivationPhase.STARTING, True),
        (ActivationPhase.STARTING, ActivationPhase.VERIFYING, True),
        (ActivationPhase.VERIFYING, ActivationPhase.COMMITTED, True),
        (ActivationPhase.COMMITTED, ActivationPhase.REOPENING_WRITES, True),
        (ActivationPhase.COMMITTED, ActivationPhase.ROLLING_BACK, True),
        (ActivationPhase.REOPENING_WRITES, ActivationPhase.WRITES_REOPENED, True),
        (ActivationPhase.REOPENING_WRITES, ActivationPhase.ROLLING_BACK, False),
        (ActivationPhase.WRITES_REOPENED, ActivationPhase.RECOVERY_REQUIRED, True),
        (ActivationPhase.STARTING, ActivationPhase.ROLLING_BACK, True),
        (ActivationPhase.WRITES_REOPENED, ActivationPhase.ROLLING_BACK, False),
        (ActivationPhase.ROLLED_BACK, ActivationPhase.STARTING, False),
    ],
)
def test_state_machine(current_phase, target_phase, allowed: bool) -> None:
    assert transition_allowed(current_phase, target_phase) is allowed


def test_automatic_rollback_is_forbidden_after_writes_reopen() -> None:
    assert automatic_rollback_allowed(
        phase=ActivationPhase.STARTING,
        writes_reopened=False,
        backup_verified=True,
        previous_release_verified=True,
    )
    assert not automatic_rollback_allowed(
        phase=ActivationPhase.VERIFYING,
        writes_reopened=True,
        backup_verified=True,
        previous_release_verified=True,
    )


def test_automatic_rollback_requires_verified_backup_and_previous_release() -> None:
    assert not automatic_rollback_allowed(
        phase=ActivationPhase.SWITCHED,
        writes_reopened=False,
        backup_verified=False,
        previous_release_verified=True,
    )
    assert not automatic_rollback_allowed(
        phase=ActivationPhase.SWITCHED,
        writes_reopened=False,
        backup_verified=True,
        previous_release_verified=False,
    )


def test_committed_candidate_can_roll_back_until_write_reopen_starts() -> None:
    assert automatic_rollback_allowed(
        phase=ActivationPhase.COMMITTED,
        writes_reopened=False,
        backup_verified=True,
        previous_release_verified=True,
    )
    assert not automatic_rollback_allowed(
        phase=ActivationPhase.COMMITTED,
        writes_reopened=True,
        backup_verified=True,
        previous_release_verified=True,
    )


def test_write_reopen_intent_is_uncertain_and_forbids_state_rollback() -> None:
    assert not automatic_rollback_allowed(
        phase=ActivationPhase.REOPENING_WRITES,
        writes_reopened=False,
        backup_verified=True,
        previous_release_verified=True,
    )


def test_terminal_phases_do_not_auto_rollback() -> None:
    for phase in (
        ActivationPhase.PLANNED,
        ActivationPhase.ROLLED_BACK,
        ActivationPhase.REFUSED,
        ActivationPhase.RECOVERY_REQUIRED,
        ActivationPhase.REOPENING_WRITES,
        ActivationPhase.WRITES_REOPENED,
    ):
        assert not automatic_rollback_allowed(
            phase=phase,
            writes_reopened=False,
            backup_verified=True,
            previous_release_verified=True,
        )


@pytest.mark.parametrize("bad_now", [-1, float("inf"), True])
def test_invalid_clock_rejected(bad_now) -> None:
    with pytest.raises(ValueError, match="current time"):
        plan_activation(
            current(),
            candidate(),
            release(PREVIOUS_SHA, PREVIOUS_DIGEST),
            controller(),
            now=bad_now,
        )
