"""Supervisor process/runtime baseline and zero-drift comparison."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

from . import identity
from .identity import Instance


class BaselineError(RuntimeError):
    """Raised when a complete supervisor baseline cannot be captured."""


@dataclass(frozen=True)
class UnitBaseline:
    unit: str
    active_state: str
    main_pid: int
    active_enter_timestamp_monotonic: int


@dataclass(frozen=True)
class SupervisorBaseline:
    instance: str
    artifact_sha: str
    artifact_version: str
    server: UnitBaseline
    host: UnitBaseline

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, blob: dict[str, Any]) -> SupervisorBaseline:
        try:
            return cls(
                instance=str(blob["instance"]),
                artifact_sha=str(blob["artifact_sha"]),
                artifact_version=str(blob["artifact_version"]),
                server=UnitBaseline(**blob["server"]),
                host=UnitBaseline(**blob["host"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise BaselineError(f"invalid supervisor baseline: {exc}") from exc


Runner = Callable[..., subprocess.CompletedProcess[str]]


def _unit_baseline(unit: str, *, runner: Runner = subprocess.run) -> UnitBaseline:
    result = runner(
        [
            "systemctl",
            "show",
            unit,
            "-p",
            "ActiveState",
            "-p",
            "MainPID",
            "-p",
            "ActiveEnterTimestampMonotonic",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise BaselineError(
            f"cannot snapshot {unit}: rc={result.returncode} {result.stderr.strip()}"
        )
    values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    try:
        state = values["ActiveState"]
        pid = int(values["MainPID"])
        active_at = int(values["ActiveEnterTimestampMonotonic"])
    except (KeyError, ValueError) as exc:
        raise BaselineError(f"incomplete systemd snapshot for {unit}: {values}") from exc
    if state != "active" or pid <= 0 or active_at <= 0:
        raise BaselineError(
            f"supervisor unit is not stably active: {unit} "
            f"state={state} pid={pid} active_at={active_at}"
        )
    return UnitBaseline(
        unit=unit,
        active_state=state,
        main_pid=pid,
        active_enter_timestamp_monotonic=active_at,
    )


def capture(
    instance: Instance,
    *,
    runner: Runner = subprocess.run,
) -> SupervisorBaseline:
    runtime = identity.runtime_identity(identity._resolve_active_python(instance.deployment_root))
    sha = runtime.get("commit_sha")
    version = runtime.get("version")
    if not isinstance(sha, str) or not isinstance(version, str):
        raise BaselineError(f"incomplete runtime identity for {instance.name}: {runtime}")
    return SupervisorBaseline(
        instance=instance.name,
        artifact_sha=sha,
        artifact_version=version,
        server=_unit_baseline(instance.service_unit, runner=runner),
        host=_unit_baseline(instance.host_unit, runner=runner),
    )


def compare(
    before: SupervisorBaseline | dict[str, Any],
    after: SupervisorBaseline | dict[str, Any],
) -> list[str]:
    """Return field-level drift; an empty list proves zero drift."""
    left = (
        before if isinstance(before, SupervisorBaseline) else SupervisorBaseline.from_dict(before)
    )
    right = after if isinstance(after, SupervisorBaseline) else SupervisorBaseline.from_dict(after)
    failures: list[str] = []

    def walk(prefix: str, a: Any, b: Any) -> None:
        if isinstance(a, dict) and isinstance(b, dict):
            for key in sorted(set(a) | set(b)):
                walk(f"{prefix}.{key}" if prefix else key, a.get(key), b.get(key))
        elif a != b:
            failures.append(f"{prefix}: {a!r} -> {b!r}")

    walk("", left.to_dict(), right.to_dict())
    return failures


__all__ = [
    "BaselineError",
    "SupervisorBaseline",
    "UnitBaseline",
    "capture",
    "compare",
]
