"""Offline SelfBench provenance export; no grading, network, or runtime dependency."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from omnigent.benchmark_capture import BenchmarkCaptureError, _sha256, _write_json


def export_candidate(capture_dir: Path, destination: Path) -> dict[str, Any]:
    manifest = json.loads((capture_dir / "manifest.json").read_text())
    repo = Path(manifest["repo_root"]).resolve()
    if destination.resolve().is_relative_to(repo):
        raise BenchmarkCaptureError("export destination must be outside the repository")
    if manifest["completed_at"] is None or not manifest["start"] or not manifest["end"]:
        raise BenchmarkCaptureError("capture requires terminal state and both Git snapshots")
    for stage in ("start", "end"):
        for artifact in manifest[stage]["artifacts"].values():
            path = capture_dir / stage / artifact["path"]
            if not path.resolve().is_relative_to(capture_dir.resolve()):
                raise BenchmarkCaptureError("artifact path escapes capture directory")
            if _sha256(path) != artifact["sha256"]:
                raise BenchmarkCaptureError("capture artifact digest mismatch")
    task_input = json.loads((capture_dir / "input.json").read_text())
    messages = [message for message in task_input["messages"] if message.get("role") == "user"]
    if not messages:
        raise BenchmarkCaptureError("capture has no user instruction")
    content = messages[-1].get("content")
    if not isinstance(content, str) or not content.strip():
        raise BenchmarkCaptureError("text instruction required for SelfBench provenance export")
    provenance = {
        "sourceType": "codex",
        "sessionId": manifest["capture_id"],
        "messageIndex": 0,
        "content": content,
    }
    result = {
        "schema_version": 1,
        "capture_id": manifest["capture_id"],
        "capture_manifest": str((capture_dir / "manifest.json").resolve()),
        "repository": manifest["start"]["origin"],
        "base_revision": manifest["start"]["head"],
        "final_revision": manifest["end"]["head"],
        "start_snapshot": str((capture_dir / "start").resolve()),
        "end_snapshot": str((capture_dir / "end").resolve()),
        "native_rollout": manifest["native_rollout"],
        "dirty_start": bool((capture_dir / "start" / "status.z").read_bytes()),
        "dirty_end": bool((capture_dir / "end" / "status.z").read_bytes()),
        "terminal_state": manifest["terminal_state"],
        "selfbench_provenance": "provenance.jsonl",
        "construction_status": "not_constructed",
        "requires_adapter": (
            "Reconstruct dirty states in an isolated repository and pin reachable "
            "base/reference commits before SelfBench discovery."
        ),
    }
    destination.mkdir(parents=True, exist_ok=False, mode=0o700)
    (destination / "provenance.jsonl").write_text(json.dumps(provenance) + "\n")
    _write_json(destination / "candidate.json", result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    export_candidate(args.capture, args.destination)


if __name__ == "__main__":
    main()
