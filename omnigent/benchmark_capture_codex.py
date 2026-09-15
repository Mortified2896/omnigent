"""Preserve Codex's own rollout, with explicit thread and turn evidence."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from omnigent.benchmark_capture import BenchmarkCaptureError, _artifact

# A per-artifact safety bound, not a storage quota or retention policy.
_MAX_ROLLOUT_BYTES = 128 * 1024 * 1024


def preserve_rollout(
    *,
    home: Path | None,
    explicit_path: str | None,
    thread_id: str | None,
    turn_id: str | None,
    destination: Path,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "missing",
        "source_path": None,
        "artifact": None,
        "thread_id": thread_id,
        "turn_id": turn_id,
        "turn_start_line": None,
        "turn_end_line": None,
        "model": None,
        "reasoning_effort": None,
        "codex_version": None,
        "boundary_status": "unknown",
    }
    if not home or not thread_id or not re.fullmatch(r"[a-zA-Z0-9_-]+", thread_id):
        return result
    candidates = []
    if explicit_path:
        candidates = [Path(explicit_path)]
    else:
        for subdir in ("sessions", "archived_sessions"):
            candidates.extend((home / subdir).glob(f"**/rollout-*-{thread_id}.jsonl"))
    candidates = [
        p for p in candidates if p.is_file() and p.resolve().is_relative_to(home.resolve())
    ]
    if len(candidates) != 1:
        result["status"] = "ambiguous" if candidates else "missing"
        return result
    source = candidates[0]
    result["source_path"] = str(source)
    size = source.stat().st_size
    result["source_size"] = size
    if size > _MAX_ROLLOUT_BYTES:
        result["status"] = "over_limit"
        return result
    # Read exactly the size at finalization; never follow a growing file forever.
    with source.open("rb") as handle:
        content = handle.read(size)
    lines = content.splitlines(keepends=True)
    if not lines:
        return result
    meta = json.loads(lines[0])
    if meta.get("type") != "session_meta" or meta.get("payload", {}).get("id") != thread_id:
        raise BenchmarkCaptureError("native rollout thread identity mismatch")
    result["codex_version"] = meta["payload"].get("cli_version")
    for number, raw in enumerate(lines, 1):
        if not raw.endswith(b"\n"):
            result["status"] = "partial"
            continue
        try:
            record = json.loads(raw)
        except ValueError:
            result["status"] = "partial"
            continue
        payload = record.get("payload", {})
        if not isinstance(payload, dict) or not turn_id or payload.get("turn_id") != turn_id:
            continue
        if record.get("type") == "turn_context":
            result["turn_start_line"] = result["turn_start_line"] or number
            result["model"] = payload.get("model")
            result["reasoning_effort"] = payload.get("effort", payload.get("reasoning_effort"))
        if record.get("type") == "event_msg":
            if payload.get("type") == "task_started":
                result["turn_start_line"] = number
            elif payload.get("type") in {"task_complete", "task_failed", "turn_aborted"}:
                result["turn_end_line"] = number
    result["boundary_status"] = (
        "explicit" if result["turn_start_line"] and result["turn_end_line"] else "incomplete"
    )
    destination.mkdir(mode=0o700)
    # Harbor validates the native filename when loading a trajectory.
    target = destination / source.name
    target.write_bytes(content)
    result["artifact"] = _artifact(target)
    result["status"] = "preserved" if result["status"] != "partial" else "partial"
    return result
