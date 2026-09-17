"""Durable single-writer reservations for logical TB4 treatments."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path


class FloorAssignmentLedger:
    def __init__(self, path: Path) -> None:
        self.path = path

    def reserve(self, key: str, fingerprint: str) -> tuple[bool, dict | None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(mode=0o600, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS assignments (identity TEXT PRIMARY KEY, "
                "fingerprint TEXT NOT NULL, result TEXT)"
            )
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT fingerprint, result FROM assignments WHERE identity = ?", (key,)
            ).fetchone()
            if row is not None:
                if row[0] != fingerprint:
                    raise ValueError(
                        "logical experiment already has a different treatment request"
                    )
                return False, json.loads(row[1]) if row[1] is not None else None
            db.execute("INSERT INTO assignments VALUES (?, ?, NULL)", (key, fingerprint))
            return True, None

    def complete(self, key: str, fingerprint: str, result: dict) -> None:
        payload = json.dumps(result, sort_keys=True)
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            cursor = db.execute(
                "UPDATE assignments SET result = ? WHERE identity = ? "
                "AND fingerprint = ? AND result IS NULL",
                (payload, key, fingerprint),
            )
            if cursor.rowcount != 1:
                row = db.execute(
                    "SELECT result FROM assignments WHERE identity = ? AND fingerprint = ?",
                    (key, fingerprint),
                ).fetchone()
                if row is None or row[0] != payload:
                    raise ValueError("TB4 assignment is immutable")
