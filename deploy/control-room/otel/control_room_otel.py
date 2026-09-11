#!/usr/bin/env python3
# ruff: noqa: E501
# ruff: noqa: UP031
"""Dependency-free Control Room OTel config merge and archive inspection."""

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from decimal import Decimal, InvalidOperation

try:
    import tomllib
except ImportError:
    tomllib = None
import contextlib
from pathlib import Path

from managed_budget import load_policy
from managed_storage import BoundedFiles

OTEL_HOME = Path(
    os.environ.get(
        "CONTROL_ROOM_OTEL_HOME", Path.home() / "Library/Application Support/ControlRoom/otel"
    )
)
CODEX = Path.home() / ".codex/config.toml"
POLICY = load_policy()
ARCHIVE_MAX_AGE_DAYS = POLICY["retention_days"]["lean"]
ARCHIVE_MAX_BYTES = POLICY["allocations"]["otel_archive"]
FORENSIC_MAX_AGE_DAYS = POLICY["retention_days"]["forensic"]
FORENSIC_MAX_BYTES = POLICY["forensic_max_bytes"]
RETENTION_MAX_LAG_SECONDS = 900
ACTIVE_ARCHIVE_FILES = {
    "lean/logs/logs.otlp.json",
    "lean/traces/traces.otlp.json",
    "lean/metrics/metrics.otlp.json",
    "forensic/traces/traces.otlp.json",
}
ARCHIVE_FILE_RE = re.compile(
    r"^(?:lean/)?(?:logs/logs|traces/traces|metrics/metrics)\.otlp(?:-[^/]+)?\.json(?:\.zst)?$"
    r"|^forensic/traces/traces\.otlp(?:-[^/]+)?\.json(?:\.zst)?$"
)
BLOCK = """[otel]\nenvironment = "mac-local"\nlog_user_prompt = false\nexporter = { otlp-http = { endpoint = "http://127.0.0.1:4318/v1/logs", protocol = "binary" } }\ntrace_exporter = { otlp-http = { endpoint = "http://127.0.0.1:4318/v1/traces", protocol = "binary" } }\nmetrics_exporter = { otlp-http = { endpoint = "http://127.0.0.1:4318/v1/metrics", protocol = "binary" } }\n"""
TEST_BLOCKS = {
    "minimal": """[otel]\nenvironment = "mac-local"\nlog_user_prompt = false\n""",
    "logs": """[otel]\nenvironment = "mac-local"\nlog_user_prompt = false\nexporter = { otlp-http = { endpoint = "http://127.0.0.1:4318/v1/logs", protocol = "binary" } }\n""",
    "traces": """[otel]\nenvironment = "mac-local"\nlog_user_prompt = false\nexporter = { otlp-http = { endpoint = "http://127.0.0.1:4318/v1/logs", protocol = "binary" } }\ntrace_exporter = { otlp-http = { endpoint = "http://127.0.0.1:4318/v1/traces", protocol = "binary" } }\n""",
    "metrics": BLOCK,
}


def valid_toml(text):
    if tomllib is not None:
        try:
            tomllib.loads(text)
            return True
        except (ValueError, TypeError, OSError):
            return False
    for line in text.splitlines():
        if re.match(r"^[+@>]", line):
            return False
    return text.count("[") == text.count("]")


def atomic_write(path, data):
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        mode = path.stat().st_mode if path.exists() else 0o600
        os.chmod(tmp, mode)
        os.replace(tmp, str(path))
    except (ValueError, TypeError, OSError):
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def corrupt_otel(text):
    lines, start, end = otel_range(text)
    if start is None:
        return False
    return any(re.match(r"^[+@>]", line) for line in lines[start:end])


def list_backups(path):
    return sorted(path.parent.glob(path.name + ".backup-*"), reverse=True)


def rollback(path, backup=None, list_only=False):
    backups = list_backups(path)
    if list_only:
        for b in backups:
            print(b.name)
        return 0
    if not backups:
        raise SystemExit(f"no timestamped backups found for {path}")
    chosen = path.parent / backup if backup else backups[0]
    if backup and chosen not in backups:
        raise SystemExit(f"backup not found: {backup}")
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if path.exists():
        snap = path.with_name(path.name + ".pre-rollback-" + stamp)
        snap.write_bytes(path.read_bytes())
    atomic_write(path, chosen.read_bytes())
    print(f"restored {path} from {chosen.name}")
    return 0


def otel_range(text):
    lines = text.splitlines(True)
    start = None
    for i, line in enumerate(lines):
        if re.match(r"^\[otel(?:\.|\])", line):
            if start is None:
                start = i
        elif start is not None and line.startswith("["):
            return lines, start, i
    return lines, start, len(lines)


def configure(path):
    text = path.read_text() if path.exists() else ""
    lines, start, end = otel_range(text)
    if start is not None:
        existing = "".join(lines[start:end]).strip()
        if existing == BLOCK.strip():
            return False
        if corrupt_otel(text):
            merged = "".join(lines[:start]) + BLOCK + "".join(lines[end:])
        else:
            raise SystemExit("conflicting existing [otel] configuration; refusing to overwrite")
    else:
        merged = text.rstrip() + "\n\n" + BLOCK
    if not valid_toml(merged):
        raise SystemExit(f"generated config.toml failed TOML validation; nothing written ({path})")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if path.exists():
        path.with_name(path.name + ".backup-" + stamp).write_bytes(path.read_bytes())
    atomic_write(path, merged.encode())
    return True


def stage_test(path, variant):
    text = path.read_text() if path.exists() else ""
    lines, start, end = otel_range(text)
    merged = (
        "".join(lines[:start]) + "".join(lines[end:]) if start is not None else text
    ).rstrip()
    merged += "\n\n" + TEST_BLOCKS[variant]
    if not valid_toml(merged):
        raise SystemExit(f"staged config.toml failed TOML validation; nothing written ({path})")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    checkpoint = path.with_name(path.name + ".control-room-known-good")
    if not checkpoint.exists() and path.exists():
        atomic_write(checkpoint, path.read_bytes())
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if path.exists():
        atomic_write(path.with_name(path.name + ".test-backup-" + stamp), path.read_bytes())
    atomic_write(path, merged.encode())
    print(f"staged {variant}; restore with: {Path(__file__).name} test-restore")
    return 0


def restore_test(path):
    checkpoint = path.with_name(path.name + ".control-room-known-good")
    if not checkpoint.exists():
        raise SystemExit(f"known-good checkpoint not found: {checkpoint}")
    if not valid_toml(checkpoint.read_text()):
        raise SystemExit("known-good checkpoint is not valid TOML")
    atomic_write(path, checkpoint.read_bytes())
    print(f"restored known-good config from {checkpoint.name}")
    return 0


def prom():
    try:
        return urllib.request.urlopen("http://127.0.0.1:8888/metrics", timeout=2).read().decode()
    except (ValueError, TypeError, OSError):
        return ""


def metric(body, name, signal=None, *, exporter=None):
    """Sum observed counter series; absent, invalid or ambiguous means unavailable."""
    sample = re.compile(
        rf"^(?P<family>{re.escape(name)}(?:_total)?)"
        r'(?P<labels>\{(?:[^"\\}]|"(?:\\.|[^"\\])*")*\})?'
        r"[ \t]+(?P<value>\S+)(?:[ \t]+[+-]?\d+)?[ \t]*$"
    )
    label = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*("(?:\\.|[^"\\])*")\s*(?:,\s*|$)')
    prefix = re.compile(rf"^{re.escape(name)}(?:_total)?(?=[{{ \t]|$)")
    families = set()
    seen = set()
    total = 0
    for line in body.splitlines():
        if not prefix.match(line):
            continue
        match = sample.fullmatch(line)
        if match is None:
            return None
        labels = {}
        text = (match.group("labels") or "{}")[1:-1].strip()
        while text:
            item = label.match(text)
            if item is None or item[1] in labels:
                return None
            labels[item[1]] = item[2]
            text = text[item.end() :]
        if signal is not None:
            values = {labels[key] for key in ("type", "data_type") if key in labels}
            if values != {json.dumps(signal)}:
                continue
        if exporter is not None and labels.get("exporter") != json.dumps(exporter):
            continue
        identity = tuple(sorted(labels.items()))
        family = match.group("family")
        families.add(family)
        if len(families) != 1 or identity in seen:
            return None
        seen.add(identity)
        try:
            value = Decimal(match.group("value"))
        except InvalidOperation:
            return None
        if (
            not value.is_finite()
            or value < 0
            or value.adjusted() > 63
            or value != value.to_integral_value()
        ):
            return None
        total += int(value)
    return total if seen else None


# This profile is limited to the reviewed, queue-free Collector configuration.
FILE_ONLY_CONFIG_SHA256 = "ed6faceefee975156d9507b6942895faf25ee4fc6d5ae356dd76c7a2179ec85e"
FILE_ONLY_PROFILE = "otelcol-contrib-0.159.0-file-only"
FILE_EXPORTERS = {
    "logs": ("log_records", ("file/logs",)),
    "spans": ("spans", ("file/traces", "file/forensic_traces")),
    "metric_points": ("metric_points", ("file/metrics",)),
}


def _probe_text(args):
    """Bound read-only process probes; never print command output or errors."""
    text = subprocess.check_output(
        args,
        text=True,
        stderr=subprocess.DEVNULL,
        timeout=3,
        env={**os.environ, "LC_ALL": "C", "TZ": "UTC"},
    )
    if len(text) > 65_536:
        raise ValueError("oversized process evidence")
    return text.strip()


def _collector_process():
    text = _probe_text(["launchctl", "print", f"gui/{os.getuid()}/com.controlroom.otelcol"])
    pids = re.findall(r"(?m)^\s*pid = ([1-9][0-9]*)\s*$", text)
    blocks = re.findall(r"(?ms)^\s*arguments = \{\n(.*?)^\s*\}\s*$", text)
    if len(pids) != 1 or len(blocks) != 1:
        raise ValueError("unrecognized loaded process")
    arguments = [line.strip() for line in blocks[0].splitlines() if line.strip()]
    child_args = [
        str(OTEL_HOME / "bin/otelcol-contrib"),
        "--config",
        str(OTEL_HOME / "config/otelcol-macos.yaml"),
    ]
    wrapper_args = [
        "/usr/bin/python3",
        str(OTEL_HOME / "bin/managed_diagnostics.py"),
        "--home",
        str(OTEL_HOME),
        "--name",
        "collector",
        "--",
        *child_args,
    ]
    if arguments == wrapper_args:
        children = _probe_text(["pgrep", "-P", pids[0]]).split()
        if len(children) != 1 or not children[0].isdigit():
            raise ValueError("unrecognized Collector supervisor child")
        if _probe_text(["ps", "-p", children[0], "-o", "command="]) != " ".join(child_args):
            raise ValueError("unexpected Collector child command")
        return children[0], child_args
    return pids[0], arguments


def _process_started(pid):
    text = _probe_text(["ps", "-p", pid, "-o", "lstart="])
    # The subprocess uses UTC/C locale; avoid the parent interpreter's locale.
    fields = text.split()
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    if len(fields) != 5 or fields[1] not in months:
        raise ValueError("unrecognized process start")
    hour, minute, second = map(int, fields[3].split(":"))
    return dt.datetime(
        int(fields[4]),
        months.index(fields[1]) + 1,
        int(fields[2]),
        hour,
        minute,
        second,
        tzinfo=dt.timezone.utc,
    ).timestamp()


def collector_counter_profile(version, scrape_started_at):
    """Bind absence rules to one running Mac binary/configuration and listener."""
    unknown = {"status": "unavailable", "reason": "runtime_profile_unverified"}
    if sys.platform != "darwin":
        return unknown | {"reason": "not_macos"}
    if version not in {"otelcol-contrib version 0.159.0", "otelcol-contrib 0.159.0"}:
        return unknown | {"reason": "unqualified_collector_version"}
    try:
        binary = OTEL_HOME / "bin/otelcol-contrib"
        config = OTEL_HOME / "config/otelcol-macos.yaml"
        dependencies = tuple(
            OTEL_HOME / "bin" / name
            for name in (
                "managed_diagnostics.py",
                "managed_storage.py",
                "managed_budget.py",
                "managed_storage_policy.json",
            )
            if (OTEL_HOME / "bin" / name).exists()
        )
        files = (binary, config, *dependencies)
        if any(path.is_symlink() or not path.is_file() for path in files):
            return unknown | {"reason": "unsafe_or_missing_profile_file"}
        before = tuple(path.stat() for path in files)
        if before[1].st_size > 65_536:
            return unknown | {"reason": "unqualified_configuration"}
        config_hash = hashlib.sha256(config.read_bytes()).hexdigest()
        if config_hash != FILE_ONLY_CONFIG_SHA256:
            return unknown | {"reason": "unqualified_configuration"}
        pid, arguments = _collector_process()
        if arguments != [str(binary), "--config", str(config)]:
            return unknown | {"reason": "loaded_configuration_mismatch"}
        started = _process_started(pid)
        if started > scrape_started_at or any(
            max(item.st_mtime, item.st_ctime) >= started for item in before
        ):
            return unknown | {"reason": "files_or_process_changed_since_launch"}
        listeners = set(
            _probe_text(
                [
                    "lsof",
                    "-nP",
                    "-a",
                    "-iTCP@127.0.0.1:8888",
                    "-sTCP:LISTEN",
                    "-t",
                ]
            ).split()
        )
        after = tuple(path.stat() for path in files)
        if (
            listeners != {pid}
            or _collector_process() != (pid, arguments)
            or _process_started(pid) != started
            or any(
                (a.st_dev, a.st_ino, a.st_size, a.st_mtime_ns, a.st_ctime_ns)
                != (b.st_dev, b.st_ino, b.st_size, b.st_mtime_ns, b.st_ctime_ns)
                for a, b in zip(before, after)  # noqa: B905 - fixed tuples; launchd Python 3.9
            )
        ):
            return unknown | {"reason": "runtime_binding_changed_or_mismatched"}
    except (OSError, ValueError, OverflowError, subprocess.SubprocessError):
        return unknown
    return {
        "status": "verified",
        "profile": FILE_ONLY_PROFILE,
        "config_sha256": config_hash,
        "scope": "local_process_and_reviewed_config_not_end_to_end_delivery",
    }


def counter_evidence(body, counters, profile):
    """Explain nulls only under qualified runtime evidence; never manufacture zero."""
    evidence = {
        key: {"status": "observed" if value is not None else "unavailable"}
        for key, value in counters.items()
    }
    if profile.get("status") != "verified" or profile.get("profile") != FILE_ONLY_PROFILE:
        return evidence
    for suffix, (metric_suffix, exporters) in FILE_EXPORTERS.items():
        sent_name = f"otelcol_exporter_sent_{metric_suffix}"
        active = all((metric(body, sent_name, exporter=name) or 0) > 0 for name in exporters)
        for prefix, family, status, reason in (
            (
                "export_failed",
                "send_failed",
                "conditionally_absent",
                "failure_series_only_created_on_failure",
            ),
            (
                "enqueue_failed",
                "enqueue_failed",
                "not_applicable",
                "reviewed_file_exporters_have_no_sending_queue",
            ),
        ):
            key = f"{prefix}_{suffix}"
            name = f"otelcol_exporter_{family}_{metric_suffix}"
            # Malformed/ambiguous samples are not the same as an absent family.
            present = re.search(rf"(?m)^\s*{re.escape(name)}(?:_total)?(?=[{{\s]|$)", body)
            if key in evidence and counters[key] is None and not present and active:
                evidence[key] = {"status": status, "reason": reason}
    return evidence


def retention_evidence(record, *, now=None, max_lag_seconds=RETENTION_MAX_LAG_SECONDS):
    """Check a cleanup result against the archive, policy and freshness window."""
    result = {"status": "invalid", "lag_seconds": None, "max_lag_seconds": max_lag_seconds}
    if record is None:
        return result | {"status": "unavailable"}
    if not isinstance(record, dict):
        return result
    expected = {
        "max_bytes": ARCHIVE_MAX_BYTES,
        "max_age_days": ARCHIVE_MAX_AGE_DAYS,
        "forensic_max_bytes": FORENSIC_MAX_BYTES,
        "forensic_max_age_days": FORENSIC_MAX_AGE_DAYS,
    }
    if (
        record.get("dry_run") is not False
        or type(record.get("converged")) is not bool
        or not isinstance(record.get("errors"), list)
        or any(
            type(record.get(key)) is not int or record[key] != value
            for key, value in expected.items()
        )
        or not isinstance(record.get("archive_root"), str)
        or not isinstance(record.get("run_at"), str)
    ):
        return result
    try:
        if Path(record["archive_root"]).resolve() != (OTEL_HOME / "data").resolve():
            return result
        stamp = dt.datetime.fromisoformat(record["run_at"])
        if stamp.tzinfo is None:
            return result
        current = now if now is not None else dt.datetime.now(dt.timezone.utc).timestamp()
        lag = current - stamp.timestamp()
    except (ValueError, TypeError, OSError, OverflowError, RuntimeError):
        return result
    if lag < 0:
        return result
    result["lag_seconds"] = lag
    if not record["converged"] or record["errors"]:
        return result | {"status": "failed"}
    return result | {"status": "stale" if lag > max_lag_seconds else "verified"}


def _is_within(path, root):
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def archive_inventory(archive_root=None):
    """Return safe, non-symlink files below the Mac Codex archive root."""
    root = (archive_root or OTEL_HOME / "data").resolve()
    if not root.exists():
        return root, []
    files = []
    for path in root.rglob("*"):
        try:
            if path.is_symlink() or not path.is_file():
                continue
            resolved = path.resolve(strict=True)
            if not _is_within(resolved, root):
                continue
            st = path.stat()
            files.append(
                {
                    "path": path,
                    "relative": path.relative_to(root).as_posix(),
                    "size": st.st_size,
                    "mtime": st.st_mtime,
                    "inode": st.st_ino,
                }
            )
        except (FileNotFoundError, OSError, ValueError):
            continue
    return root, files


def retention_candidate(item):
    return (
        bool(ARCHIVE_FILE_RE.fullmatch(item["relative"]))
        and item["relative"] not in ACTIVE_ARCHIVE_FILES
    )


def prune_archive(
    archive_root=None,
    max_bytes=ARCHIVE_MAX_BYTES,
    max_age_days=ARCHIVE_MAX_AGE_DAYS,
    forensic_max_bytes=FORENSIC_MAX_BYTES,
    forensic_max_age_days=FORENSIC_MAX_AGE_DAYS,
    now=None,
    dry_run=False,
):
    root, files = archive_inventory(archive_root)
    now = now if now is not None else dt.datetime.now(dt.timezone.utc).timestamp()
    total = sum(x["size"] for x in files)
    before = total
    removed = []
    errors = []
    removed_count = 0
    removed_bytes = 0
    error_count = 0
    removed_paths = set()

    def remove(item, reason):
        nonlocal total, removed_count, removed_bytes, error_count
        path = item["path"]
        if path in removed_paths:
            return False
        try:
            resolved = path.resolve(strict=True)
            st = path.lstat()
            if path.is_symlink() or not _is_within(resolved, root):
                raise RuntimeError("unsafe path")
            if (
                st.st_ino != item["inode"]
                or st.st_size != item["size"]
                or st.st_mtime != item["mtime"]
            ):
                raise RuntimeError("file changed during retention scan")
            if not dry_run:
                path.unlink()
            total -= item["size"]
            removed_paths.add(path)
            removed_count += 1
            removed_bytes += item["size"]
            if len(removed) < 128:
                removed.append({"path": item["relative"], "bytes": item["size"], "reason": reason})
            return True
        except (FileNotFoundError, OSError, RuntimeError) as exc:
            error_count += 1
            if len(errors) < 128:
                errors.append({"path": item["relative"], "error": str(exc)})
            return False

    candidates = sorted(
        (x for x in files if retention_candidate(x)), key=lambda x: (x["mtime"], x["relative"])
    )
    for item in candidates:
        age_days = (
            forensic_max_age_days if item["relative"].startswith("forensic/") else max_age_days
        )
        if item["mtime"] < now - age_days * 86400:
            remove(item, "age")

    forensic_total = sum(
        x["size"]
        for x in files
        if x["relative"].startswith("forensic/") and x["path"] not in removed_paths
    )
    if forensic_total > forensic_max_bytes:
        for item in candidates:
            if forensic_total <= forensic_max_bytes:
                break
            if item["relative"].startswith("forensic/") and item["path"] not in removed_paths:
                if remove(item, "forensic_size"):
                    forensic_total -= item["size"]

    if total > max_bytes:
        for item in candidates:
            if total <= max_bytes:
                break
            if item["path"] not in removed_paths:
                remove(item, "global_size")

    return {
        "archive_root": str(root),
        "before_bytes": before,
        "after_bytes": total,
        "run_at": dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat(),
        "max_bytes": max_bytes,
        "max_age_days": max_age_days,
        "forensic_max_bytes": forensic_max_bytes,
        "forensic_max_age_days": forensic_max_age_days,
        "dry_run": dry_run,
        "removed": removed,
        "removed_count": removed_count,
        "removed_bytes": removed_bytes,
        "error_count": error_count,
        "errors": errors,
        "converged": total <= max_bytes and forensic_total <= forensic_max_bytes,
    }


def storage_summary(recent_seconds=86400):
    _root, files = archive_inventory()
    now = dt.datetime.now(dt.timezone.utc).timestamp()
    by_signal = dict.fromkeys(("logs", "traces", "metrics"), 0)
    for item in files:
        parts = Path(item["relative"]).parts
        for signal in by_signal:
            if signal in parts:
                by_signal[signal] += item["size"]
                break
    recognized = [x for x in files if ARCHIVE_FILE_RE.fullmatch(x["relative"])]
    rotated = [x for x in recognized if x["relative"] not in ACTIVE_ARCHIVE_FILES]
    oldest = min(recognized, key=lambda x: (x["mtime"], x["relative"])) if recognized else None
    recent = sum(
        x["size"]
        for x in files
        if x["mtime"] >= now - recent_seconds and x["relative"].startswith(("lean/", "forensic/"))
    )
    total = sum(x["size"] for x in files)
    forensic = sum(x["size"] for x in files if x["relative"].startswith("forensic/"))
    return {
        "budget_scope": "archive_only",
        "ancillary_bounds_verified": False,
        "archive_present": _root.is_dir(),
        "headroom_bytes": max(0, ARCHIVE_MAX_BYTES - total),
        "overshoot_bytes": max(0, total - ARCHIVE_MAX_BYTES),
        "forensic_max_bytes": FORENSIC_MAX_BYTES,
        "forensic_headroom_bytes": max(0, FORENSIC_MAX_BYTES - forensic),
        "forensic_overshoot_bytes": max(0, forensic - FORENSIC_MAX_BYTES),
        "total_bytes": total,
        "logs_bytes": by_signal["logs"],
        "traces_bytes": by_signal["traces"],
        "metrics_bytes": by_signal["metrics"],
        "forensic_bytes": forensic,
        "rotated_files": len(rotated),
        "oldest_retained_file": oldest["relative"] if oldest else None,
        "oldest_retained_mtime": dt.datetime.fromtimestamp(
            oldest["mtime"], dt.timezone.utc
        ).isoformat()
        if oldest
        else None,
        "max_age_days": ARCHIVE_MAX_AGE_DAYS,
        "max_bytes": ARCHIVE_MAX_BYTES,
        "percent_of_max": round(total * 100 / ARCHIVE_MAX_BYTES, 4),
        "approx_recent_bytes_per_hour": round(recent * 3600 / recent_seconds, 2)
        if recent_seconds
        else None,
        "recent_window_seconds": recent_seconds,
    }


def telemetry_files(signal):
    data = OTEL_HOME / "data"
    if not data.exists():
        return []
    return [
        p
        for p in data.rglob("*.json*")
        if p.is_file()
        and not p.is_symlink()
        and signal in p.relative_to(data).parts
        and not p.relative_to(data).as_posix().startswith("forensic/")
    ]


def archives(since):
    cutoff = dt.datetime.now(dt.timezone.utc).timestamp() - since
    out = {
        s: {
            "files": 0,
            "records": 0,
            "malformed": 0,
            "items": 0,
            "missing_ids": 0,
            "first": None,
            "last": None,
        }
        for s in ("logs", "traces", "metrics")
    }
    keys = {"logs": "resourceLogs", "traces": "resourceSpans", "metrics": "resourceMetrics"}
    scope_keys = {"logs": "scopeLogs", "traces": "scopeSpans", "metrics": "scopeMetrics"}
    for sig in out:
        for p in telemetry_files(sig):
            if p.stat().st_mtime < cutoff:
                continue
            out[sig]["files"] += 1
            for line in p.open(errors="replace"):
                try:
                    obj = json.loads(line)
                    out[sig]["records"] += 1
                except (ValueError, TypeError, OSError):
                    out[sig]["malformed"] += 1
                    continue

                def walk(x, sig=sig):
                    if isinstance(x, dict):
                        for k, v in x.items():
                            if k in (
                                "timeUnixNano",
                                "observedTimeUnixNano",
                                "startTimeUnixNano",
                                "endTimeUnixNano",
                            ):
                                try:
                                    z = dt.datetime.fromtimestamp(
                                        int(v) / 1e9, dt.timezone.utc
                                    ).isoformat()
                                    out[sig]["first"] = min(filter(None, [out[sig]["first"], z]))
                                    out[sig]["last"] = max(filter(None, [out[sig]["last"], z]))
                                except (ValueError, TypeError, OSError):
                                    pass
                            walk(v)
                    elif isinstance(x, list):
                        for v in x:
                            walk(v)

                walk(obj)
                root = obj.get(keys[sig], [])
                for resource in root:
                    for scope in resource.get(scope_keys[sig], []):
                        if sig == "logs":
                            items = scope.get("logRecords", [])
                        elif sig == "traces":
                            items = scope.get("spans", [])
                            out[sig]["missing_ids"] += sum(
                                not x.get("traceId") or not x.get("spanId") for x in items
                            )
                        else:
                            items = []
                            for m in scope.get("metrics", []):
                                for kind in (
                                    "gauge",
                                    "sum",
                                    "histogram",
                                    "exponentialHistogram",
                                    "summary",
                                ):
                                    items.extend(m.get(kind, {}).get("dataPoints", []))
                        out[sig]["items"] += len(items)
    return out


def configured():
    if not CODEX.exists():
        return False
    t = CODEX.read_text(errors="replace")
    return "[otel]" in t and "127.0.0.1:4318" in t and "log_user_prompt = false" in t


def launchd_loaded(label):
    try:
        return (
            subprocess.run(
                ["launchctl", "print", "gui/%d/%s" % (os.getuid(), label)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ).returncode
            == 0
        )
    except (ValueError, TypeError, OSError):
        return False


def report(since=3600, retention_max_lag=RETENTION_MAX_LAG_SECONDS):
    scrape_started_at = dt.datetime.now(dt.timezone.utc).timestamp()
    body = prom()
    arc = archives(since)
    storage = storage_summary(since)
    running = launchd_loaded("com.controlroom.otelcol")
    retention_loaded = launchd_loaded("com.controlroom.otel-retention")
    retention_last = None
    try:
        retention_last = json.loads((OTEL_HOME / "state/retention-last-run.json").read_text())
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        retention_last = {}
    retention = retention_evidence(retention_last, max_lag_seconds=retention_max_lag)
    version = "unknown"
    b = OTEL_HOME / "bin/otelcol-contrib"
    if b.exists():
        with contextlib.suppress(Exception):
            version = subprocess.check_output(
                [b, "--version"], text=True, stderr=subprocess.STDOUT
            ).strip()
    data = {
        "state": "FAILED",
        "managed_storage": {
            "state": "INCOMPLETE",
            "total_max_bytes": POLICY["total_max_bytes"],
            "allocations": POLICY["allocations"],
            "unassigned_margin_bytes": POLICY["total_max_bytes"]
            - sum(POLICY["allocations"].values()),
            "archive_enforcement": "retention_target_only; external_exporter_not_admitted",
            "native_capture_enforcement": "unbounded_external_codex_trace_writer",
            "capture_cleanup": "paused; native_activity_and_references_uncoordinated",
        },
        "collector_running": running,
        "collector_version": version,
        "retention_agent_loaded": retention_loaded,
        "retention_last_run": retention_last,
        "retention_evidence": retention,
        "endpoints": {
            "otlp_grpc": "127.0.0.1:4317",
            "otlp_http": "127.0.0.1:4318",
            "metrics": "127.0.0.1:8888",
            "health": "127.0.0.1:13133",
        },
        "capture_node_id": (OTEL_HOME / "state/capture_node_id").read_text().strip()
        if (OTEL_HOME / "state/capture_node_id").exists()
        else None,
        "archive": str(OTEL_HOME / "data"),
        "archive_bytes": storage["total_bytes"],
        "storage": storage,
        "codex_configured": configured(),
        "signals": arc,
        "collector": {
            "accepted_logs": metric(body, "otelcol_receiver_accepted_log_records"),
            "accepted_spans": metric(body, "otelcol_receiver_accepted_spans"),
            "accepted_metric_points": metric(body, "otelcol_receiver_accepted_metric_points"),
            "refused_logs": metric(body, "otelcol_receiver_refused_log_records"),
            "refused_spans": metric(body, "otelcol_receiver_refused_spans"),
            "refused_metric_points": metric(body, "otelcol_receiver_refused_metric_points"),
            "exported_logs": metric(body, "otelcol_exporter_sent_log_records"),
            "exported_spans": metric(body, "otelcol_exporter_sent_spans"),
            "exported_metric_points": metric(body, "otelcol_exporter_sent_metric_points"),
            "export_failed_logs": metric(body, "otelcol_exporter_send_failed_log_records"),
            "export_failed_spans": metric(body, "otelcol_exporter_send_failed_spans"),
            "export_failed_metric_points": metric(
                body, "otelcol_exporter_send_failed_metric_points"
            ),
            "enqueue_failed_logs": metric(body, "otelcol_exporter_enqueue_failed_log_records"),
            "enqueue_failed_spans": metric(body, "otelcol_exporter_enqueue_failed_spans"),
            "enqueue_failed_metric_points": metric(
                body, "otelcol_exporter_enqueue_failed_metric_points"
            ),
        },
    }
    data["managed_storage"]["diagnostics"] = {}
    for name, horizon in (("collector", 10), ("retention", retention_max_lag)):
        try:
            path = OTEL_HOME / "collector-logs" / ("managed-" + name + "-status.json")
            if path.is_symlink():
                raise ValueError("unsafe diagnostic status")
            with path.open("rb") as handle:
                item = json.loads(handle.read(4097))
            age = dt.datetime.now(dt.timezone.utc).timestamp() - item["observed_at_unix"]
            item["freshness"] = "current" if 0 <= age <= horizon else "stale"
        except (OSError, ValueError, TypeError, KeyError):
            item = {"freshness": "unavailable"}
        data["managed_storage"]["diagnostics"][name] = item
    malformed = sum(x["malformed"] for x in arc.values())
    activity = sum(x["records"] for x in arc.values())
    data["unavailable_counters"] = [
        key for key, value in data["collector"].items() if value is None
    ]
    profile = collector_counter_profile(version, scrape_started_at)
    data["counter_profile"] = profile
    data["counter_evidence"] = counter_evidence(body, data["collector"], profile)
    data["unexplained_counters"] = [
        key for key, item in data["counter_evidence"].items() if item["status"] == "unavailable"
    ]
    failures = sum(
        value
        for key, value in data["collector"].items()
        if ("failed" in key or "refused" in key) and value is not None
    )
    retention_bad = not retention_loaded or retention["status"] == "failed"
    over_budget = storage["overshoot_bytes"] > 0 or storage["forensic_overshoot_bytes"] > 0
    incomplete = (
        data["unexplained_counters"]
        or retention["status"] != "verified"
        or not storage["archive_present"]
    )
    data["state"] = (
        "FAILED"
        if not running or not body
        else (
            "DEGRADED"
            if failures or malformed or retention_bad or over_budget
            else ("INCOMPLETE" if incomplete else ("HEALTHY" if activity else "NO ACTIVITY"))
        )
    )
    return data


def human(d, status=False):
    print("Collector:", "running" if d["collector_running"] else "not running")
    print("Version:", d["collector_version"])
    print("Retention agent:", "loaded" if d["retention_agent_loaded"] else "not loaded")
    print("Endpoints:", ", ".join(d["endpoints"].values()))
    print("capture_node_id:", d["capture_node_id"])
    print("Archive:", d["archive"], "(%d bytes)" % d["archive_bytes"])
    print("Codex OTel configured:", d["codex_configured"])
    s = d["storage"]
    print("Storage budget scope:", s["budget_scope"], "(ancillary bounds not verified)")
    print("Archive headroom:", s["headroom_bytes"], "overshoot:", s["overshoot_bytes"])
    print("Retention evidence:", d["retention_evidence"]["status"])
    if d["unavailable_counters"]:
        print("Collector counters unavailable:", ", ".join(d["unavailable_counters"]))
    for key, item in d.get("counter_evidence", {}).items():
        if item["status"] in {"conditionally_absent", "not_applicable"}:
            print(f"{key}: {item['status']} ({item['reason']}; raw value unavailable)")
    print(
        "Storage: logs=%d traces=%d metrics=%d forensic=%d rotated=%d oldest=%s"
        % (
            s["logs_bytes"],
            s["traces_bytes"],
            s["metrics_bytes"],
            s["forensic_bytes"],
            s["rotated_files"],
            s["oldest_retained_file"],
        )
    )
    print(
        "Retention: max_age=%dd max_bytes=%d used=%.4f%% approx_recent_bytes_per_hour=%.2f"
        % (
            s["max_age_days"],
            s["max_bytes"],
            s["percent_of_max"],
            s["approx_recent_bytes_per_hour"],
        )
    )
    for s, x in d["signals"].items():
        print(
            "%s: files=%d records=%d groups=%d malformed=%d first=%s last=%s"
            % (s, x["files"], x["records"], x["items"], x["malformed"], x["first"], x["last"])
        )
    if not status:
        print(
            "Collector integrity:",
            " ".join(
                f"{key}={value if value is not None else 'unavailable'}"
                for key, value in d["collector"].items()
            ),
        )
    print(d["state"])


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("configure")
    c.add_argument("--path", type=Path, default=CODEX)
    r = sub.add_parser("rollback")
    r.add_argument("--path", type=Path, default=CODEX)
    r.add_argument("--backup")
    r.add_argument("--list", action="store_true")
    t = sub.add_parser("test-stage")
    t.add_argument("variant", choices=TEST_BLOCKS)
    t.add_argument("--path", type=Path, default=CODEX)
    x = sub.add_parser("test-restore")
    x.add_argument("--path", type=Path, default=CODEX)
    for n in ("status", "check"):
        q = sub.add_parser(n)
        q.add_argument("--json", action="store_true")
        q.add_argument("--since", default="1h")
        q.add_argument("--max-retention-lag-seconds", type=int, default=RETENTION_MAX_LAG_SECONDS)

    k = sub.add_parser("retain")
    k.add_argument("--archive-root", type=Path, default=OTEL_HOME / "data")
    k.add_argument("--max-bytes", type=int, default=ARCHIVE_MAX_BYTES)
    k.add_argument("--max-age-days", type=int, default=ARCHIVE_MAX_AGE_DAYS)
    k.add_argument("--forensic-max-bytes", type=int, default=FORENSIC_MAX_BYTES)
    k.add_argument("--forensic-max-age-days", type=int, default=FORENSIC_MAX_AGE_DAYS)
    k.add_argument("--now", type=float)
    k.add_argument("--dry-run", action="store_true")
    a = p.parse_args()
    if a.cmd == "configure":
        print("changed" if configure(a.path) else "unchanged")
        return
    if a.cmd == "rollback":
        sys.exit(rollback(a.path, a.backup, a.list))
    if a.cmd == "test-stage":
        sys.exit(stage_test(a.path, a.variant))
    if a.cmd == "test-restore":
        sys.exit(restore_test(a.path))
    if a.cmd == "retain":
        if (
            a.archive_root.resolve() != (OTEL_HOME / "data").resolve()
            and os.environ.get("CONTROL_ROOM_OTEL_TESTING") != "1"
        ):
            raise SystemExit("refusing retention outside the configured Mac Codex archive")
        result = prune_archive(
            a.archive_root,
            a.max_bytes,
            a.max_age_days,
            a.forensic_max_bytes,
            a.forensic_max_age_days,
            a.now,
            a.dry_run,
        )
        print(json.dumps(result, indent=2))
        if not a.dry_run and a.archive_root.resolve() == (OTEL_HOME / "data").resolve():
            from managed_storage import exclusive

            state = OTEL_HOME / "state"
            with exclusive(state / ".provenance-adoption.lock"):
                BoundedFiles(state, POLICY["allocations"]["telemetry_backups"]).write(
                    state / "retention-last-run.json",
                    (json.dumps(result, indent=2) + "\n").encode(),
                )
        sys.exit(0 if result["converged"] and not result["errors"] else 1)
    m = re.fullmatch(r"(\d+)([smhd])", a.since)
    secs = int(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)] if m else 3600
    if a.max_retention_lag_seconds <= 0:
        p.error("--max-retention-lag-seconds must be positive")
    d = report(secs, a.max_retention_lag_seconds)
    print(json.dumps(d, indent=2) if a.json else "")
    if not a.json:
        human(d, a.cmd == "status")
    if a.cmd == "check" and d["state"] in ("FAILED", "DEGRADED", "INCOMPLETE"):
        sys.exit(1)


if __name__ == "__main__":
    main()
