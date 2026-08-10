"""Root-owned immutable promotion plans.

A ``PromotionPlan`` is the *trusted* description of a permitted
control-room upgrade: which target instance is allowed to be
upgraded, which supervisor must remain untouched, which systemd
units own each side, which paths are the deployment / state roots,
and which committed rollback contract the engine must obey.

The daemon loads exactly the plan whose allowed topology matches the
``(caller, target)`` pair that was actually authenticated from
kernel-observed peer credentials.  The daemon does **not** let the
caller choose plan parameters at request time.

Plans are root-owned ``0600`` JSON files at::

    /var/lib/control-room-peer-deployer/plans/<name>.json

They are produced by the bootstrap installer.  Any modification of
the plan after install requires a new bootstrap (which atomically
regenerates the registry and the systemd unit).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PLAN_SCHEMA = "control-room-peer-deployer.promotion-plan.v1"
SHA_RE = re.compile(r"[0-9a-f]{40}")


class PlanError(RuntimeError):
    pass


@dataclass(frozen=True)
class InstanceState:
    commit_sha: str
    version: str
    schema: str = ""

    def is_valid(self) -> bool:
        if not SHA_RE.fullmatch(self.commit_sha):
            return False
        if not isinstance(self.version, str) or not self.version:
            return False
        return True


@dataclass(frozen=True)
class PromotionPlan:
    name: str
    supervisor_name: str
    target_name: str
    target_service_units: tuple[str, ...]
    supervisor_service_units: tuple[str, ...]
    target_deployment_root: str
    supervisor_deployment_root: str
    target_state_root: str
    supervisor_state_root: str
    target_health_url: str
    supervisor_health_url: str
    accepted_artifact_sha: str
    accepted_artifact_version: str
    expected_target_pre_state: InstanceState
    expected_supervisor_pre_state: InstanceState
    paired_rollback: bool
    supervisor_zero_drift: bool
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def target(self) -> tuple[str, str]:
        return (self.target_name, self.target_deployment_root)

    @property
    def supervisor(self) -> tuple[str, str]:
        return (self.supervisor_name, self.supervisor_deployment_root)


def _state(blob: Any) -> InstanceState:
    if not isinstance(blob, dict):
        raise PlanError(f"state must be a dict, got {type(blob).__name__}")
    sha = blob.get("commit_sha")
    version = blob.get("version")
    schema = blob.get("schema") or ""
    if not isinstance(sha, str) or not SHA_RE.fullmatch(sha):
        raise PlanError(f"invalid commit_sha {sha!r}")
    if not isinstance(version, str) or not version:
        raise PlanError(f"invalid version {version!r}")
    return InstanceState(commit_sha=sha, version=version, schema=schema)


def _tuple(value: Any, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise PlanError(f"{field_name} must be a non-empty list")
    out = []
    for v in value:
        if not isinstance(v, str) or not v:
            raise PlanError(f"{field_name} must be list of strings")
        out.append(v)
    return tuple(out)


def _validate(name: str, blob: dict[str, Any]) -> PromotionPlan:
    if blob.get("schema") != PLAN_SCHEMA:
        raise PlanError(f"plan {name} schema mismatch: {blob.get('schema')!r}")
    topo = blob.get("allowed_topology")
    if not isinstance(topo, dict):
        raise PlanError(f"plan {name} missing allowed_topology dict")
    supervisor = topo.get("supervisor")
    target = topo.get("target")
    if not isinstance(supervisor, str) or not supervisor:
        raise PlanError("plan allowed_topology.supervisor is required")
    if not isinstance(target, str) or not target:
        raise PlanError("plan allowed_topology.target is required")
    if supervisor == target:
        raise PlanError(
            f"REFUSED: plan {name} allows target == supervisor == {target!r}"
        )
    units = blob.get("service_units")
    if not isinstance(units, dict):
        raise PlanError("plan missing service_units dict")
    roots = blob.get("deployment_roots")
    if not isinstance(roots, dict):
        raise PlanError("plan missing deployment_roots dict")
    states = blob.get("state_roots")
    if not isinstance(states, dict):
        raise PlanError("plan missing state_roots dict")
    health = blob.get("health_urls")
    if not isinstance(health, dict):
        raise PlanError("plan missing health_urls dict")
    pre = blob.get("expected_pre_state")
    if not isinstance(pre, dict):
        raise PlanError("plan missing expected_pre_state dict")
    accepted_sha = blob.get("accepted_artifact_sha")
    accepted_version = blob.get("accepted_artifact_version")
    if not isinstance(accepted_sha, str) or not SHA_RE.fullmatch(accepted_sha):
        raise PlanError("plan accepted_artifact_sha must be 40-char SHA")
    if not isinstance(accepted_version, str) or not accepted_version:
        raise PlanError("plan accepted_artifact_version is required")
    rollback = blob.get("rollback") or {}
    paired = bool(rollback.get("paired_runtime_db", False))
    zero_drift = bool(rollback.get("supervisor_zero_drift", False))
    return PromotionPlan(
        name=name,
        supervisor_name=supervisor,
        target_name=target,
        target_service_units=_tuple(units.get("target"), "target service units"),
        supervisor_service_units=_tuple(units.get("supervisor"), "supervisor service units"),
        target_deployment_root=roots.get("target"),
        supervisor_deployment_root=roots.get("supervisor"),
        target_state_root=states.get("target"),
        supervisor_state_root=states.get("supervisor"),
        target_health_url=health.get("target"),
        supervisor_health_url=health.get("supervisor"),
        accepted_artifact_sha=accepted_sha,
        accepted_artifact_version=accepted_version,
        expected_target_pre_state=_state(pre.get("target")),
        expected_supervisor_pre_state=_state(pre.get("supervisor")),
        paired_rollback=paired,
        supervisor_zero_drift=zero_drift,
        raw=blob,
    )


def load(path: Path) -> PromotionPlan:
    if not path.is_file():
        raise PlanError(f"plan missing: {path}")
    blob = json.loads(path.read_text())
    if not isinstance(blob, dict):
        raise PlanError(f"plan is not a JSON object: {path}")
    return _validate(path.stem, blob)


def load_all(plan_dir: Path | None = None) -> dict[tuple[str, str], PromotionPlan]:
    """Return all plans keyed by (supervisor, target).

    Each combination must appear at most once.
    """
    if plan_dir is None:
        plan_dir = Path("/var/lib/control-room-peer-deployer/plans")
    out: dict[tuple[str, str], PromotionPlan] = {}
    if not plan_dir.is_dir():
        raise PlanError(f"plan directory missing: {plan_dir}")
    for entry in sorted(plan_dir.glob("*.json")):
        plan = load(entry)
        key = (plan.supervisor_name, plan.target_name)
        if key in out:
            raise PlanError(f"duplicate plan for {key}: {entry}")
        out[key] = plan
    if not out:
        raise PlanError(f"no promotion plans found under {plan_dir}")
    return out


def find(caller: str, target: str, plan_dir: Path | None = None) -> PromotionPlan:
    plans = load_all(plan_dir)
    key = (caller, target)
    if key not in plans:
        raise PlanError(
            f"REFUSED: there is no accepted plan for {caller} -> {target}; "
            f"available: {sorted(plans)}"
        )
    return plans[key]


__all__ = [
    "InstanceState",
    "PromotionPlan",
    "PlanError",
    "PLAN_SCHEMA",
    "load",
    "load_all",
    "find",
]
