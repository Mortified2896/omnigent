"""Read-only release display and O1 -> O2 sync preflight.

Inputs MUST be observations collected by a trusted external controller, never
browser-supplied evidence. This module neither deploys nor authorizes a caller.
The host adapter must hold its deployment lock, fence target writes, verify
artifact bytes, and durably deduplicate requests before any service mutation.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any, TypeGuard
from uuid import UUID

MAX_AGE_SECONDS = 60.0
MAX_FUTURE_SKEW_SECONDS = 5.0
ACCEPTANCE_CHECKS = (
    "dependencies",
    "isolated_boot",
    "build_identity",
    "frontend",
    "o3_off",
    "smart_routing",
)
PIN_FIELDS = (
    "instance", "source_sha", "release_digest", "generation", "database_id", "schema"
)


class SyncRefused(ValueError):
    """A stale, incomplete, or unsafe sync request was rejected."""


def _hex(value: Any, length: int) -> bool:
    """Return whether value is a full lowercase hexadecimal identity."""
    return isinstance(value, str) and re.fullmatch(rf"[a-f0-9]{{{length}}}", value) is not None


def _text(value: Any) -> str | None:
    """Return a bounded printable label, or None for unknown/malformed data."""
    if isinstance(value, str) and 0 < len(value.strip()) <= 160 and value.isprintable():
        return value.strip()
    return None


def _number(value: Any) -> TypeGuard[int | float]:
    """Reject booleans and non-finite timestamps instead of coercing them."""
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _fresh(observed: Any, now: float) -> bool:
    """Return whether controller evidence is recent and not future-dated."""
    return (
        _number(observed)
        and observed >= 0
        and -MAX_FUTURE_SKEW_SECONDS <= now - observed < MAX_AGE_SECONDS
    )


def acceptance_digest(record: Mapping[str, Any]) -> str:
    """Hash canonical acceptance JSON using rtx_contract.canonical_digest's format.

    :param record: Parsed controller-owned acceptance-v2.json.
    :returns: Digest of the record, NOT a digest of a wheel or tar archive.
    :raises ValueError: If the record contains non-JSON/non-finite values.
    """
    try:
        data = json.dumps(dict(record), sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid acceptance JSON") from exc
    return hashlib.sha256(data.encode()).hexdigest()


@dataclass(frozen=True)
class PeerObservation:
    """Controller-collected identity; None means unknown, never a passing check.

    package_version is the installed distribution version. upstream_version/ref
    require explicit build provenance; do not infer them from package_version.
    generation must bind BOTH server and host process incarnations.
    live_validated_digest/generation bind O1's successful live acceptance.
    """

    instance: str
    source_sha: str | None = None
    release_digest: str | None = None
    package_version: str | None = None
    upstream_version: str | None = None
    upstream_ref: str | None = None
    generation: str | None = None
    database_id: str | None = None
    schema: str | None = None
    observed_at: float | None = None
    healthy: bool | None = None
    active_work: int | None = None
    live_validated_digest: str | None = None
    live_validated_generation: str | None = None

    def display(self) -> dict[str, Any]:
        """Return safe display fields without guessing official provenance.

        :returns: Version, schema, health and freshness fields for the admin UI.
        """
        sha = self.source_sha if _hex(self.source_sha, 40) else None
        return {
            "instance": _text(self.instance),
            "official_version": _text(self.upstream_version),
            "upstream_ref": _text(self.upstream_ref),
            "package_version": _text(self.package_version),
            "custom_version": f"git-{sha[:12]}" if sha else None,
            "source_sha": sha,
            "release_acceptance_digest": (
                self.release_digest if _hex(self.release_digest, 64) else None
            ),
            "schema_revision": _text(self.schema),
            "healthy": self.healthy if type(self.healthy) is bool else None,
            "observed_at": self.observed_at if _number(self.observed_at) else None,
        }

    def pins(self) -> dict[str, Any]:
        """Return exact compare-and-swap identities, without paths or commands."""
        values = asdict(self)
        return {key: values[key] for key in PIN_FIELDS}


@dataclass(frozen=True)
class ControllerReadiness:
    """Trusted host capabilities; no capability is implicitly enabled.

    Write fencing must cover browser/API writes, host/runner traffic and
    background jobs until acceptance or rollback. rollback_ready also requires
    target-specific rehearsal; matching Alembic heads alone is insufficient.
    """

    observed_at: float | None = None
    independent: bool = False
    idle_guard_ready: bool = False
    write_fence_ready: bool = False
    rollback_ready: bool = False
    transaction_idle: bool = False
    verified_release_digest: str | None = None


@dataclass(frozen=True)
class SyncPlan:
    """A read-only decision, not a privileged deployment job or authorization."""

    status: str
    blockers: tuple[str, ...]
    source: PeerObservation
    target: PeerObservation
    expires_at: float

    def expected(self) -> dict[str, Any]:
        """Return all identities a later request must still match."""
        return {"source": self.source.pins(), "target": self.target.pins()}

    def display(self) -> dict[str, Any]:
        """Return a serializable admin-panel response with stable reason codes."""
        return {
            "status": self.status,
            "blockers": list(self.blockers),
            "source": self.source.display(),
            "target": self.target.display(),
            "can_sync": self.status == "ready",
            "expires_at": self.expires_at,
        }

    def request(self, idempotency_key: str) -> dict[str, Any]:
        """Construct a pinned request for an authenticated external adapter.

        :param idempotency_key: Canonical UUID4, durably reserved by the adapter.
        :returns: Request containing no selectable target, URL, path or command.
        :raises SyncRefused: If the plan is not ready or the key is malformed.
        """
        if self.status != "ready":
            raise SyncRefused("sync is not ready")
        _check_key(idempotency_key)
        return {
            "operation": "sync-o2-to-o1",
            "expected": self.expected(),
            "expires_at": self.expires_at,
            "idempotency_key": idempotency_key,
        }


def plan_sync(
    source: PeerObservation,
    target: PeerObservation,
    acceptance: Mapping[str, Any],
    controller: ControllerReadiness,
    *,
    now: float,
) -> SyncPlan:
    """Compare verified O1/O2 observations without I/O or model calls.

    :param source: Live O1 observation, including live acceptance binding.
    :param target: Live O2 observation; O2 keeps its own database and artifacts.
    :param acceptance: O1's exact controller-owned, byte-verified acceptance record.
    :param controller: Capabilities and checks from the external controller.
    :param now: Current Unix timestamp (explicit for deterministic testing).
    :returns: A ready, blocked or already_current plan. Unknowns block mutation.
    :raises ValueError: If the supplied clock is invalid.
    """
    if not _number(now) or now < 0:
        raise ValueError("invalid current time")
    acceptance = acceptance if isinstance(acceptance, Mapping) else {}
    blocked: list[str] = []
    for label, peer, expected in (("source", source, "O1"), ("target", target, "O2")):
        checks = {
            "instance": peer.instance == expected,
            "source_sha": _hex(peer.source_sha, 40),
            "release_digest": _hex(peer.release_digest, 64),
            "generation": _text(peer.generation) is not None,
            "database_id": _hex(peer.database_id, 32),
            "schema": _text(peer.schema) is not None,
            "freshness": _fresh(peer.observed_at, now),
            "health": peer.healthy is True,
        }
        blocked.extend(f"{label}_{key}" for key, passed in checks.items() if not passed)
    if source.database_id == target.database_id:
        blocked.append("database_identity_collision")
    if source.schema != target.schema:
        blocked.append("peer_schema_mismatch")

    # A no-op is recognized only with complete, healthy, fresh peer identities.
    if not blocked and (source.source_sha, source.release_digest) == (
        target.source_sha, target.release_digest
    ):
        return SyncPlan("already_current", (), source, target, now)

    if type(target.active_work) is not int or target.active_work != 0:
        blocked.append("target_not_idle")
    if (
        source.live_validated_digest != source.release_digest
        or not _hex(source.live_validated_digest, 64)
    ):
        blocked.append("source_live_validation_missing")
    if (
        source.live_validated_generation != source.generation
        or _text(source.live_validated_generation) is None
    ):
        blocked.append("source_live_validation_stale")

    for name in (
        "independent",
        "idle_guard_ready",
        "write_fence_ready",
        "rollback_ready",
        "transaction_idle",
    ):
        if getattr(controller, name) is not True:
            blocked.append(f"controller_{name}")
    if not _fresh(controller.observed_at, now):
        blocked.append("controller_freshness")
    if (
        controller.verified_release_digest != source.release_digest
        or not _hex(controller.verified_release_digest, 64)
    ):
        blocked.append("artifact_not_verified")

    try:
        digest = acceptance_digest(acceptance)
    except (ValueError, TypeError):
        digest = None
    if digest != source.release_digest:
        blocked.append("acceptance_digest_mismatch")
    if (
        acceptance.get("source_sha") != source.source_sha
        or not _hex(acceptance.get("source_sha"), 40)
    ):
        blocked.append("acceptance_source_mismatch")
    if acceptance.get("schema_policy") != "same-schema":
        blocked.append("unsupported_schema_policy")
    if acceptance.get("schema") != target.schema or _text(acceptance.get("schema")) is None:
        blocked.append("acceptance_schema_mismatch")
    checks = acceptance.get("checks")
    if not isinstance(checks, Mapping) or any(
        checks.get(key) is not True for key in ACCEPTANCE_CHECKS
    ):
        blocked.append("acceptance_checks_incomplete")
    hashes = acceptance.get("hashes")
    if not isinstance(hashes, Mapping) or not hashes or any(
        not isinstance(path, str) or not path or not _hex(value, 64)
        for path, value in hashes.items()
    ):
        blocked.append("acceptance_hashes_incomplete")

    timestamps = (source.observed_at, target.observed_at, controller.observed_at)
    expiry = min(
        now + MAX_AGE_SECONDS, *(t + MAX_AGE_SECONDS for t in timestamps if _number(t))
    )
    return SyncPlan(
        "blocked" if blocked else "ready", tuple(blocked), source, target, max(now, expiry)
    )


def _check_key(key: Any) -> None:
    """Require a canonical UUID4; this does not itself prevent request replay."""
    try:
        parsed = UUID(key) if isinstance(key, str) else None
    except ValueError:
        parsed = None
    if parsed is None or parsed.version != 4 or str(parsed) != key:
        raise SyncRefused("invalid idempotency key")


def validate_sync_request(request: Mapping[str, Any], fresh_plan: SyncPlan, *, now: float) -> None:
    """Reject stale/tampered requests against a freshly recollected, locked plan.

    :param request: Authenticated admin's request (never evidence/capabilities).
    :param fresh_plan: Recomputed by the adapter under its deployment lock.
    :param now: Current Unix timestamp.
    :raises SyncRefused: On unsafe, expired, drifted or malformed requests.
    :returns: None. The adapter must still reserve idempotency, fence and recheck.
    """
    if not isinstance(request, Mapping) or set(request) != {
        "operation", "expected", "expires_at", "idempotency_key"
    }:
        raise SyncRefused("unsupported request fields")
    if request["operation"] != "sync-o2-to-o1" or fresh_plan.status != "ready":
        raise SyncRefused("sync is not ready")
    expiry = request["expires_at"]
    if not _number(now) or not _number(expiry) or not now < expiry <= now + MAX_AGE_SECONDS:
        raise SyncRefused("expired or invalid request")
    if not now < fresh_plan.expires_at:
        raise SyncRefused("fresh evidence expired")
    if request["expected"] != fresh_plan.expected():
        raise SyncRefused("source or target changed; refresh the plan")
    _check_key(request["idempotency_key"])
