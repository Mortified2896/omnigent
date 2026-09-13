"""Cold maintenance of an idle local peer under its real supervisor.

This freezes writers without selecting a replacement or automatically reopening
the source. Cross-host observations are never accepted as a local supervisor.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import socket
import sqlite3
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any

from . import acceptance, baseline, host_promotion, identity, preflight, transaction

SYSTEMD_UNIT_ROOT = Path("/etc/systemd/system")
MACHINE_ID_FILE = Path("/etc/machine-id")
LOCK_FILE = Path("/run/lock/omnigent-cold-maintenance.lock")

IDLE = frozenset({"idle", "failed", "stopped"})


class MaintenanceError(RuntimeError):
    """A cold maintenance gate failed; preserve the recorded phase."""


def request(base: str, path: str) -> dict[str, Any]:
    with urllib.request.urlopen(base + path, timeout=10) as response:
        return json.load(response)


def idle_sessions(base: str) -> dict[str, Any]:
    """Resolve list display state against each fresh session detail."""
    rows = []
    after = ""
    for _ in range(100):
        page = request(base, "/v1/sessions?limit=200&include_archived=true" + after)
        data = page.get("data")
        if not isinstance(data, list) or not isinstance(page.get("has_more"), bool):
            raise MaintenanceError("sessions.incomplete_inventory")
        rows.extend(data)
        if not page["has_more"]:
            break
        cursor = page.get("last_id")
        if not isinstance(cursor, str) or not cursor.isalnum():
            raise MaintenanceError("sessions.invalid_cursor")
        after = "&after=" + cursor
    else:
        raise MaintenanceError("sessions.inventory_limit")
    checked = []
    for row in rows:
        session_id = row.get("id")
        if not isinstance(session_id, str) or not session_id.isalnum():
            raise MaintenanceError("sessions.invalid_id")
        detail = request(base, "/v1/sessions/" + session_id)
        status = detail.get("status")
        pending = detail.get("pending_elicitations_count", 0)
        if status not in IDLE or pending != 0:
            raise MaintenanceError(f"sessions.busy_or_unknown:{session_id}:{status}")
        checked.append({"id": session_id, "status": status})
    return {"sessions": checked, "count": len(checked), "observed_at": time.time()}


def process_inventory(target: identity.Instance) -> list[dict[str, Any]]:
    """Reject execution children other than the pre-fork idle zygote."""
    units = (target.service_unit, target.host_unit)
    result = []
    main = {}
    for unit in units:
        out = subprocess.check_output(
            ["systemctl", "show", unit, "-p", "MainPID", "--value"], text=True
        )
        main[unit] = int(out.strip())
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            groups = (proc / "cgroup").read_text()
            unit = next((u for u in units if ("/" + u) in groups), None)
            if unit is None:
                continue
            argv = (proc / "cmdline").read_bytes().split(b"\0")
            pid = int(proc.name)
            zygote = len(argv) == 4 and argv[1:3] == [b"-m", b"omnigent.runner._zygote"]
            if pid != main[unit] and not zygote:
                raise MaintenanceError(f"writers.unknown_child:{unit}:{pid}")
            result.append({"pid": pid, "unit": unit, "role": "zygote" if zygote else "main"})
        except FileNotFoundError:
            continue
    return result


def run_checks(target: identity.Instance, supervisor: identity.Instance, record_path: Path):
    """Keep every existing preflight gate; freeze does not bypass promotion failures."""
    report = preflight.run_preflight(
        target=target,
        supervisor=supervisor,
        mode="peer-copy",
        acceptance_record_path=record_path,
    )
    if not report.passed:
        raise MaintenanceError(json.dumps(report.to_dict()))
    current = baseline.capture(target)
    accepted = acceptance.load(record_path, require_immutable_permissions=True)
    if current.artifact_sha != accepted.source_sha:
        raise MaintenanceError("source.acceptance_does_not_match_running_artifact")
    database = preflight.target_home_for(target) / "chat.db"
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
        if db.execute("SELECT COUNT(*) FROM scheduled_tasks").fetchone()[0]:
            raise MaintenanceError("source.scheduled_tasks_need_supported_pause")
    return report, current


def existing_freeze(
    target: identity.Instance,
    supervisor: identity.Instance,
    machine_id: str,
    expected_peer: dict[str, Any],
) -> dict[str, Any] | None:
    paths = [
        SYSTEMD_UNIT_ROOT / (u + ".d") / "90-cold-maintenance.conf"
        for u in (target.service_unit, target.host_unit)
    ]
    if not any(p.exists() for p in paths):
        return None
    if not all(p.is_file() for p in paths):
        raise MaintenanceError("source.partial_fence; inspect preserved transaction")
    conditions = []
    for path in paths:
        lines = [
            line.removeprefix("ConditionPathExists=!")
            for line in path.read_text().splitlines()
            if line.startswith("ConditionPathExists=!")
        ]
        if len(lines) != 1:
            raise MaintenanceError("source.invalid_fence")
        conditions.append(Path(lines[0]))
    fence = conditions[0]
    if conditions[1] != fence or fence.parent.parent != transaction.DEFAULT_TX_ROOT:
        raise MaintenanceError("source.fence_identity")
    receipt = json.loads((fence.parent / "cold-maintenance.json").read_text())
    if receipt.get("target") != target.name or receipt.get("supervisor") != supervisor.name:
        raise MaintenanceError("source.transaction_roles")
    if receipt.get("machine_id") != machine_id or receipt.get("phase") != "source-frozen":
        raise MaintenanceError("source.incomplete_freeze; inspect preserved transaction")
    if not fence.is_file():
        raise MaintenanceError("source.fence_missing")
    if baseline.compare(expected_peer, baseline.capture(supervisor)):
        raise MaintenanceError("supervisor.zero_drift")
    for unit in (target.service_unit, target.host_unit):
        wait_stopped(unit)
    if process_inventory(target):
        raise MaintenanceError("source.remaining_processes")
    database = preflight.target_home_for(target) / "chat.db"
    if host_promotion._sha(database) != receipt["frozen_database_sha256"]:
        raise MaintenanceError("source.data_changed_after_freeze")
    return receipt


def freeze(
    *,
    target: identity.Instance,
    supervisor: identity.Instance,
    record_path: Path,
    expected_machine_id: str,
    expected_supervisor: dict[str, Any],
    apply: bool,
) -> dict[str, Any]:
    identity.require_distinct(target, supervisor)
    if os.geteuid() != 0 or MACHINE_ID_FILE.read_text().strip() != expected_machine_id:
        raise MaintenanceError("source.host_identity")
    previous = existing_freeze(target, supervisor, expected_machine_id, expected_supervisor)
    if previous is not None:
        return previous
    report, source = run_checks(target, supervisor, record_path)
    peer = baseline.capture(supervisor)
    if baseline.compare(expected_supervisor, peer):
        raise MaintenanceError("supervisor.zero_drift")
    base = target.health_url.removesuffix("/health")
    sessions = idle_sessions(base)
    processes = process_inventory(target)
    result: dict[str, Any] = {
        "kind": "cold-maintenance",
        "target": target.name,
        "supervisor": supervisor.name,
        "hostname": socket.gethostname(),
        "machine_id": expected_machine_id,
        "source": source.to_dict(),
        "supervisor_baseline": peer.to_dict(),
        "preflight": report.to_dict(),
        "idle": sessions,
        "processes": processes,
        "phase": "preflight",
        "mutation_boundary_crossed": False,
    }
    if not apply:
        return result
    host_promotion._no_live_transactions()
    lock = LOCK_FILE.open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    txid = transaction.make_tx_id()
    accepted = acceptance.load(record_path)
    wheels = {w.role: w for w in accepted.wheels}
    record = transaction.create(
        root=transaction.DEFAULT_TX_ROOT,
        tx_id=txid,
        target=target.name,
        supervisor=supervisor.name,
        target_artifact_sha=accepted.source_sha,
        target_artifact_version=accepted.package_version,
        main_wheel_sha256=wheels["main"].sha256,
        sdk_client_wheel_sha256=wheels["sdk_client"].sha256,
        sdk_ui_wheel_sha256=wheels["sdk_ui"].sha256,
        acceptance_record_path=str(record_path),
        acceptance_record_sha256=accepted.acceptance_record_sha256,
        supervisor_baseline=peer.to_dict(),
        referenced_resources=[str(target.deployment_root), str(preflight.target_home_for(target))],
    )
    root = transaction.DEFAULT_TX_ROOT / txid
    result["transaction"] = str(root)
    evidence = root / "cold-maintenance.json"

    def phase(value: str) -> None:
        result["phase"] = value
        host_promotion._atomic_json(evidence, result)

    phase("checkpoint")
    database = preflight.target_home_for(target) / "chat.db"
    backup = root / "pre-stop-chat.db"
    record.db_backup_sha256 = host_promotion._backup_db(database, backup)
    record.db_backup_path = str(backup)
    record.db_backup_integrity = "ok"
    record.old_db_schema = host_promotion._db(database)[1]
    record.target_db_schema = record.old_db_schema
    record.old_runtime_path = str(
        identity._resolve_active_python(target.deployment_root).parent.parent
    )
    record.old_runtime_sha = source.artifact_sha
    transaction.register_owned(record, str(backup), root=transaction.DEFAULT_TX_ROOT)
    transaction.save(record, root=transaction.DEFAULT_TX_ROOT)
    units = (target.service_unit, target.host_unit)
    fence = root / "writer-fence"
    dropins = []
    result["unit_enablement"] = {}
    for unit in units:
        state = subprocess.run(
            ["systemctl", "is-enabled", unit], capture_output=True, text=True, check=False
        ).stdout.strip()
        if state not in {"enabled", "disabled"}:
            raise MaintenanceError(f"units.unsupported_enablement:{unit}:{state}")
        result["unit_enablement"][unit] = state
        path = SYSTEMD_UNIT_ROOT / (unit + ".d") / "90-cold-maintenance.conf"
        if path.exists():
            raise MaintenanceError(f"units.preexisting_fence:{path}")
        dropins.append(path)
    result["fence"] = str(fence)
    result["dropins"] = [str(p) for p in dropins]
    phase("prepared")
    guard = preflight.default_storage_guard_probe()
    if not guard.active or guard.latched:
        raise MaintenanceError("common.storage_guard")
    idle_sessions(base)
    process_inventory(target)
    if baseline.compare(peer, baseline.capture(supervisor)):
        raise MaintenanceError("supervisor.zero_drift")
    if baseline.compare(source, baseline.capture(target)):
        raise MaintenanceError("source.runtime_drift")
    transaction.cross_mutation_boundary(record, root=transaction.DEFAULT_TX_ROOT)
    result["mutation_boundary_crossed"] = True
    phase("fencing")
    fence.write_text("Cold migration: do not start this source.\n")
    transaction.register_owned(record, str(fence), root=transaction.DEFAULT_TX_ROOT)
    for path in dropins:
        path.parent.mkdir(exist_ok=True)
        path.write_text(
            "[Unit]\nConditionPathExists=!"
            + str(fence)
            + "\n[Service]\nRestart=no\nTimeoutStopSec=infinity\nSendSIGKILL=no\n"
        )
        transaction.register_owned(record, str(path), root=transaction.DEFAULT_TX_ROOT)
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "disable", *units], check=True)
    phase("admission-stopping")
    # Uvicorn closes admission first. A timeout never authorizes a forced kill.
    subprocess.run(["systemctl", "--no-block", "stop", target.service_unit], check=True)
    wait_stopped(target.service_unit)
    process_inventory(target)
    phase("execution-stopping")
    subprocess.run(["systemctl", "--no-block", "stop", target.host_unit], check=True)
    wait_stopped(target.host_unit)
    if process_inventory(target):
        raise MaintenanceError("source.remaining_processes")
    if baseline.compare(peer, baseline.capture(supervisor)):
        raise MaintenanceError("supervisor.zero_drift")
    result["frozen_database_sha256"] = host_promotion._sha(database)
    result["frozen_at"] = time.time()
    result["recovery"] = {
        "runtime": record.old_runtime_path,
        "database": str(database),
        "pre_stop_backup": str(backup),
        "policy": "Inspect current data; never automatically restore the pre-stop backup.",
        "fence": str(fence),
    }
    phase("source-frozen")
    transaction.advance(record, "tx_committed", root=transaction.DEFAULT_TX_ROOT)
    return result


def wait_stopped(unit: str) -> None:
    deadline = time.monotonic() + 90
    while True:
        output = subprocess.check_output(
            ["systemctl", "show", unit, "-p", "ActiveState", "-p", "MainPID"], text=True
        )
        fields = dict(line.split("=", 1) for line in output.splitlines())
        if fields == {"MainPID": "0", "ActiveState": "inactive"}:
            return
        if fields.get("ActiveState") == "failed" or time.monotonic() > deadline:
            raise MaintenanceError(f"source.still_draining:{unit}:{fields}; no force kill")
        time.sleep(1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=("O1", "O2"), required=True)
    parser.add_argument("--supervisor", choices=("O1", "O2"), required=True)
    parser.add_argument("--acceptance-record", type=Path, required=True)
    parser.add_argument("--expected", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    expected = json.loads(args.expected.read_text())
    result = freeze(
        target=identity.get(args.target),
        supervisor=identity.get(args.supervisor),
        record_path=args.acceptance_record,
        expected_machine_id=expected["source"]["machine_id"],
        expected_supervisor=expected["supervisor_baseline"],
        apply=args.apply,
    )
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
