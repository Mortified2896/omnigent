"""Owned-root accounting and serialized, bounded optional file writes.

Component bounds apply only to cooperating writers. Aggregate pressure uses a
managed target; external writer overshoot is reported and never blocks tasks.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import json
import os
import stat
import tempfile
from pathlib import Path

from managed_budget import COMPONENTS, assess_budget, load_policy


class StoragePaused(RuntimeError):
    """Optional telemetry cannot safely grow; ordinary work should continue."""


def measure(roots):
    """Count every inode once; unreadable, aliased or changing trees stay unknown.

    Roots have path, component and optional classification prefixes. All bytes
    are conservatively protected here; deletion needs separate reference proof.
    """
    components = {
        k: {"bytes": 0, "logical_bytes": 0, "allocated_bytes": 0, "protected_bytes": 0}
        for k in COMPONENTS
    }
    errors = []
    seen = {}
    paths = []
    for root in roots:
        p = Path(root["path"])
        if not p.is_absolute() or p.resolve() != p:
            errors.append("unsafe_root")
        if any(p == q or p in q.parents or q in p.parents for q in paths):
            errors.append("overlapping_roots")
        paths.append(p)
        if root["component"] not in COMPONENTS or any(
            v not in COMPONENTS for v in root.get("prefixes", {}).values()
        ):
            errors.append("unknown_component")
    if errors:
        return {"complete": False, "components": None, "errors": sorted(set(errors))}

    def visit(p, root, base):
        try:
            s = p.lstat()
            if stat.S_ISLNK(s.st_mode) or not (stat.S_ISREG(s.st_mode) or stat.S_ISDIR(s.st_mode)):
                errors.append("unsafe_entry")
                return
            key = (s.st_dev, s.st_ino)
            if key in seen:
                seen[key][1] += 1
                return
            seen[key] = [s.st_nlink if stat.S_ISREG(s.st_mode) else 1, 1]
            relative = p.relative_to(base).as_posix()
            component = root["component"]
            for prefix, category in sorted(
                root.get("prefixes", {}).items(), key=lambda kv: len(kv[0])
            ):
                if relative == prefix or relative.startswith(prefix + "/"):
                    component = category
            item = components[component]
            logical, allocated = s.st_size, s.st_blocks * 512
            amount = max(logical, allocated)
            item["bytes"] += amount
            item["logical_bytes"] += logical
            item["allocated_bytes"] += allocated
            item["protected_bytes"] += amount
            if stat.S_ISDIR(s.st_mode):
                for child in p.iterdir():
                    visit(child, root, base)
                after = p.lstat()
                if (after.st_ino, after.st_mtime_ns) != (s.st_ino, s.st_mtime_ns):
                    errors.append("inventory_changed")
            elif p.lstat().st_size != s.st_size:
                errors.append("inventory_changed")
        except OSError:
            errors.append("unreadable_or_missing_entry")

    for index, root in enumerate(roots):
        visit(paths[index], root, paths[index])
    if any(expected != found for expected, found in seen.values()):
        errors.append("external_hardlink")
    return {
        "complete": not errors,
        "observed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "components": components,
        "errors": sorted(set(errors)),
        "reserved_bytes": None,
        "enforcement_verified": False,
        "protection": "all_bytes_conservatively_protected; no_deletion_authority",
    }


@contextlib.contextmanager
def exclusive(path):
    """Do not wait on another telemetry operation in an ordinary task's hook."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise StoragePaused("another_telemetry_writer_is_active") from None
        yield
    finally:
        os.close(fd)


class BoundedFiles:
    """Atomic replacement with scratch bytes admitted before creating the file."""

    def __init__(self, root, limit, record_limit=8 * 1024**2):
        self.root = Path(root)
        self.limit = limit
        self.record_limit = record_limit
        if not self.root.is_dir() or self.root.resolve() != self.root:
            raise StoragePaused("missing_or_unsafe_owned_root")

    def write(self, path, data):
        if len(data) > self.record_limit:
            raise StoragePaused("oversized_capture")
        path = Path(path)
        if not path.is_relative_to(self.root) or path.resolve() != path or path == self.root:
            raise StoragePaused("unsafe_capture_path")
        # The lock resides outside the capture tree and is counted as runtime.
        with exclusive(self.root.parent / ("." + self.root.name + ".writer.lock")):
            scan = measure([{"path": str(self.root), "component": "provenance_captures"}])
            if not scan["complete"]:
                raise StoragePaused("capture_inventory_unknown")
            used = scan["components"]["provenance_captures"]["bytes"]
            # Includes allocation rounding, temporary inode and bounded directories.
            reserve = ((len(data) + 4095) // 4096) * 4096 + 65536
            if used + reserve > self.limit:
                raise StoragePaused("capture_allocation_full")
            if len(path.relative_to(self.root).parts) > 4:
                raise StoragePaused("capture_path_too_deep")
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(
                    dir=path.parent, prefix=".capture-", delete=False
                ) as stream:
                    temporary = Path(stream.name)
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)


class BoundedLog:
    """Rotate only this writer's files; open/close descriptors on every record."""

    def __init__(
        self, path, segment_bytes=1024**2, backups=3, record_bytes=65536, allocation=None
    ):
        self.path = Path(path)
        self.segment_bytes = segment_bytes
        self.backups = backups
        self.record_bytes = min(record_bytes, segment_bytes)
        self.allocation = allocation

    def write(self, text):
        data = text.encode()
        if len(data) > self.record_bytes:
            data = b'{"optional_telemetry_paused":"oversized_log_record"}\n'[: self.record_bytes]
        if not self.path.parent.is_dir() or self.path.resolve() != self.path:
            raise StoragePaused("log_root_unknown")
        lock_name = (
            ".managed-logs.lock" if self.allocation is not None else "." + self.path.name + ".lock"
        )
        with exclusive(self.path.parent / lock_name):
            members = [self.path] + [
                Path(str(self.path) + "." + str(i)) for i in range(1, self.backups + 1)
            ]
            for member in members:
                if member.is_symlink() or (
                    member.exists() and member.stat().st_size > self.segment_bytes
                ):
                    raise StoragePaused("unexpected_log_member")
            size = self.path.stat().st_size if self.path.exists() else 0
            if size + len(data) > self.segment_bytes:
                members[-1].unlink(missing_ok=True)
                for source, target in (
                    (members[i], members[i + 1]) for i in range(len(members) - 2, -1, -1)
                ):
                    if source.exists():
                        source.replace(target)
            if self.allocation is not None:
                scan = measure([{"path": str(self.path.parent), "component": "telemetry_logs"}])
                if not scan["complete"]:
                    raise StoragePaused("log_inventory_unknown")
                reserve = ((len(data) + 65535) // 65536) * 65536 + 65536
                if scan["components"]["telemetry_logs"]["bytes"] + reserve > self.allocation:
                    raise StoragePaused("log_allocation_full")
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                view = memoryview(data)
                while view:
                    written = os.write(fd, view)
                    view = view[written:]
            finally:
                os.close(fd)


MANAGEMENT_MAX_AGE = 360


def management_home():
    return Path(
        os.environ.get(
            "CONTROL_ROOM_OTEL_HOME",
            str(Path.home() / "Library/Application Support/ControlRoom/otel"),
        )
    )


def _read_control(path):
    if path.resolve() != path:
        raise ValueError("unsafe_management_path")
    with path.open("rb") as stream:
        data = stream.read(65537)
    if len(data) > 65536:
        raise ValueError("oversized_management_record")
    return json.loads(data)


GAP_REASONS = (
    "managed_pause",
    "writer_lock_busy",
    "storage_allocation",
    "sqlite_or_wal",
    "reconcile_skipped",
    "hook_failure",
    "unknown",
)


def gap_reason(exc):
    """Map known failure codes to fixed labels; never persist exception text."""
    import sqlite3

    if isinstance(exc, sqlite3.Error):
        return "sqlite_or_wal"
    code = exc.args[0] if isinstance(exc, StoragePaused) and exc.args else None
    if not isinstance(code, str):
        return "unknown"
    if code == "another_telemetry_writer_is_active":
        return "writer_lock_busy"
    if code in {
        "database_allocation_full",
        "wal_reader_pinned",
        "wal_allocation_full",
        "oversized_database_record",
    }:
        return "sqlite_or_wal"
    if code in {
        "capture_allocation_full",
        "log_allocation_full",
        "oversized_capture",
        "oversized_untracked_capture",
    }:
        return "storage_allocation"
    return "unknown"


def _gap_counts(prior):
    total = (prior or {}).get("skipped_optional_hooks", 0)
    saved = (prior or {}).get("optional_gap_reasons")
    if (
        not isinstance(saved, dict)
        or set(saved) != set(GAP_REASONS)
        or any(type(v) is not int or not 0 <= v <= 2**63 - 1 for v in saved.values())
        or sum(saved.values()) != total
    ):
        return dict.fromkeys(GAP_REASONS, 0) | {"unknown": total}
    return dict(saved)


def management_status(home=None, *, policy=None, refresh=False, gap=False, reason="unknown"):
    """Persist hysteresis in a bounded control record; never reserve task storage.

    The existing retention timer refreshes the inventory. Hooks only read fresh
    state, or count a skipped hook, so scanning cannot delay ordinary tasks.
    """
    home = Path(home) if home is not None else management_home()
    policy = policy if policy is not None else load_policy()
    now = dt.datetime.now(dt.timezone.utc)
    unavailable = assess_budget({}, policy=policy, now=now, max_age_seconds=MANAGEMENT_MAX_AGE)
    unavailable.update(
        state="INCOMPLETE",
        inventory_complete=False,
        gap_reporting="unavailable",
        optional_capture_gap=True,
    )
    state = home / "state"
    path = state / "managed-target-state.json"

    def read_previous():
        try:
            prior = _read_control(path)
            if (
                not isinstance(prior, dict)
                or type(prior.get("optional_telemetry_pause_requested")) is not bool
                or type(prior.get("skipped_optional_hooks")) is not int
                or not 0 <= prior["skipped_optional_hooks"] <= 2**63 - 1
            ):
                raise ValueError("invalid_management_state")
            return prior
        except (OSError, ValueError, TypeError):
            return None

    def current(prior):
        if prior is None:
            return dict(unavailable)
        try:
            age = (now - dt.datetime.fromisoformat(prior["observed_at"])).total_seconds()
            if not 0 <= age <= MANAGEMENT_MAX_AGE:
                raise ValueError("stale_management_state")
            for key, value in (
                ("target_bytes", policy["total_max_bytes"]),
                *policy["management"].items(),
            ):
                if prior.get(key) != value:
                    raise ValueError("management_policy_changed")
            return dict(prior)
        except (KeyError, TypeError, ValueError):
            return dict(
                unavailable,
                reason="stale_or_changed_management_state",
                skipped_optional_hooks=prior["skipped_optional_hooks"],
            )

    if not refresh and not gap:
        return current(read_previous())
    try:
        if state.resolve() != state or not state.is_dir():
            raise StoragePaused("management_root_unavailable")
        with exclusive(state / ".managed-target.lock"):
            prior = read_previous()
            result = current(prior)
            if refresh:
                try:
                    roots = _read_control(state / "managed-roots.json")
                    if not isinstance(roots, list) or not roots:
                        raise ValueError("missing_managed_roots")
                    scan = measure(roots)
                except (OSError, ValueError, TypeError, KeyError, AttributeError):
                    scan = {
                        "complete": False,
                        "components": None,
                        "errors": ["managed_roots_unavailable"],
                        "observed_at": now.isoformat(),
                    }
                # Evaluate observed disk pressure, not unknown future reservations.
                result = assess_budget(
                    dict(scan, reserved_bytes=0),
                    policy=policy,
                    now=dt.datetime.now(dt.timezone.utc),
                    max_age_seconds=MANAGEMENT_MAX_AGE,
                    optional_paused=(prior or {}).get("optional_telemetry_pause_requested", True),
                )
                result.update(
                    state=result["management_state"],
                    observed_at=scan.get("observed_at", now.isoformat()),
                    inventory_complete=scan["complete"],
                    inventory_errors=scan["errors"],
                    components=scan.get("components"),
                    measurement_basis="per_inode_max_logical_allocated; observed_disk_pressure",
                    protected_bytes_basis="conservative_count; not_deletion_eligibility",
                    reserved_bytes=None,
                    external_inflight_bytes=None,
                    fits_snapshot=None,
                    coverage_limitations=[
                        "Collector/native optional writes may overshoot asynchronously",
                        "Native optional trace writer has no safe live pause interface",
                        "Unknown activity/evidence/rollback references prevent capture deletion",
                        "APFS snapshots/clones and unrelated files are outside this measurement",
                        "Hook inventory may be 360 seconds old; silent producer loss is unknown",
                    ],
                    known_external_writer_overshoot={
                        "writer": "Collector fileexporter 0.159.0",
                        "isolated_test_nominal_bytes": 2097152,
                        "isolated_test_peak_bytes": 9584640,
                        "current_overshoot_attribution": "unknown; see overshoot_bytes",
                    },
                    skipped_optional_hooks=(prior or {}).get("skipped_optional_hooks", 0),
                    gap_reporting="available",
                    gap_count_basis="lower_bound; lock_or_disk_failure_may_prevent_count",
                    last_optional_gap_at=(prior or {}).get("last_optional_gap_at"),
                )
            result["last_optional_gap_at"] = (prior or {}).get("last_optional_gap_at")
            counts = _gap_counts(prior)
            result["optional_gap_reasons"] = counts
            latest = (prior or {}).get("last_optional_gap_reason")
            result["last_optional_gap_reason"] = latest if latest in GAP_REASONS else "unknown"
            if gap:
                reason = reason if reason in GAP_REASONS else "unknown"
                if result.get("skipped_optional_hooks", 0) < 2**63 - 1:
                    counts[reason] += 1
                result["last_optional_gap_reason"] = reason
                result["skipped_optional_hooks"] = min(
                    2**63 - 1, result.get("skipped_optional_hooks", 0) + 1
                )
                result["last_optional_gap_at"] = now.isoformat()
                result["gap_reporting"] = "available"
            result["optional_capture_gap"] = bool(
                result.get("skipped_optional_hooks", 0)
                or result["optional_telemetry_pause_requested"]
            )
            # Control evidence may update during pause; it cannot grow unbounded.
            BoundedFiles(
                state, policy["allocations"]["telemetry_backups"], record_limit=65536
            ).write(path, (json.dumps(result, sort_keys=True) + "\n").encode())
            return result
    except (StoragePaused, OSError, ValueError, KeyError, TypeError):
        return dict(unavailable, reason="management_inventory_or_state_unavailable")


def cleanup_archive_target(management, configured_target):
    """Accelerate only the existing archive candidate cleanup under pressure."""
    if not management.get("inventory_complete") or not management.get("cleanup_requested"):
        return configured_target
    excess = max(0, management["used_bytes"] - management["cleanup_start_bytes"])
    archive_bytes = management["components"]["otel_archive"]["bytes"]
    return min(configured_target, max(0, archive_bytes - excess))


def main():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("roots", type=Path, nargs="?")
    parser.add_argument("--home", type=Path)
    args = parser.parse_args()
    if args.roots:
        result = measure(_read_control(args.roots))
        result["measured_bytes"] = sum(v["bytes"] for v in (result["components"] or {}).values())
    else:
        result = management_status(args.home, refresh=True)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("inventory_complete", result.get("complete")) else 2


if __name__ == "__main__":
    raise SystemExit(main())
