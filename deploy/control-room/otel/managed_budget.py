"""Pure shared-budget and cleanup decisions; no writer, scanner or deletion.

Callers must inventory all managed roots and serialize admission/reservations.
A snapshot that fits is not a reservation or evidence of runtime enforcement.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

COMPONENTS = frozenset(
    {
        "otel_archive",
        "provenance_captures",
        "provenance_database",
        "telemetry_logs",
        "telemetry_backups",
        "telemetry_runtime",
        "other_managed",
    }
)
_POLICY_KEYS = {"schema_version", "total_max_bytes", "forensic_max_bytes", "retention_days"}
_RETENTION_KEYS = {"lean", "forensic", "captures", "metadata"}


def _integer(value: object, minimum: int = 0) -> bool:
    return type(value) is int and value >= minimum


def _timestamp(value: object) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        stamp = dt.datetime.fromisoformat(value)
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            return None
        return stamp.astimezone(dt.UTC)
    except (ValueError, OverflowError):
        return None


def validate_policy(policy: Mapping[str, Any]) -> None:
    """Reject ambiguous policy rather than defaulting to an archive-only budget."""
    if (
        not isinstance(policy, Mapping)
        or set(policy) != _POLICY_KEYS
        or type(policy["schema_version"]) is not int
        or policy["schema_version"] != 1
        or not _integer(policy["total_max_bytes"], 1)
        or not _integer(policy["forensic_max_bytes"], 1)
        or policy["forensic_max_bytes"] > policy["total_max_bytes"]
        or not isinstance(policy["retention_days"], Mapping)
        or set(policy["retention_days"]) != _RETENTION_KEYS
        or not all(_integer(value, 1) for value in policy["retention_days"].values())
    ):
        raise ValueError("invalid managed-storage policy")


def load_policy(path: Path | None = None) -> dict[str, Any]:
    """Read the small tracked policy; duplicate keys are not last-writer-wins."""
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate policy key")
            result[key] = value
        return result

    path = path or Path(__file__).with_name("managed_storage_policy.json")
    with path.open("rb") as stream:
        content = stream.read(65_537)
    if len(content) > 65_536:
        raise ValueError("policy exceeds size limit")
    try:
        policy = json.loads(content, object_pairs_hook=unique)
        validate_policy(policy)
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError("invalid managed-storage policy") from None
    return policy


def assess_budget(
    snapshot: Mapping[str, Any],
    *,
    policy: Mapping[str, Any],
    now: dt.datetime,
    max_age_seconds: int,
    requested_growth_bytes: int = 0,
) -> dict[str, Any]:
    """Assess total bytes, including protected data and all pending reservations."""
    validate_policy(policy)
    if (
        not isinstance(now, dt.datetime)
        or now.tzinfo is None
        or now.utcoffset() is None
        or not _integer(max_age_seconds, 1)
        or not _integer(requested_growth_bytes)
    ):
        raise ValueError("invalid budget evaluation arguments")
    result = {
        "scope": "all_managed_telemetry",
        "enforcement_verified": False,
        "reservation_created": False,
        "status": "INCOMPLETE",
        "reason": "invalid_snapshot",
        "fits_snapshot": False,
        "max_bytes": policy["total_max_bytes"],
        "used_bytes": None,
        "protected_bytes": None,
        "reserved_bytes": None,
        "available_bytes": None,
        "overshoot_bytes": None,
        "requested_growth_bytes": requested_growth_bytes,
    }
    if not isinstance(snapshot, Mapping) or snapshot.get("complete") is not True:
        return result | {"reason": "inventory_not_complete"}
    observed = _timestamp(snapshot.get("observed_at"))
    if observed is None:
        return result | {"reason": "invalid_observation_time"}
    age = (now.astimezone(dt.UTC) - observed).total_seconds()
    if age < 0 or age > max_age_seconds:
        return result | {"reason": "future_or_stale_inventory"}
    components = snapshot.get("components")
    if not isinstance(components, Mapping) or set(components) != COMPONENTS:
        return result | {"reason": "component_coverage_mismatch"}
    reserved = snapshot.get("reserved_bytes")
    if not _integer(reserved):
        return result | {"reason": "unknown_reservations"}
    used = protected = 0
    for item in components.values():
        if (
            not isinstance(item, Mapping)
            or not _integer(item.get("bytes"))
            or not _integer(item.get("protected_bytes"))
            or item["protected_bytes"] > item["bytes"]
        ):
            return result | {"reason": "invalid_component_measurement"}
        used += item["bytes"]
        protected += item["protected_bytes"]
    available = max(0, policy["total_max_bytes"] - used - reserved)
    fits = used + reserved + requested_growth_bytes <= policy["total_max_bytes"]
    return result | {
        "status": "OVER_BUDGET" if used > policy["total_max_bytes"] else "WITHIN_BUDGET",
        "reason": "fits_snapshot" if fits else "stop_optional_growth",
        "fits_snapshot": fits,
        "used_bytes": used,
        "protected_bytes": protected,
        "reserved_bytes": reserved,
        "available_bytes": available,
        "overshoot_bytes": max(0, used - policy["total_max_bytes"]),
    }


def metadata_cleanup_decision(
    record: Mapping[str, Any], *, policy: Mapping[str, Any], now: dt.datetime
) -> dict[str, Any]:
    """Select only old completed metadata whose retained dependants are absent."""
    validate_policy(policy)
    if not isinstance(now, dt.datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("evaluation time must be timezone-aware")
    result = {"eligible": False, "reason": "unknown_record", "deletion_performed": False}
    if not isinstance(record, Mapping):
        return result
    if record.get("completed") is not True:
        return result | {"reason": "not_completed"}
    for flag in ("frozen", "in_flight", "needed_for_retained_evidence", "needed_for_rollback"):
        if record.get(flag) is not False:
            return result | {"reason": "protected_or_unknown"}
    if record.get("captures_pruned") is not True:
        return result | {"reason": "captures_retained_or_unknown"}
    completed = _timestamp(record.get("completed_at"))
    if completed is None:
        return result | {"reason": "invalid_completion_time"}
    age = (now.astimezone(dt.UTC) - completed).total_seconds()
    if age <= policy["retention_days"]["metadata"] * 86_400:
        return result | {"reason": "within_retention_or_future"}
    return result | {"eligible": True, "reason": "expired_completed_unreferenced"}
