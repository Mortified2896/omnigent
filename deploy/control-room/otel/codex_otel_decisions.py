#!/usr/bin/env python3
"""Tiny provenance sidecar for local Codex turns.

Native rollouts and the existing OTel archive remain canonical. This stores one
row per turn plus otherwise-missing Git START/END reconstruction artifacts.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import io
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import tarfile
from collections.abc import Iterable
from typing import Any

from managed_budget import load_policy
from managed_metadata import cleanup_manifest
from managed_sqlite import BoundedConnection
from managed_storage import (
    BoundedFiles,
    BoundedLog,
    StoragePaused,
    exclusive,
    gap_reason,
    management_status,
)

OTEL = pathlib.Path.home() / "Library/Application Support/ControlRoom/otel/data"
HOME = pathlib.Path.home() / "Library/Application Support/Codex/TelemetryProvenance"
DB = HOME / "provenance.sqlite3"
ARTIFACTS = HOME / "artifacts"
TRACE_ROOT = HOME / "rollout-traces"
SCHEMA_VERSION = 2
POLICY = load_policy()
RETENTION_DAYS = POLICY["retention_days"]["captures"]
RETENTION_BYTES = 10 * 1024**3
FILES = None


def now() -> str:
    return (
        dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )


def parsed(value: str | None) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None
    except ValueError:
        return None


def sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha_file(path: pathlib.Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def component(value: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in value)[:160] or "unknown"


def git(cwd: pathlib.Path, *args: str, check: bool = True) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        timeout=120,
        check=False,
    )
    if check and result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", "replace").strip() or "git failed")
    return result.stdout


def write(path: pathlib.Path, data: bytes) -> dict[str, Any]:
    if FILES is None:
        raise StoragePaused("capture_writer_not_initialized")
    FILES.write(path, data)
    return {"path": str(path), "sha256": sha_bytes(data), "size": len(data)}


def jbytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode()


def connect(path: pathlib.Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    conn = sqlite3.connect(path, timeout=0.1, factory=BoundedConnection)
    conn.configure(path, POLICY["allocations"]["provenance_database"])
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    schema = """
      CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS turns(
        session_id TEXT NOT NULL,turn_id TEXT NOT NULL,start_time TEXT,end_time TEXT,
        start_hook TEXT,end_hook TEXT,cwd TEXT,repo_root TEXT,repo_origin TEXT,
        start_head TEXT,start_branch TEXT,start_detached INTEGER,end_head TEXT,end_branch TEXT,
        model TEXT,permission_mode TEXT,transcript_path TEXT,transcript_start_size INTEGER,
        transcript_end_size INTEGER, transcript_sha256 TEXT, start_artifacts_json TEXT NOT NULL
        DEFAULT '{}',
        full_trace_path TEXT, full_trace_size INTEGER, full_trace_manifest_sha256 TEXT,
        full_trace_pruned_at TEXT,
        full_trace_end_offset INTEGER,full_trace_prefix_sha256 TEXT,
        full_trace_artifacts_json TEXT NOT NULL DEFAULT '[]', full_trace_complete INTEGER NOT
        NULL DEFAULT 0,
        end_artifacts_json TEXT NOT NULL DEFAULT '{}',identities_json TEXT NOT NULL DEFAULT '{}',
        otel_pointers_json TEXT NOT NULL DEFAULT '[]',start_complete INTEGER NOT NULL DEFAULT 0,
        end_complete INTEGER NOT NULL DEFAULT 0,native_complete INTEGER NOT NULL DEFAULT 0,
        otel_complete INTEGER NOT NULL DEFAULT 0,frozen INTEGER NOT NULL DEFAULT 0,
        frozen_at TEXT, pruned_at TEXT, last_error TEXT, created_at TEXT NOT NULL, updated_at
        TEXT NOT NULL,
        PRIMARY KEY(session_id,turn_id));
      CREATE INDEX IF NOT EXISTS turns_start_idx ON turns(start_time);
    """
    for statement in schema.split(";"):
        if statement.strip():
            conn.execute(statement)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(turns)")}
    migrations = {
        "full_trace_end_offset": "INTEGER",
        "full_trace_prefix_sha256": "TEXT",
        "full_trace_artifacts_json": "TEXT NOT NULL DEFAULT '[]'",
        "full_trace_complete": "INTEGER NOT NULL DEFAULT 0",
    }
    for name, declaration in migrations.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE turns ADD COLUMN {name} {declaration}")
    conn.execute("INSERT OR REPLACE INTO meta VALUES('schema_version',?)", (str(SCHEMA_VERSION),))
    conn.commit()
    return conn


def turn_dir(root: pathlib.Path, session: str, turn: str) -> pathlib.Path:
    return root / component(session) / component(turn)


def identity(path: pathlib.Path) -> dict[str, Any]:
    item: dict[str, Any] = {"path": str(path), "exists": path.exists()}
    if path.is_file():
        item.update(sha256=sha_file(path), size=path.stat().st_size)
    return item


def applicable(cwd: pathlib.Path, repo: pathlib.Path | None) -> list[pathlib.Path]:
    paths: list[pathlib.Path] = []
    resolved = cwd.resolve()
    for parent in reversed((resolved, *resolved.parents)):
        for name in ("AGENTS.override.md", "AGENTS.md"):
            if (parent / name).is_file():
                paths.append(parent / name)
    names = {
        "Cargo.lock",
        "Cargo.toml",
        "Gemfile",
        "Gemfile.lock",
        "go.mod",
        "go.sum",
        "package.json",
        "package-lock.json",
        "pnpm-lock.yaml",
        "poetry.lock",
        "pyproject.toml",
        "requirements.txt",
        "uv.lock",
        "yarn.lock",
    }
    if repo:
        with contextlib.suppress(OSError):
            paths += [p for p in repo.iterdir() if p.is_file() and p.name in names]
    return list({str(p): p for p in paths}.values())


def identities(cwd: pathlib.Path, repo: pathlib.Path | None) -> dict[str, Any]:
    app = pathlib.Path("/Applications/ChatGPT.app")
    return {
        "captured_at": now(),
        "platform": {
            "system": os.uname().sysname,
            "release": os.uname().release,
            "machine": os.uname().machine,
        },
        "codex_runtime": identity(app / "Contents/Resources/codex"),
        "app": identity(app / "Contents/Info.plist"),
        "config": identity(pathlib.Path.home() / ".codex/config.toml"),
        "hooks": identity(pathlib.Path.home() / ".codex/hooks.json"),
        "applicable_inputs": [identity(p) for p in sorted(applicable(cwd, repo))],
    }


def metadata(cwd: pathlib.Path) -> dict[str, Any] | None:
    raw = git(cwd, "rev-parse", "--show-toplevel", check=False).strip()
    if not raw:
        return None
    root = pathlib.Path(os.fsdecode(raw)).resolve()
    head = git(root, "rev-parse", "HEAD", check=False).decode().strip() or None
    branch = (
        git(root, "symbolic-ref", "--quiet", "--short", "HEAD", check=False).decode().strip()
        or None
    )
    origin = git(root, "remote", "get-url", "origin", check=False).decode().strip() or None
    return {
        "root": root,
        "head": head,
        "branch": branch,
        "detached": branch is None,
        "origin": origin,
    }


def untracked(repo: pathlib.Path) -> list[pathlib.Path]:
    return [
        pathlib.Path(os.fsdecode(p))
        for p in git(repo, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0")
        if p
    ]


def manifest(repo: pathlib.Path, paths: Iterable[pathlib.Path]) -> list[dict[str, Any]]:
    result = []
    for rel in paths:
        path = repo / rel
        item: dict[str, Any] = {"path": rel.as_posix()}
        try:
            stat = path.lstat()
            item.update(size=stat.st_size, mode=stat.st_mode & 0o7777, symlink=path.is_symlink())
            if path.is_symlink():
                item.update(
                    target=os.readlink(path), sha256=sha_bytes(os.fsencode(os.readlink(path)))
                )
            elif path.is_file():
                item["sha256"] = sha_file(path)
            else:
                item["unsupported_type"] = True
        except OSError as exc:
            item["error"] = str(exc)
        result.append(item)
    return result


def archive_untracked(
    repo: pathlib.Path, paths: list[pathlib.Path], target: pathlib.Path
) -> dict[str, Any]:
    if FILES is None:
        raise StoragePaused("capture_writer_not_initialized")
    if sum((repo / rel).lstat().st_size for rel in paths) > FILES.record_limit:
        raise StoragePaused("oversized_untracked_capture")

    class Buffer(io.BytesIO):
        def write(self, data):
            if self.tell() + len(data) > FILES.record_limit:
                raise StoragePaused("oversized_untracked_capture")
            return super().write(data)

    with Buffer() as buffer:
        with tarfile.open(
            fileobj=buffer, mode="w:gz", dereference=False, format=tarfile.PAX_FORMAT
        ) as tar:
            for rel in paths:
                path = repo / rel
                if path.exists() or path.is_symlink():
                    tar.add(path, arcname=rel.as_posix(), recursive=False)
        result = write(target, buffer.getvalue())
    return {**result, "files": len(paths)}


def capture(
    repo: pathlib.Path, target: pathlib.Path, phase: str, include_content: bool
) -> tuple[dict[str, Any], bool]:
    result: dict[str, Any] = {}
    complete = True
    commands = {
        "status": ("status", "--porcelain=v2", "-z", "--branch"),
        "unstaged_patch": ("diff", "--binary", "--full-index", "--no-ext-diff"),
        "staged_patch": ("diff", "--cached", "--binary", "--full-index", "--no-ext-diff"),
    }
    for name, args in commands.items():
        try:
            result[name] = write(target / f"{phase}-{name}.bin", git(repo, *args))
        except Exception as exc:  # noqa: BLE001 - optional hook failures cannot stop Codex
            result[name] = {"error": str(exc)}
            complete = False
    try:
        paths = untracked(repo)
        listing = manifest(repo, paths)
        result["untracked_manifest"] = write(
            target / f"{phase}-untracked-manifest.json", jbytes(listing)
        )
        if any("error" in i or i.get("unsupported_type") for i in listing):
            complete = False
        if include_content and paths:
            result["untracked_archive"] = archive_untracked(
                repo, paths, target / f"{phase}-untracked.tar.gz"
            )
    except Exception as exc:  # noqa: BLE001 - optional hook failures cannot stop Codex
        result["untracked"] = {"error": str(exc)}
        complete = False
    return result, complete


def transcript(value: Any) -> tuple[str | None, int | None, str | None]:
    if not value:
        return None, None, None
    path = pathlib.Path(str(value)).expanduser()
    try:
        return str(path), path.stat().st_size, sha_file(path)
    except OSError:
        return str(path), None, None


def trace_info(
    session: str, root: pathlib.Path = TRACE_ROOT
) -> tuple[str | None, int | None, str | None]:
    try:
        matches = [path for path in root.glob(f"trace-*-{session}") if path.is_dir()]
    except OSError:
        return None, None, None
    if not matches:
        return None, None, None
    path = max(matches, key=lambda item: item.stat().st_mtime)
    entries = []
    for item in sorted(path.rglob("*")):
        if item.is_file():
            entries.append(
                {
                    "path": str(item.relative_to(path)),
                    "size": item.stat().st_size,
                    "sha256": sha_file(item),
                }
            )
    return str(path), sum(item["size"] for item in entries), sha_bytes(jbytes(entries))


def trace_turn_info(session: str, turn: str, root: pathlib.Path = TRACE_ROOT) -> dict[str, Any]:
    path_value, total, manifest_digest = trace_info(session, root)
    result = {
        "path": path_value,
        "size": total,
        "manifest_sha256": manifest_digest,
        "end_offset": None,
        "prefix_sha256": None,
        "artifacts": [],
        "complete": False,
        "completeness_scope": "end_marker_and_referenced_files; producer_silent_loss_unknown",
    }
    if not path_value:
        return result
    trace_dir = pathlib.Path(path_value)
    trace_path = trace_dir / "trace.jsonl"
    digest = hashlib.sha256()
    offset = 0
    refs = set()
    intact = True
    previous_seq = 0
    try:
        with trace_path.open("rb") as handle:
            for line in handle:
                digest.update(line)
                offset += len(line)
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    intact = False
                    continue
                if not isinstance(record, dict):
                    intact = False
                    continue
                seq = record.get("seq")
                if type(seq) is not int or seq != previous_seq + 1:
                    intact = False
                if type(seq) is int:
                    previous_seq = seq
                if record.get("codex_turn_id") == turn or contains(record.get("payload"), turn):

                    def collect(value: Any) -> None:
                        if isinstance(value, dict):
                            for key, child in value.items():
                                if (
                                    key == "path"
                                    and isinstance(child, str)
                                    and child.startswith("payloads/")
                                ):
                                    refs.add(child)
                                collect(child)
                        elif isinstance(value, list):
                            for child in value:
                                collect(child)

                    collect(record)
                payload = record.get("payload", {})
                if (
                    record.get("codex_turn_id") == turn
                    and payload.get("type") == "codex_turn_ended"
                ):
                    result.update(
                        end_offset=offset, prefix_sha256=digest.hexdigest(), complete=intact
                    )
                    break
        if result["end_offset"] is None:
            result.update(end_offset=offset, prefix_sha256=digest.hexdigest())
        for relative in sorted(refs):
            item = trace_dir / relative
            if item.resolve() == item and trace_dir in item.parents and item.is_file():
                result["artifacts"].append(
                    {
                        "path": str(item),
                        "relative_path": relative,
                        "size": item.stat().st_size,
                        "sha256": sha_file(item),
                    }
                )
            else:
                result["complete"] = False
    except OSError:
        result["complete"] = False
    return result


def start(conn: sqlite3.Connection, event: dict[str, Any], root: pathlib.Path) -> None:
    session, turn = str(event["session_id"]), str(event["turn_id"])
    row = conn.execute(
        "SELECT start_complete FROM turns WHERE session_id=? AND turn_id=?", (session, turn)
    ).fetchone()
    if row and row[0]:
        return
    stamp = now()
    cwd = pathlib.Path(str(event.get("cwd") or os.getcwd())).expanduser().resolve()
    target = turn_dir(root, session, turn)
    errors = []
    info = None
    arts = {}
    complete = True
    try:
        info = metadata(cwd)
        if info:
            arts, complete = capture(info["root"], target, "start", True)
    except Exception as exc:  # noqa: BLE001 - optional hook failures cannot stop Codex
        errors.append(str(exc))
        complete = False
    try:
        ids = identities(cwd, info["root"] if info else None)
        arts["identities"] = write(target / "identities.json", jbytes(ids))
    except Exception as exc:  # noqa: BLE001 - optional hook failures cannot stop Codex
        ids = {"error": str(exc)}
        errors.append(str(exc))
        complete = False
    rollout, size, _ = transcript(event.get("transcript_path"))
    values = (
        session,
        turn,
        stamp,
        event.get("hook_event_name"),
        str(cwd),
        str(info["root"]) if info else None,
        info.get("origin") if info else None,
        info.get("head") if info else None,
        info.get("branch") if info else None,
        int(info["detached"]) if info else None,
        event.get("model"),
        event.get("permission_mode"),
        rollout,
        size,
        json.dumps(arts, sort_keys=True),
        json.dumps(ids, sort_keys=True),
        int(complete),
        int(bool(rollout and size is not None)),
        "; ".join(errors) or None,
        stamp,
        stamp,
    )
    conn.execute(
        """INSERT INTO turns(session_id,turn_id,start_time,start_hook,cwd,repo_root,
      repo_origin,start_head,start_branch,
      start_detached, model, permission_mode, transcript_path, transcript_start_size,
      start_artifacts_json, identities_json,
      start_complete, native_complete, last_error, created_at, updated_at) VALUES(?, ?, ?, ?,
      ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(session_id, turn_id) DO UPDATE SET start_time=COALESCE(turns.start_time,
      excluded.start_time),
      start_hook=excluded.start_hook,
      cwd=excluded.cwd, repo_root=excluded.repo_root, repo_origin=excluded.repo_origin,
      start_head=excluded.start_head,
      start_branch=excluded.start_branch, start_detached=excluded.start_detached,
      model=excluded.model,
      permission_mode=excluded.permission_mode,
      transcript_path=COALESCE(excluded.transcript_path, turns.transcript_path),
      transcript_start_size=COALESCE(turns.transcript_start_size,excluded.transcript_start_size),
      start_artifacts_json=excluded.start_artifacts_json,identities_json=excluded.identities_json,
      start_complete=excluded.start_complete, native_complete=excluded.native_complete,
      last_error=excluded.last_error,
      updated_at=excluded.updated_at""",
        values,
    )
    conn.commit()


def end(conn: sqlite3.Connection, event: dict[str, Any], root: pathlib.Path) -> None:
    session, turn = str(event["session_id"]), str(event["turn_id"])
    row = conn.execute(
        "SELECT * FROM turns WHERE session_id=? AND turn_id=?", (session, turn)
    ).fetchone()
    if not row:
        start(conn, {**event, "hook_event_name": "StopFallback"}, root)
        row = conn.execute(
            "SELECT * FROM turns WHERE session_id=? AND turn_id=?", (session, turn)
        ).fetchone()
    stamp = now()
    cwd = pathlib.Path(str(event.get("cwd") or row["cwd"] or os.getcwd())).resolve()
    arts = {}
    complete = True
    errors = []
    info = None
    try:
        info = metadata(cwd)
        if info:
            arts, complete = capture(info["root"], turn_dir(root, session, turn), "end", False)
    except Exception as exc:  # noqa: BLE001 - optional hook failures cannot stop Codex
        errors.append(str(exc))
        complete = False
    rollout, size, digest = transcript(event.get("transcript_path") or row["transcript_path"])
    trace = trace_turn_info(session, turn)
    conn.execute(
        """UPDATE turns SET end_time=?,end_hook=?,end_head=?,end_branch=?,model=COALESCE(?,model),
      transcript_path=COALESCE(?, transcript_path), transcript_end_size=?, transcript_sha256=?,
      end_artifacts_json=?,
      full_trace_path=?, full_trace_size=?, full_trace_manifest_sha256=?,
      full_trace_end_offset=?, full_trace_prefix_sha256=?,
      full_trace_artifacts_json=?,full_trace_complete=?,end_complete=?,native_complete=?,
      last_error=?,updated_at=? WHERE session_id=? AND turn_id=?""",
        (
            stamp,
            event.get("hook_event_name"),
            info.get("head") if info else None,
            info.get("branch") if info else None,
            event.get("model"),
            rollout,
            size,
            digest,
            json.dumps(arts, sort_keys=True),
            trace["path"],
            trace["size"],
            trace["manifest_sha256"],
            trace["end_offset"],
            trace["prefix_sha256"],
            json.dumps(trace["artifacts"], sort_keys=True),
            int(trace["complete"]),
            int(complete),
            int(bool(rollout and size is not None and digest)),
            "; ".join(errors) or row["last_error"],
            stamp,
            session,
            turn,
        ),
    )
    conn.commit()


def hook(db: pathlib.Path, root: pathlib.Path, management_home=None) -> int:
    try:
        event = json.load(sys.stdin)
        name = event.get("hook_event_name")
        if not event.get("session_id") or not event.get("turn_id"):
            raise ValueError("missing session_id or turn_id")
        with contextlib.closing(connect(db)) as conn:
            if name in {"UserPromptSubmit", "PreToolUse"}:
                start(conn, event, root)
            elif name == "Stop":
                end(conn, event, root)
        print("{}")
    except Exception as exc:  # noqa: BLE001 - optional hook failures cannot stop Codex
        reason = gap_reason(exc)
        with contextlib.suppress(Exception):
            management_status(
                management_home,
                policy=POLICY,
                gap=True,
                reason="hook_failure" if reason == "unknown" else reason,
            )
        print(f"provenance hook failed: {exc}", file=sys.stderr)
        print("{}")
    return 0


def trace_ids(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"traceId", "trace_id"} and isinstance(child, str) and child:
                yield child
            yield from trace_ids(child)
    elif isinstance(value, list):
        for child in value:
            yield from trace_ids(child)


def contains(value: Any, needle: str) -> bool:
    if isinstance(value, str):
        return value == needle
    if isinstance(value, dict):
        return any(contains(child, needle) for child in value.values())
    if isinstance(value, list):
        return any(contains(child, needle) for child in value)
    return False


def matching_trace_ids(value: Any, turn: str) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        own = value.get("traceId") or value.get("trace_id")
        if isinstance(own, str) and own and contains(value, turn):
            found.add(own)
        for child in value.values():
            found.update(matching_trace_ids(child, turn))
    elif isinstance(value, list):
        for child in value:
            found.update(matching_trace_ids(child, turn))
    return found


def correlate_many(
    archive: pathlib.Path, turns: dict[str, str | None], limit: int = 256 * 1024**2
) -> dict[str, list[dict[str, Any]]]:
    found = {turn: [] for turn in turns}
    if not archive.exists() or not turns:
        return found
    times = [value.timestamp() for value in (parsed(value) for value in turns.values()) if value]
    cutoff = (min(times) - 3600) if times else 0
    paths = []
    for path in archive.rglob("*.json"):
        try:
            if (
                path.is_file()
                and path.stat().st_mtime >= cutoff
                and "collector-logs" not in path.parts
            ):
                paths.append(path)
        except OSError:
            pass
    scanned = 0
    needles = {turn: turn.encode() for turn in turns}
    for path in sorted(paths, key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            size = path.stat().st_size
            if scanned + size > limit:
                break
            scanned += size
            with path.open("rb") as handle:
                for number, line in enumerate(handle, 1):
                    matches = [turn for turn, needle in needles.items() if needle in line]
                    if not matches:
                        continue
                    try:
                        payload = json.loads(line)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        payload = None
                    for turn in matches:
                        ids = (
                            sorted(matching_trace_ids(payload, turn))
                            if payload is not None
                            else []
                        )
                        found[turn].append({"path": str(path), "line": number, "trace_ids": ids})
        except OSError:
            pass
    return found


def correlate(
    archive: pathlib.Path, turn: str, start_time: str | None, limit: int = 256 * 1024**2
) -> tuple[list[dict[str, Any]], bool]:
    pointers = correlate_many(archive, {turn: start_time}, limit)[turn]
    return pointers, bool(pointers)


def reconcile(
    conn: sqlite3.Connection, archive: pathlib.Path, _root: pathlib.Path, limit: int
) -> dict[str, int]:
    rows = conn.execute(
        """SELECT * FROM turns WHERE pruned_at IS NULL AND (end_time IS NULL OR
        otel_complete=0 OR full_trace_complete=0) ORDER BY start_time DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    linked = finalized = 0
    unresolved = {row["turn_id"]: row["start_time"] for row in rows if not row["otel_complete"]}
    correlations = correlate_many(archive, unresolved)
    for row in rows:
        trace = trace_turn_info(row["session_id"], row["turn_id"])
        if trace["path"]:
            conn.execute(
                """UPDATE turns SET full_trace_path=?,full_trace_size=?,
              full_trace_manifest_sha256=?,
              full_trace_end_offset=?, full_trace_prefix_sha256=?, full_trace_artifacts_json=?,
              full_trace_complete=?, updated_at=?
              WHERE session_id=? AND turn_id=?""",
                (
                    trace["path"],
                    trace["size"],
                    trace["manifest_sha256"],
                    trace["end_offset"],
                    trace["prefix_sha256"],
                    json.dumps(trace["artifacts"], sort_keys=True),
                    int(trace["complete"]),
                    now(),
                    row["session_id"],
                    row["turn_id"],
                ),
            )
        if not row["otel_complete"]:
            pointers = correlations.get(row["turn_id"], [])
            complete = bool(pointers)
            if pointers:
                conn.execute(
                    """UPDATE turns SET otel_pointers_json=?, otel_complete=?,
                    updated_at=? WHERE session_id=? AND turn_id=?""",
                    (
                        json.dumps(pointers, sort_keys=True),
                        int(complete),
                        now(),
                        row["session_id"],
                        row["turn_id"],
                    ),
                )
                linked += 1
    conn.commit()
    return {"examined": len(rows), "correlated": linked, "finalized": finalized}


def size(path: pathlib.Path) -> int:
    total = 0
    if path.exists():
        for item in path.rglob("*"):
            try:
                if item.is_file() and not item.is_symlink():
                    total += item.stat().st_size
            except OSError:
                pass
    return total


def cleanup(
    _conn: sqlite3.Connection, root: pathlib.Path, _days: int, _max_bytes: int
) -> dict[str, Any]:
    # Native writers do not share this lock; absence of a hook is not inactivity.
    return {
        "pruned": 0,
        "reclaimed_bytes": 0,
        "remaining_bytes": size(root) + size(root.parent / "rollout-traces"),
        "paused": "native_activity_and_rollback_references_unverified",
    }


def freeze(conn: sqlite3.Connection, session: str, turn: str, value: bool) -> None:
    stamp = now()
    result = conn.execute(
        "UPDATE turns SET frozen=?,frozen_at=?,updated_at=? WHERE session_id=? AND turn_id=?",
        (int(value), stamp if value else None, stamp, session, turn),
    )
    if result.rowcount != 1:
        raise SystemExit(f"turn not found: {session}/{turn}")
    conn.commit()


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--management-home", type=pathlib.Path)
    p.add_argument("--db", type=pathlib.Path, default=DB)
    p.add_argument("--artifacts", type=pathlib.Path, default=ARTIFACTS)
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("hook")
    r = sub.add_parser("reconcile")
    r.add_argument("--archive", type=pathlib.Path, default=OTEL)
    r.add_argument("--limit", type=int, default=20)
    c = sub.add_parser("cleanup")
    c.add_argument("--days", type=int, default=RETENTION_DAYS)
    c.add_argument("--max-bytes", type=int, default=RETENTION_BYTES)
    m = sub.add_parser("maintain")
    m.add_argument("--archive", type=pathlib.Path, default=OTEL)
    m.add_argument("--limit", type=int, default=20)
    m.add_argument("--days", type=int, default=RETENTION_DAYS)
    m.add_argument("--max-bytes", type=int, default=RETENTION_BYTES)
    for name in ("freeze", "unfreeze"):
        q = sub.add_parser(name)
        q.add_argument("session_id")
        q.add_argument("turn_id")
    s = sub.add_parser("show")
    s.add_argument("session_id", nargs="?")
    s.add_argument("turn_id", nargs="?")
    s.add_argument("--limit", type=int, default=20)
    sub.add_parser("doctor")
    return p


def run(args) -> int:
    if args.command == "hook":
        return hook(args.db, args.artifacts, args.management_home)
    with contextlib.closing(connect(args.db)) as conn:
        if args.command == "reconcile":
            print(
                json.dumps(
                    reconcile(conn, args.archive, args.artifacts, args.limit), sort_keys=True
                )
            )
        elif args.command == "cleanup":
            print(
                json.dumps(
                    cleanup(conn, args.artifacts, args.days, args.max_bytes), sort_keys=True
                )
            )
        elif args.command == "maintain":
            print(
                json.dumps(
                    {
                        "reconcile": (
                            {"paused": "managed_target_optional_pause", "capture_gap": True}
                            if args.management["optional_telemetry_pause_requested"]
                            else reconcile(conn, args.archive, args.artifacts, args.limit)
                        ),
                        "management": args.management,
                        "cleanup": cleanup(conn, args.artifacts, args.days, args.max_bytes),
                        "metadata": cleanup_manifest(conn, POLICY)[0],
                    },
                    sort_keys=True,
                )
            )
        elif args.command in {"freeze", "unfreeze"}:
            freeze(conn, args.session_id, args.turn_id, args.command == "freeze")
        elif args.command == "show":
            if args.session_id and args.turn_id:
                rows = conn.execute(
                    "SELECT * FROM turns WHERE session_id=? AND turn_id=?",
                    (args.session_id, args.turn_id),
                ).fetchall()
            elif args.session_id:
                rows = conn.execute(
                    "SELECT * FROM turns WHERE session_id=? ORDER BY start_time DESC LIMIT ?",
                    (args.session_id, args.limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM turns ORDER BY start_time DESC LIMIT ?", (args.limit,)
                ).fetchall()
            print(json.dumps([dict(row) for row in rows], indent=2, sort_keys=True))
        elif args.command == "doctor":
            print(
                json.dumps(
                    {
                        "schema_version": conn.execute(
                            "SELECT value FROM meta WHERE key='schema_version'"
                        ).fetchone()[0],
                        "db": str(args.db),
                        "artifacts": str(args.artifacts),
                        "turns": conn.execute("SELECT count(*) FROM turns").fetchone()[0],
                    },
                    sort_keys=True,
                )
            )
    return 0


def main(argv: list[str] | None = None) -> int:
    global FILES
    args = parser().parse_args(argv)
    try:
        args.management = management_status(
            args.management_home, policy=POLICY, refresh=args.command == "maintain"
        )
        if (
            args.command in {"hook", "reconcile"}
            and args.management["optional_telemetry_pause_requested"]
        ):
            gap = management_status(
                args.management_home,
                policy=POLICY,
                gap=True,
                reason="reconcile_skipped" if args.command == "reconcile" else "managed_pause",
            )
            print("{}" if args.command == "hook" else json.dumps(gap))
            return 0
        args.artifacts.mkdir(parents=True, exist_ok=True, mode=0o700)
        FILES = BoundedFiles(args.artifacts, POLICY["allocations"]["provenance_artifacts"])
        with exclusive(args.db.parent / ".provenance-writer.lock"):
            if args.command == "maintain":
                output = io.StringIO()
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                    result = run(args)
                BoundedLog(
                    args.db.parent / "logs/managed-reconciler.log",
                    allocation=POLICY["allocations"]["telemetry_logs"] // 4,
                ).write(output.getvalue())
                return result
            return run(args)
    except (StoragePaused, sqlite3.Error, OSError) as exc:
        if args.command == "hook":
            management_status(
                args.management_home, policy=POLICY, gap=True, reason=gap_reason(exc)
            )
        report = json.dumps({"optional_telemetry_paused": type(exc).__name__}) + "\n"
        if args.command == "maintain":
            with contextlib.suppress(StoragePaused, OSError):
                BoundedLog(
                    args.db.parent / "logs/managed-reconciler.log",
                    allocation=POLICY["allocations"]["telemetry_logs"] // 4,
                ).write(report)
        else:
            print("{}" if args.command == "hook" else report)
        return 0 if args.command == "hook" else 2


if __name__ == "__main__":
    raise SystemExit(main())
