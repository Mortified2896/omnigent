"""Reproducible metadata inventories for the local Codex and ZCode collections."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


def inventory(codex: Path, zcode: Path) -> dict:
    with sqlite3.connect(codex.as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        all_rows = list(connection.execute("SELECT * FROM threads"))
        roots = [r for r in all_rows if "subagent" not in r["source"]]
        codex_result = {
            "indexed_chats": len(all_rows),
            "root_chats": len(roots),
            "excluded_subagent_guardian_rows": len(all_rows) - len(roots),
            "present_rollouts": sum(Path(r["rollout_path"]).is_file() for r in roots),
            "rollout_bytes": sum(
                Path(r["rollout_path"]).stat().st_size
                for r in roots
                if Path(r["rollout_path"]).is_file()
            ),
            "missing_index_model": sum(not r["model"] for r in roots),
            "missing_index_effort": sum(not r["reasoning_effort"] for r in roots),
            "project_directories": dict(Counter(Path(r["cwd"]).name for r in roots)),
            "index_models": dict(Counter(r["model"] or "unknown" for r in roots)),
            "caveat": "Current/last chat model; not per-request execution attribution.",
        }
    # ZCode currently uses checkpointed databases without WAL. Avoid creating
    # read locks or journal files, and reject any snapshot that changes mid-read.
    if zcode.with_name(zcode.name + "-wal").exists():
        raise ValueError("ZCode WAL exists: use a consistent SQLite backup instead")
    before = zcode.stat()
    with sqlite3.connect(zcode.as_uri() + "?mode=ro&immutable=1", uri=True) as connection:
        zcode_result = {
            "tables": {
                table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in (
                    "session",
                    "message",
                    "part",
                    "model_usage",
                    "turn_usage",
                    "tool_usage",
                )
            },
            "requests_joined_to_assistant_message": connection.execute(
                "SELECT count(*) FROM model_usage u JOIN message m "
                "ON u.assistant_message_id=m.id AND u.session_id=m.session_id"
            ).fetchone()[0],
            "requests_joined_to_parent_user_message": connection.execute(
                "SELECT count(*) FROM model_usage u JOIN message m "
                "ON u.parent_user_message_id=m.id AND u.session_id=m.session_id"
            ).fetchone()[0],
            "missing_model_provider_variant": connection.execute(
                "SELECT count(*) FROM model_usage "
                "WHERE model_id IS NULL OR provider_id IS NULL OR variant IS NULL"
            ).fetchone()[0],
            "missing_latency": connection.execute(
                "SELECT count(*) FROM model_usage WHERE duration_ms IS NULL"
            ).fetchone()[0],
            "missing_input_output_tokens": connection.execute(
                "SELECT count(*) FROM model_usage "
                "WHERE input_tokens IS NULL OR output_tokens IS NULL"
            ).fetchone()[0],
            "turn_status": dict(
                connection.execute("SELECT status,count(*) FROM turn_usage GROUP BY status")
            ),
            "turn_model_retry_counter_sum": connection.execute(
                "SELECT sum(model_retry_count) FROM turn_usage"
            ).fetchone()[0],
            "models": dict(
                connection.execute("SELECT model_id,count(*) FROM model_usage GROUP BY model_id")
            ),
            "caveat": "Runtime completed/error/cancelled are not task-success judgments.",
        }
    after = zcode.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("ZCode changed during inspection; retry")
    return {
        "schema_version": 1,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "sources": {"codex": str(codex), "zcode": str(zcode)},
        "codex": codex_result,
        "zcode": zcode_result,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", type=Path, default=Path.home() / ".codex/state_5.sqlite")
    parser.add_argument("--zcode", type=Path, default=Path.home() / ".zcode/cli/db/db.sqlite")
    args = parser.parse_args()
    print(
        json.dumps(inventory(args.codex.resolve(), args.zcode.resolve()), indent=2, sort_keys=True)
    )
