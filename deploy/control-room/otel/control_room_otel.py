# ruff: noqa: E501
# ruff: noqa: UP031
#!/usr/bin/env python3
"""Dependency-free Control Room OTel config merge and archive inspection."""

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request

try:
    import tomllib
except ImportError:
    tomllib = None
import contextlib
from pathlib import Path

OTEL_HOME = Path(
    os.environ.get(
        "CONTROL_ROOM_OTEL_HOME", Path.home() / "Library/Application Support/ControlRoom/otel"
    )
)
CODEX = Path.home() / ".codex/config.toml"
ARCHIVE_MAX_AGE_DAYS = 60
ARCHIVE_MAX_BYTES = 50_000_000_000
FORENSIC_MAX_AGE_DAYS = 3
FORENSIC_MAX_BYTES = 4_000_000_000
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


def metric(body, name, signal=None):
    total = 0.0
    for line in body.splitlines():
        if line.startswith(name + ("{" if "{" in line else " ")):
            if signal and (f'type="{signal}"' not in line and f'data_type="{signal}"' not in line):
                continue
            with contextlib.suppress(Exception):
                total += float(line.rsplit(" ", 1)[1])
    return int(total)


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
    removed_paths = set()

    def remove(item, reason):
        nonlocal total
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
            removed.append({"path": item["relative"], "bytes": item["size"], "reason": reason})
            return True
        except (FileNotFoundError, OSError, RuntimeError) as exc:
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
    return {
        "total_bytes": total,
        "logs_bytes": by_signal["logs"],
        "traces_bytes": by_signal["traces"],
        "metrics_bytes": by_signal["metrics"],
        "forensic_bytes": sum(x["size"] for x in files if x["relative"].startswith("forensic/")),
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


def report(since=3600):
    body = prom()
    arc = archives(since)
    storage = storage_summary(since)
    running = launchd_loaded("com.controlroom.otelcol")
    retention_loaded = launchd_loaded("com.controlroom.otel-retention")
    retention_last = None
    with contextlib.suppress(Exception):
        retention_last = json.loads((OTEL_HOME / "state/retention-last-run.json").read_text())
    version = "unknown"
    b = OTEL_HOME / "bin/otelcol-contrib"
    if b.exists():
        with contextlib.suppress(Exception):
            version = subprocess.check_output(
                [b, "--version"], text=True, stderr=subprocess.STDOUT
            ).strip()
    data = {
        "state": "FAILED",
        "collector_running": running,
        "collector_version": version,
        "retention_agent_loaded": retention_loaded,
        "retention_last_run": retention_last,
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
    malformed = sum(x["malformed"] for x in arc.values())
    activity = sum(x["records"] for x in arc.values())
    failures = sum(
        data["collector"][k] for k in data["collector"] if "failed" in k or "refused" in k
    )
    retention_bad = (OTEL_HOME / "bin/control_room_otel.py").exists() and (
        not retention_loaded
        or (
            retention_last is not None
            and (retention_last.get("errors") or not retention_last.get("converged", False))
        )
    )
    data["state"] = (
        "FAILED"
        if not running or not body
        else (
            "DEGRADED"
            if failures or malformed or retention_bad
            else ("HEALTHY" if activity else "NO ACTIVITY")
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
        print("Collector integrity:", " ".join("{}={}".format(*x) for x in d["collector"].items()))
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
            atomic_write(
                OTEL_HOME / "state/retention-last-run.json",
                (json.dumps(result, indent=2) + "\n").encode(),
            )
        sys.exit(0 if result["converged"] and not result["errors"] else 1)
    m = re.fullmatch(r"(\d+)([smhd])", a.since)
    secs = int(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)] if m else 3600
    d = report(secs)
    print(json.dumps(d, indent=2) if a.json else "")
    if not a.json:
        human(d, a.cmd == "status")
    if a.cmd == "check" and d["state"] in ("FAILED", "DEGRADED"):
        sys.exit(1)


if __name__ == "__main__":
    main()
