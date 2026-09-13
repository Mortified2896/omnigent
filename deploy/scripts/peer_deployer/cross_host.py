"""Read-only cross-host observations; never dispatch the local promotion FSM.

The source and candidate are inspected on their real hosts. Combining these
observations does not authorize promotion or invent a local supervisor.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from . import acceptance, baseline, identity, preflight, service_state, transaction

MAX_AGE_SECONDS = 60
MUTATION_BLOCKERS = (
    "reviewed source and immutable acceptance must be approved for promotion",
    "cross-host writer fencing and final state/workspace transfer are not implemented",
    "cross-host transaction ownership and paired recovery are not implemented",
    "real phone streaming/continuation and final cutover acceptance remain required",
)


class CrossHostError(RuntimeError):
    """Evidence cannot establish the declared remote identity or readiness."""


def _host() -> dict[str, Any]:
    return {
        "hostname": socket.gethostname(),
        "machine_id": Path("/etc/machine-id").read_text().strip(),
        "observed_at": time.time(),
    }


def _transactions() -> list[str]:
    root = transaction.DEFAULT_TX_ROOT
    if not root.exists():
        return []
    unresolved = []
    entries = sorted(root.iterdir())
    if len(entries) > 1000:
        raise CrossHostError("transaction inventory exceeds bounded inspection")
    for directory in entries:
        path = directory / "transaction.json"
        if not path.exists():
            unresolved.append(directory.name + ":missing-record")
            continue
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError):
            unresolved.append(directory.name + ":unreadable")
            continue
        # Ambiguous historical records require the existing reconciliation path.
        if record.get("phase") not in {"tx_committed", "rolled_back"}:
            unresolved.append(directory.name)
    return unresolved


def observe_source() -> dict[str, Any]:
    """Inspect canonical O1 and the actual O2 on the source host, without writes."""
    result = _host()
    result.update(
        role="source",
        target="O1",
        supervisor="O2",
        supervisor_baseline=baseline.capture(identity.O2).to_dict(),
        supervisor_health=identity.http_health_ok(identity.O2.health_url),
        target_baseline=baseline.capture(identity.O1).to_dict(),
        target_health=identity.http_health_ok(identity.O1.health_url),
        storage_guard=asdict(preflight.default_storage_guard_probe()),
        root_free_bytes=shutil.disk_usage("/").free,
        unresolved_transactions=_transactions(),
    )
    return result


def observe_candidate(
    record_path: Path, database: Path, mountpoint: Path, server_unit: str, host_unit: str
) -> dict[str, Any]:
    """Revalidate the installed immutable artifact and independent candidate."""
    record = acceptance.load(record_path, require_immutable_permissions=True)
    failures = acceptance.verify_release(record, Path(record.immutable_release_root))
    with sqlite3.connect(database.absolute().as_uri() + "?mode=ro", uri=True) as db:
        integrity = db.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        schemas = [row[0] for row in db.execute("SELECT version_num FROM alembic_version")]
    mounted = subprocess.run(
        ["findmnt", "--json", "--mountpoint", str(mountpoint), "-o", "TARGET,UUID,FSTYPE"],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    mounts = json.loads(mounted.stdout).get("filesystems", [])
    if len(mounts) != 1:
        raise CrossHostError("data mount cannot be identified exactly")
    result = _host()
    result.update(
        role="isolated-candidate",
        source_sha=record.source_sha,
        acceptance_sha256=record.acceptance_record_sha256,
        acceptance_failures=failures,
        target_db_schema=record.target_db_schema,
        database_integrity=integrity,
        database_schemas=schemas,
        data_mount=mounts[0],
        data_free_bytes=shutil.disk_usage(mountpoint).free,
        candidate_server_active=service_state.is_active(server_unit),
        candidate_host_active=service_state.is_active(host_unit),
        canonical_units={
            unit: (service_state.get_state(unit) if service_state.is_known(unit) else "unknown")
            for unit in (
                identity.O1.service_unit,
                identity.O1.host_unit,
                identity.O2.service_unit,
                identity.O2.host_unit,
            )
        },
        unresolved_transactions=_transactions(),
    )
    return result


def assess(
    source: dict[str, Any],
    candidate: dict[str, Any],
    expected: dict[str, Any],
    *,
    target: str,
    supervisor: str,
    now: float | None = None,
) -> dict[str, Any]:
    """Return fail-closed readiness evidence bound to both persistent host IDs."""
    identity.require_distinct(identity.get(target), identity.get(supervisor))
    if (target, supervisor) != ("O1", "O2"):
        raise CrossHostError("this migration inspects TARGET=O1, SUPERVISOR=O2 only")
    now = time.time() if now is None else now
    checks = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    for role, observed in (("source", source), ("candidate", candidate)):
        host = expected[role]
        check(
            role + ".identity",
            observed.get("hostname") == host["hostname"]
            and observed.get("machine_id") == host["machine_id"],
            "hostname and persistent machine ID",
        )
        timestamp = observed.get("observed_at")
        check(
            role + ".fresh",
            isinstance(timestamp, (int, float)) and 0 <= now - timestamp <= MAX_AGE_SECONDS,
            "observations must be at most 60 seconds old",
        )
        check(
            role + ".transactions",
            observed.get("unresolved_transactions") == [],
            "no unresolved canonical transaction",
        )
    check(
        "hosts.distinct",
        source.get("machine_id") != candidate.get("machine_id"),
        "candidate must be a separate machine",
    )
    check(
        "source.roles",
        source.get("role") == "source"
        and source.get("target") == target
        and source.get("supervisor") == supervisor,
        "real canonical source peers",
    )
    check(
        "source.health",
        source.get("target_health") is True and source.get("supervisor_health") is True,
        "both old peers healthy",
    )
    guard = source.get("storage_guard", {})
    check(
        "source.storage_guard",
        guard.get("active") is True and guard.get("latched") is False,
        "existing source guard must be active and unlatched",
    )
    check(
        "source.headroom",
        source.get("root_free_bytes", 0) >= preflight.MIN_FREE_BYTES,
        "existing source free-space requirement",
    )
    try:
        actual = baseline.SupervisorBaseline.from_dict(source["supervisor_baseline"])
        reference = baseline.SupervisorBaseline.from_dict(expected["supervisor_baseline"])
        drift = baseline.compare(reference, actual)
        canonical = (
            actual.instance == "O2"
            and actual.server.unit == identity.O2.service_unit
            and actual.host.unit == identity.O2.host_unit
        )
        live = all(
            unit.active_state == "active"
            and unit.main_pid > 0
            and unit.active_enter_timestamp_monotonic > 0
            for unit in (actual.server, actual.host)
        )
        check(
            "supervisor.zero_drift",
            not drift and canonical and live,
            "; ".join(drift) or "real O2 artifact, PIDs and active timestamps",
        )
    except (KeyError, TypeError, ValueError, baseline.BaselineError):
        check("supervisor.zero_drift", False, "missing or invalid real O2 baseline")
    check(
        "candidate.role",
        candidate.get("role") == "isolated-candidate",
        "candidate evidence must not impersonate a canonical peer",
    )
    check(
        "candidate.artifact",
        candidate.get("source_sha") == expected["source_sha"]
        and candidate.get("acceptance_sha256") == expected["acceptance_sha256"]
        and candidate.get("acceptance_failures") == [],
        "exact immutable accepted release",
    )
    check(
        "candidate.database",
        candidate.get("database_integrity") is True
        and candidate.get("database_schemas") == [candidate.get("target_db_schema")],
        "integrity and accepted schema",
    )
    mount = candidate.get("data_mount", {})
    check(
        "candidate.data_mount",
        mount.get("target") == expected["data_mount"]
        and mount.get("uuid") == expected["data_uuid"],
        "exact persistent data mount",
    )
    check(
        "candidate.headroom",
        candidate.get("data_free_bytes", 0) >= preflight.MIN_FREE_BYTES,
        "candidate data space meets existing minimum",
    )
    check(
        "candidate.services",
        candidate.get("candidate_server_active") is True
        and candidate.get("candidate_host_active") is True,
        "independent candidate server and host",
    )
    states = candidate.get("canonical_units", {})
    required_units = (
        identity.O1.service_unit,
        identity.O1.host_unit,
        identity.O2.service_unit,
        identity.O2.host_unit,
    )
    check(
        "candidate.no_canonical_writers",
        all(states.get(unit) in ("inactive", "unknown") for unit in required_units),
        "no canonical writer is active on candidate host",
    )
    return {
        "target": target,
        "supervisor": supervisor,
        "observations_passed": all(item["ok"] for item in checks),
        "ready_for_mutation": False,
        "checks": checks,
        "remaining_gates": list(MUTATION_BLOCKERS),
        "scope": (
            "read-only cross-host observations; local promote cannot consume these as a supervisor"
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="operation", required=True)
    sub.add_parser("observe-source")
    candidate = sub.add_parser("observe-candidate")
    candidate.add_argument("--acceptance-record", type=Path, required=True)
    candidate.add_argument("--database", type=Path, required=True)
    candidate.add_argument("--mountpoint", type=Path, required=True)
    candidate.add_argument("--server-unit", required=True)
    candidate.add_argument("--host-unit", required=True)
    check = sub.add_parser("assess")
    check.add_argument("--source", type=Path, required=True)
    check.add_argument("--candidate", type=Path, required=True)
    check.add_argument("--expected", type=Path, required=True)
    check.add_argument("--target", choices=("O1", "O2"), required=True)
    check.add_argument("--supervisor", choices=("O1", "O2"), required=True)
    args = parser.parse_args(argv)
    if args.operation == "assess":
        result = assess(
            json.loads(args.source.read_text()),
            json.loads(args.candidate.read_text()),
            json.loads(args.expected.read_text()),
            target=args.target,
            supervisor=args.supervisor,
        )
    else:
        if os.geteuid() != 0:
            raise CrossHostError("complete host observations require scoped root read access")
        if args.operation == "observe-source":
            result = observe_source()
        else:
            result = observe_candidate(
                args.acceptance_record,
                args.database,
                args.mountpoint,
                args.server_unit,
                args.host_unit,
            )
    print(json.dumps(result, indent=2))
    return 2 if args.operation == "assess" else 0


if __name__ == "__main__":
    raise SystemExit(main())
