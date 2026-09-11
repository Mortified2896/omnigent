"""Owned-root accounting and serialized, bounded optional file writes.

These bounds apply only to cooperating writers. Native rollout and Collector
coverage must be verified separately before declaring total-budget enforcement.
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

from managed_budget import COMPONENTS


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

    def __init__(self, path, segment_bytes=1024**2, backups=3, record_bytes=65536):
        self.path = Path(path)
        self.segment_bytes = segment_bytes
        self.backups = backups
        self.record_bytes = min(record_bytes, segment_bytes)

    def write(self, text):
        data = text.encode()
        if len(data) > self.record_bytes:
            data = b'{"optional_telemetry_paused":"oversized_log_record"}\n'[: self.record_bytes]
        if not self.path.parent.is_dir() or self.path.resolve() != self.path:
            raise StoragePaused("log_root_unknown")
        with exclusive(self.path.parent / ("." + self.path.name + ".lock")):
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
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                view = memoryview(data)
                while view:
                    written = os.write(fd, view)
                    view = view[written:]
            finally:
                os.close(fd)


def main():
    import argparse

    from managed_budget import load_policy

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("roots", type=Path)
    args = parser.parse_args()
    roots = json.loads(args.roots.read_text())
    result = measure(roots)
    result["total_max_bytes"] = load_policy()["total_max_bytes"]
    result["measured_bytes"] = sum(v["bytes"] for v in (result["components"] or {}).values())
    result["status"] = "INCOMPLETE"
    result["reason"] = "writer_coverage_and_inflight_bytes_unverified"
    print(json.dumps(result, indent=2, sort_keys=True))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
