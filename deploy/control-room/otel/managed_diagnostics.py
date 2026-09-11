#!/usr/bin/env python3
"""Own child stdout/stderr descriptors and drain even when optional logs pause."""

from __future__ import annotations

import argparse
import json
import os
import selectors
import signal
import subprocess
import time
from pathlib import Path

from managed_budget import load_policy
from managed_storage import BoundedFiles, BoundedLog, StoragePaused, exclusive

STATUS_RESERVE = 262144
CHUNK_BYTES = 16384


def collector_rotation_environment(policy):
    """Nominal rotation targets only: asynchronous cleanup is NOT a hard cap."""
    archive = policy["allocations"]["otel_archive"]
    forensic = policy["forensic_max_bytes"]
    segment = 16 * 1024**2
    lean_target = (archive - forensic) * 7 // 8
    targets = {
        "LOGS": lean_target // 5,
        "TRACES": lean_target * 3 // 5,
        "METRICS": lean_target // 5,
        "FORENSIC": forensic * 7 // 8,
    }
    if any(target < 2 * segment for target in targets.values()):
        raise StoragePaused("archive_allocation_too_small_for_rotation")
    return {
        "CONTROL_ROOM_" + key + "_BACKUPS": str(target // segment - 1)
        for key, target in targets.items()
    }


def supervise(command, root, name, allocation, *, segment_bytes=1024**2):
    """No child descriptor points at a rotated pathname; no disk queue is used.

    All Collector/retention logs, including legacy files, share 3/4 of the log
    allocation. The remaining 1/4 belongs to provenance. Kernel pipes and two
    fixed read chunks are memory only; pressure drains/discards with gap counts.
    """
    root = Path(root)
    if name not in {"collector", "retention"} or root.resolve() != root or not root.is_dir():
        raise StoragePaused("diagnostic_root_or_name_unknown")
    status = {
        "schema_version": 1,
        "name": name,
        "accepted_bytes": 0,
        "dropped_bytes": 0,
        "pause_events": 0,
        "last_pause": None,
        "state": "STARTING",
        "allocation_bytes": allocation,
        "child_pid": None,
    }
    status_path = root / ("managed-" + name + "-status.json")
    try:
        with status_path.open("rb") as previous_status:
            prior = json.loads(previous_status.read(4097))
        for key in ("accepted_bytes", "dropped_bytes", "pause_events"):
            value = prior[key]
            if type(value) is not int or not 0 <= value <= 2**63 - 1:
                raise ValueError("invalid diagnostic counter")
            status[key] = value
        status["last_pause"] = prior.get("last_pause")
    except FileNotFoundError:
        pass
    except (OSError, ValueError, KeyError):
        status["history_unknown"] = True
    logs = {
        key: BoundedLog(
            root / ("managed-" + name + "-" + key + ".log"),
            segment_bytes=segment_bytes,
            allocation=allocation - STATUS_RESERVE,
        )
        for key in ("stdout", "stderr")
    }

    def snapshot():
        status["observed_at_unix"] = time.time()
        try:
            with exclusive(root / ".managed-logs.lock"):
                BoundedFiles(root, allocation, record_limit=4096).write(
                    status_path,
                    (json.dumps(status) + "\n").encode(),
                )
        except (OSError, StoragePaused):
            # A missing/stale status is unknown, never evidence of successful logging.
            pass

    with exclusive(root / (".managed-" + name + ".lock")):
        snapshot()
        environment = dict(os.environ)
        if name == "collector":
            environment.update(collector_rotation_environment(load_policy()))
        child = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment
        )
        status.update(child_pid=child.pid, state="RUNNING")
        previous = {}

        def forward(signum, _frame):
            if child.poll() is None:
                child.send_signal(signum)

        try:
            for signum in (signal.SIGTERM, signal.SIGINT):
                previous[signum] = signal.signal(signum, forward)
            with selectors.DefaultSelector() as selector:
                for key, stream in (("stdout", child.stdout), ("stderr", child.stderr)):
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, selectors.EVENT_READ, key)
                last_snapshot = 0
                while selector.get_map():
                    for key, _events in selector.select(timeout=1):
                        data = os.read(key.fd, CHUNK_BYTES)
                        if not data:
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                            continue
                        try:
                            logs[key.data].write(data.decode("utf-8", errors="replace"))
                            status["accepted_bytes"] = min(
                                2**63 - 1, status["accepted_bytes"] + len(data)
                            )
                            status["state"] = (
                                "RUNNING_WITH_GAPS" if status["dropped_bytes"] else "RUNNING"
                            )
                        except (StoragePaused, OSError) as exc:
                            status["dropped_bytes"] = min(
                                2**63 - 1, status["dropped_bytes"] + len(data)
                            )
                            status["pause_events"] = min(2**63 - 1, status["pause_events"] + 1)
                            status["state"] = "PAUSED"
                            status["last_pause"] = str(exc)[:160]
                    if time.monotonic() - last_snapshot >= 1:
                        snapshot()
                        last_snapshot = time.monotonic()
            code = child.wait()
            status.update(
                state="EXITED_WITH_GAPS" if status["dropped_bytes"] else "EXITED", exit_code=code
            )
            snapshot()
            return code if code >= 0 else 128 - code
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
            for stream in (child.stdout, child.stderr):
                stream.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--name", choices=("collector", "retention"), required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("child command required")
    return supervise(
        command,
        args.home / "collector-logs",
        args.name,
        load_policy()["allocations"]["telemetry_logs"] * 3 // 4,
    )


if __name__ == "__main__":
    raise SystemExit(main())
