"""Compact durable probe outcomes; never store prompts, responses or credentials."""

import json
import os
import sqlite3
import time
from pathlib import Path


def root() -> Path:
    return Path(
        os.environ.get(
            "OMNIGENT_MODEL_AVAILABILITY_DIR",
            str(Path.home() / ".omnigent" / "model-availability"),
        )
    )


class ProbeStore:
    def __init__(self, directory: Path | None = None):
        self.directory = directory or root()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / "probes.sqlite"
        with self.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS results (route TEXT, effort TEXT, "
                "data TEXT NOT NULL, PRIMARY KEY(route,effort))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS runs "
                "(id TEXT PRIMARY KEY, started REAL, data TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS history "
                "(run_id TEXT, route TEXT, effort TEXT, checked REAL, data TEXT NOT NULL, "
                "PRIMARY KEY(run_id,route,effort))"
            )
        self.path.chmod(0o600)

    def connect(self):
        return sqlite3.connect(self.path, timeout=5)

    def result(self, data: dict):
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO results VALUES (?,?,?)",
                (data["route"], data["effort"], json.dumps(data)),
            )
            db.execute(
                "INSERT OR REPLACE INTO history VALUES (?,?,?,?,?)",
                (
                    data["run_id"],
                    data["route"],
                    data["effort"],
                    data["checked_at"],
                    json.dumps(data),
                ),
            )
            # Hourly unified ingestion retains compact outcomes for long-term analysis.
            db.execute("DELETE FROM history WHERE checked < ?", (time.time() - 90 * 86400,))

    def results(self) -> dict:
        with self.connect() as db:
            return {
                (r, e): json.loads(d)
                for r, e, d in db.execute("SELECT route,effort,data FROM results")
            }

    def run(self, data: dict):
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO runs VALUES (?,?,?)",
                (data["id"], data["started_at"], json.dumps(data)),
            )
            db.execute(
                "DELETE FROM runs WHERE id NOT IN "
                "(SELECT id FROM runs ORDER BY started DESC LIMIT 20)"
            )

    def latest(self) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT data FROM runs ORDER BY started DESC LIMIT 1").fetchone()
            return json.loads(row[0]) if row else None


def recorded_results() -> dict:
    """Read without creating files at native launch or on installations without probes."""
    path = root() / "probes.sqlite"
    if not path.exists():
        return {}
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        return {
            (r, e): json.loads(d)
            for r, e, d in db.execute("SELECT route,effort,data FROM results")
        }
