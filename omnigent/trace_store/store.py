"""Small SQLite analysis store, independent of native chat/history databases."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, path TEXT NOT NULL,
 last_scan REAL, last_event REAL, status TEXT NOT NULL, error TEXT,
 checkpoint TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS sessions (
 source TEXT NOT NULL, id TEXT NOT NULL, started REAL, updated REAL,
 harness TEXT, model TEXT, effort TEXT, status TEXT, is_test INTEGER,
 parent_id TEXT, external_id TEXT, metadata TEXT NOT NULL,
 PRIMARY KEY(source,id)
);
CREATE TABLE IF NOT EXISTS events (
 source TEXT NOT NULL, id TEXT NOT NULL, session_id TEXT, turn_id TEXT,
 timestamp REAL, kind TEXT NOT NULL, model TEXT, effort TEXT, status TEXT,
 input_tokens INTEGER, output_tokens INTEGER, cached_tokens INTEGER,
 duration_ms REAL, estimated_cost_usd REAL, cost_basis TEXT,
 metadata TEXT NOT NULL, PRIMARY KEY(source,id)
);
CREATE INDEX IF NOT EXISTS events_session ON events(source,session_id,turn_id);
CREATE INDEX IF NOT EXISTS events_kind ON events(kind,timestamp);
CREATE TABLE IF NOT EXISTS details (
 source TEXT NOT NULL, event_id TEXT NOT NULL, expires REAL NOT NULL,
 text TEXT NOT NULL, PRIMARY KEY(source,event_id)
);
CREATE INDEX IF NOT EXISTS details_expiry ON details(expires);
CREATE TABLE IF NOT EXISTS human_outcomes (
 source TEXT NOT NULL, session_id TEXT NOT NULL, turn_id TEXT NOT NULL,
 actor TEXT NOT NULL, outcome TEXT NOT NULL,
 PRIMARY KEY(source,session_id,turn_id,actor)
);
CREATE TABLE IF NOT EXISTS session_links (
 source TEXT NOT NULL, session_id TEXT NOT NULL, target_source TEXT NOT NULL,
 target_id TEXT NOT NULL, basis TEXT NOT NULL,
 PRIMARY KEY(source,session_id,target_source,target_id)
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT OR IGNORE INTO meta VALUES ('schema_version','1');
CREATE VIEW IF NOT EXISTS source_health AS
 SELECT s.*, (SELECT count(*) FROM sessions x WHERE x.source=s.id) sessions,
 (SELECT count(*) FROM events x WHERE x.source=s.id) events FROM sources s;
CREATE VIEW IF NOT EXISTS scored_turns AS
 SELECT o.*, s.model AS session_model, s.effort AS session_effort,
 s.harness, s.is_test FROM human_outcomes o JOIN sessions s
 ON s.source=o.source AND s.id=o.session_id WHERE s.is_test=0;
"""

# Defense in depth for short task/answer snippets. Tool arguments, command output,
# headers, environment, reasoning and arbitrary metadata are never copied.
_SECRETS = re.compile(
    r"(?i)(?:bearer\s+\S+|(?:sk|rk|ghp|github_pat)[_-][A-Za-z0-9_-]{16,}|"
    r"(?:password|api[_-]?key|access[_-]?token|secret)\s*[:=]\s*[^\s,;]+)"
)
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----", re.S
)


def scrub(text: str) -> str:
    return _SECRETS.sub("[redacted]", _PRIVATE_KEY.sub("[redacted private key]", text))[:16000]


def timestamp(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) or str(value).isdigit():
        number = float(value)
        while number > 100000000000:
            number /= 1000
        return number
    from datetime import datetime

    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


@contextmanager
def source_db(path: Path):
    """Consistent read transaction; never open a missing source in write mode."""

    def connect(candidate):
        conn = sqlite3.connect(
            f"file:{quote(str(candidate.resolve()))}?mode=ro", uri=True, timeout=15
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        try:
            conn.execute("SELECT count(*) FROM sqlite_schema").fetchone()
        except sqlite3.Error:
            conn.close()
            raise
        return conn

    temporary = None
    try:
        conn = connect(path)
    except sqlite3.OperationalError as exc:
        # An inactive WAL-mode DB can need a new -shm even for a read-only
        # connection. Never create it in a native history directory. Only a
        # quiescent, checkpointed DB qualifies for this temporary-copy fallback;
        # active/uncheckpointed WALs must remain readable through SQLite itself.
        if exc.sqlite_errorcode & 0xFF not in (sqlite3.SQLITE_READONLY, sqlite3.SQLITE_CANTOPEN):
            raise
        if not path.is_file():
            raise
        sidecars = [Path(str(path) + suffix) for suffix in ("-wal", "-journal")]

        def identity():
            stat = path.stat()
            if any(p.exists() and p.stat().st_size for p in sidecars):
                raise sqlite3.OperationalError("Source has a live WAL or recovery journal")
            return stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns

        before = identity()
        temporary = tempfile.TemporaryDirectory(prefix="omnigent-trace-read-")
        try:
            copy = Path(temporary.name) / path.name
            shutil.copyfile(path, copy)
            if identity() != before:
                raise sqlite3.OperationalError("Source changed during read-only snapshot")
            conn = connect(copy)
        except Exception:
            temporary.cleanup()
            raise
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()
        if temporary:
            temporary.cleanup()


class Store:
    def __init__(self, path: Path):
        self.rollout_context: dict[str, Any] = {}
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path, timeout=30)
        os.chmod(path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.db.commit()

    def upsert(self, table: str, **fields: Any) -> None:
        # Table/field names are exclusively fixed adapter code, never input data.
        fields = {
            k: json.dumps(v, sort_keys=True) if isinstance(v, (dict, list)) else v
            for k, v in fields.items()
        }
        columns = ",".join(fields)
        marks = ",".join("?" for _ in fields)
        updates = ",".join(f"{k}=excluded.{k}" for k in fields)
        self.db.execute(
            f"INSERT INTO {table} ({columns}) VALUES ({marks}) "
            f"ON CONFLICT DO UPDATE SET {updates}",
            list(fields.values()),
        )

    def event(self, source: str, id: str, kind: str, *, detail: str | None = None, **fields: Any):
        metadata = fields.pop("metadata", {})
        self.upsert("events", source=source, id=id, kind=kind, metadata=metadata, **fields)
        when = fields.get("timestamp")
        if detail and when:
            days = 90 if fields.get("status") in ("failed", "error") else 30
            expires = when + days * 86400
            if expires > time.time():
                self.upsert(
                    "details", source=source, event_id=id, expires=expires, text=scrub(detail)
                )

    def checkpoint(self, source: str) -> dict:
        row = self.db.execute("SELECT checkpoint FROM sources WHERE id=?", [source]).fetchone()
        return json.loads(row[0]) if row else {}

    def finish(self, source: str, kind: str, path: Path, checkpoint: dict):
        latest = self.db.execute(
            "SELECT max(timestamp) FROM events WHERE source=?", [source]
        ).fetchone()[0]
        self.upsert(
            "sources",
            id=source,
            kind=kind,
            path=str(path),
            last_scan=time.time(),
            last_event=latest,
            status="partial" if checkpoint.get("malformed_lines") else "ok",
            error="malformed_jsonl" if checkpoint.get("malformed_lines") else None,
            checkpoint=checkpoint,
        )

    def expire_details(self):
        self.db.execute("DELETE FROM details WHERE expires < ?", [time.time()])

    def snapshot(self, destination: Path):
        self.db.commit()
        with sqlite3.connect(destination) as target:
            self.db.backup(target)
            target.execute("PRAGMA journal_mode=DELETE")
            if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("analysis snapshot integrity check failed")
        os.chmod(destination, 0o600)
