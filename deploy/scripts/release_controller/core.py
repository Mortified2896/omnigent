"""Pure single-service Omnigent release activation contract.

No I/O or privilege lives here. A trusted external controller must collect
evidence, verify accepted bytes, serialize mutation, fence/drain writers, back
up state, switch the release, restart, verify, and only then reopen writes.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any, TypeGuard
from uuid import UUID

MAX_EVIDENCE_AGE_SECONDS = 60.0
MAX_FUTURE_SKEW_SECONDS = 5.0
_SHA = re.compile(r"^[a-f0-9]{40}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")


class ActivationRefused(ValueError):
    """Activation evidence or request failed closed."""


class ActivationPhase(StrEnum):
    """Durable phases for the future single-service activation transaction."""

    PLANNED = "planned"
    FENCING = "fencing"
    DRAINING = "draining"
    QUIESCED = "quiesced"
    BACKED_UP = "backed_up"
    SWITCHED = "switched"
    STARTING = "starting"
    VERIFYING = "verifying"
    COMMITTED = "committed"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"
    RECOVERY_REQUIRED = "recovery_required"


_ALLOWED = {
    ActivationPhase.PLANNED: {
        ActivationPhase.FENCING,
        ActivationPhase.RECOVERY_REQUIRED,
    },
    ActivationPhase.FENCING: {
        ActivationPhase.DRAINING,
        ActivationPhase.ROLLING_BACK,
        ActivationPhase.RECOVERY_REQUIRED,
    },
    ActivationPhase.DRAINING: {
        ActivationPhase.QUIESCED,
        ActivationPhase.ROLLING_BACK,
        ActivationPhase.RECOVERY_REQUIRED,
    },
    ActivationPhase.QUIESCED: {
        ActivationPhase.BACKED_UP,
        ActivationPhase.ROLLING_BACK,
        ActivationPhase.RECOVERY_REQUIRED,
    },
    ActivationPhase.BACKED_UP: {
        ActivationPhase.SWITCHED,
        ActivationPhase.ROLLING_BACK,
        ActivationPhase.RECOVERY_REQUIRED,
    },
    ActivationPhase.SWITCHED: {
        ActivationPhase.STARTING,
        ActivationPhase.ROLLING_BACK,
        ActivationPhase.RECOVERY_REQUIRED,
    },
    ActivationPhase.STARTING: {
        ActivationPhase.VERIFYING,
        ActivationPhase.ROLLING_BACK,
        ActivationPhase.RECOVERY_REQUIRED,
    },
    ActivationPhase.VERIFYING: {
        ActivationPhase.COMMITTED,
        ActivationPhase.ROLLING_BACK,
        ActivationPhase.RECOVERY_REQUIRED,
    },
    ActivationPhase.ROLLING_BACK: {
        ActivationPhase.ROLLED_BACK,
        ActivationPhase.RECOVERY_REQUIRED,
    },
    ActivationPhase.COMMITTED: set(),
    ActivationPhase.ROLLED_BACK: set(),
    ActivationPhase.RECOVERY_REQUIRED: set(),
}


def transition_allowed(current: ActivationPhase, target: ActivationPhase) -> bool:
    """Return whether the durable state machine permits this transition."""

    return target in _ALLOWED[current]


def _number(value: Any) -> TypeGuard[int | float]:
    return type(value) in (int, float) and math.isfinite(value)


def _fresh(value: Any, now: float) -> bool:
    return (
        _number(value)
        and value >= 0
        and -MAX_FUTURE_SKEW_SECONDS <= now - value < MAX_EVIDENCE_AGE_SECONDS
    )


def _sha(value: Any) -> bool:
    return isinstance(value, str) and _SHA.fullmatch(value) is not None


def _sha256(value: Any) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _text(value: Any) -> str | None:
    if isinstance(value, str) and 0 < len(value.strip()) <= 200 and value.isprintable():
        return value.strip()
    return None


@dataclass(frozen=True)
class ReleaseIdentity:
    """Immutable software identity derived from trusted acceptance evidence."""

    source_sha: str | None = None
    acceptance_digest: str | None = None
    package_version: str | None = None
    upstream_version: str | None = None
    upstream_ref: str | None = None
    schema_revision: str | None = None

    def complete(self) -> bool:
        """Return whether mutation-critical identity fields are complete."""

        return (
            _sha(self.source_sha)
            and _sha256(self.acceptance_digest)
            and _text(self.schema_revision) is not None
        )

    def display(self) -> dict[str, Any]:
        """Return UI-safe version identity without inventing provenance."""

        sha = self.source_sha if _sha(self.source_sha) else None
        return {
            "official_version": _text(self.upstream_version),
            "upstream_ref": _text(self.upstream_ref),
            "package_version": _text(self.package_version),
            "custom_version": f"git-{sha[:12]}" if sha else None,
            "source_sha": sha,
            "acceptance_digest": (
                self.acceptance_digest if _sha256(self.acceptance_digest) else None
            ),
            "schema_revision": _text(self.schema_revision),
        }

    def pins(self) -> dict[str, Any]:
        """Return exact compare-and-swap software pins."""

        return asdict(self)


@dataclass(frozen=True)
class RuntimeObservation:
    """Fresh external-controller observation of the running service."""

    release: ReleaseIdentity
    process_generation: str | None = None
    state_identity: str | None = None
    state_generation: str | None = None
    observed_at: float | None = None
    healthy: bool | None = None
    active_work: int | None = None
    writes_fenced: bool | None = None

    def pins(self) -> dict[str, Any]:
        """Return runtime identities that must not drift before mutation."""

        return {
            "release": self.release.pins(),
            "process_generation": self.process_generation,
            "state_identity": self.state_identity,
            "state_generation": self.state_generation,
        }


@dataclass(frozen=True)
class CandidateEvidence:
    """Controller-owned evidence for the exact candidate release."""

    release: ReleaseIdentity
    observed_at: float | None = None
    bytes_verified: bool = False
    isolated_boot_ok: bool = False
    health_ok: bool = False
    build_identity_ok: bool = False
    frontend_ok: bool = False
    rollback_from_current_ok: bool = False


@dataclass(frozen=True)
class ControllerReadiness:
    """Host capabilities that must be proven before activation is offered."""

    observed_at: float | None = None
    independent: bool = False
    serialized: bool = False
    drain_ready: bool = False
    write_fence_ready: bool = False
    backup_ready: bool = False
    restart_ready: bool = False
    rollback_ready: bool = False
    previous_release_retained: bool = False


@dataclass(frozen=True)
class ActivationPlan:
    """Read-only exact-release plan; it grants no host privilege."""

    status: str
    blockers: tuple[str, ...]
    current: RuntimeObservation
    candidate: CandidateEvidence
    previous: ReleaseIdentity | None
    expires_at: float

    def expected(self) -> dict[str, Any]:
        """Return all identities a later activation request must still match."""

        return {
            "current": self.current.pins(),
            "candidate": self.candidate.release.pins(),
            "previous": self.previous.pins() if self.previous else None,
        }

    def request(self, idempotency_key: str) -> dict[str, Any]:
        """Build a narrow request for an authenticated external controller."""

        if self.status != "ready":
            raise ActivationRefused("activation is not ready")
        _check_uuid4(idempotency_key)
        return {
            "operation": "activate-release",
            "expected": self.expected(),
            "expires_at": self.expires_at,
            "idempotency_key": idempotency_key,
        }


def plan_activation(
    current: RuntimeObservation,
    candidate: CandidateEvidence,
    previous: ReleaseIdentity | None,
    controller: ControllerReadiness,
    *,
    now: float,
) -> ActivationPlan:
    """Plan a same-schema exact-release activation without performing I/O."""

    if not _number(now) or now < 0:
        raise ValueError("invalid current time")

    blockers: list[str] = []
    if not current.release.complete():
        blockers.append("current_release_identity")
    if not candidate.release.complete():
        blockers.append("candidate_release_identity")
    if current.release.schema_revision != candidate.release.schema_revision:
        blockers.append("schema_mismatch")
    if _text(current.process_generation) is None:
        blockers.append("process_generation")
    if _text(current.state_identity) is None:
        blockers.append("state_identity")
    if _text(current.state_generation) is None:
        blockers.append("state_generation")
    if not _fresh(current.observed_at, now):
        blockers.append("current_freshness")
    if current.healthy is not True:
        blockers.append("current_health")
    if type(current.active_work) is not int or current.active_work < 0:
        blockers.append("active_work_unknown")

    if not _fresh(candidate.observed_at, now):
        blockers.append("candidate_freshness")
    for field in (
        "bytes_verified",
        "isolated_boot_ok",
        "health_ok",
        "build_identity_ok",
        "frontend_ok",
        "rollback_from_current_ok",
    ):
        if getattr(candidate, field) is not True:
            blockers.append(f"candidate_{field}")

    for field in (
        "independent",
        "serialized",
        "drain_ready",
        "write_fence_ready",
        "backup_ready",
        "restart_ready",
        "rollback_ready",
        "previous_release_retained",
    ):
        if getattr(controller, field) is not True:
            blockers.append(f"controller_{field}")
    if not _fresh(controller.observed_at, now):
        blockers.append("controller_freshness")

    if not blockers and (
        current.release.source_sha,
        current.release.acceptance_digest,
    ) == (
        candidate.release.source_sha,
        candidate.release.acceptance_digest,
    ):
        return ActivationPlan("already_current", (), current, candidate, previous, now)

    if previous is not None:
        if not previous.complete():
            blockers.append("previous_release_identity")
        elif previous.schema_revision != current.release.schema_revision:
            blockers.append("previous_schema_mismatch")

    stamps = [current.observed_at, candidate.observed_at, controller.observed_at]
    expiry = min(
        [now + MAX_EVIDENCE_AGE_SECONDS]
        + [
            float(value) + MAX_EVIDENCE_AGE_SECONDS
            for value in stamps
            if _number(value)
        ]
    )
    return ActivationPlan(
        "blocked" if blockers else "ready",
        tuple(dict.fromkeys(blockers)),
        current,
        candidate,
        previous,
        max(now, expiry),
    )


def validate_activation_request(
    request: Mapping[str, Any],
    fresh_plan: ActivationPlan,
    *,
    now: float,
) -> None:
    """Reject stale or tampered activation requests against fresh evidence."""

    if not isinstance(request, Mapping) or set(request) != {
        "operation",
        "expected",
        "expires_at",
        "idempotency_key",
    }:
        raise ActivationRefused("unsupported request fields")
    if request["operation"] != "activate-release" or fresh_plan.status != "ready":
        raise ActivationRefused("activation is not ready")
    expiry = request["expires_at"]
    if (
        not _number(now)
        or not _number(expiry)
        or not now < expiry <= now + MAX_EVIDENCE_AGE_SECONDS
    ):
        raise ActivationRefused("expired or invalid request")
    if not now < fresh_plan.expires_at:
        raise ActivationRefused("fresh evidence expired")
    if request["expected"] != fresh_plan.expected():
        raise ActivationRefused("runtime or candidate changed; refresh the plan")
    _check_uuid4(request["idempotency_key"])


def automatic_rollback_allowed(
    *,
    phase: ActivationPhase,
    writes_reopened: bool,
    backup_verified: bool,
    previous_release_verified: bool,
) -> bool:
    """Return whether stale-state rollback remains safe to automate."""

    if writes_reopened:
        return False
    if phase in {
        ActivationPhase.COMMITTED,
        ActivationPhase.ROLLED_BACK,
        ActivationPhase.RECOVERY_REQUIRED,
        ActivationPhase.PLANNED,
    }:
        return False
    return backup_verified and previous_release_verified


def _check_uuid4(value: Any) -> None:
    try:
        parsed = UUID(value) if isinstance(value, str) else None
    except ValueError:
        parsed = None
    if parsed is None or parsed.version != 4 or str(parsed) != value:
        raise ActivationRefused("invalid idempotency key")
