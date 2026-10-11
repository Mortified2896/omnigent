"""Create a private, deterministic recent-chat sample for benchmark fit inspection.

Samples before reading outcomes: equal slots for the four main project families
and ZCode. This is a coverage probe, not an estimate of the user's task mixture.
Input contains original user requests and transcript digests, never stored human
reviews, model metadata, assistant answers or proposed outcome labels.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from retrospective_review import codex_index, codex_trace, zcode_index, zcode_trace


def family(row: dict) -> str | None:
    if row["source"] == "zcode":
        return "ZCode"
    cwd = row["cwd"].lower().replace(" ", "-")
    for needle, name in (
        ("blocknations", "BlockNations"),
        ("finance-r-simulations", "Article Lab"),
        ("homelab", "HomeLab"),
        ("omnigent", "Omnigent"),
    ):
        if needle in cwd:
            return name
    return None


def request_text(text: str) -> str | None:
    if text.lstrip().startswith(("<recommended_plugins>", "<skills_instructions>")):
        return None
    # Ambient browser state precedes the actual request in some stored messages.
    # Keep annotations/attachment references, which carry user-supplied context.
    if text.lstrip().startswith("<in-app-browser-context") and "## My request:" in text:
        return text.partition("## My request:")[2].strip() or None
    return text


def sample(codex: Path, zcode: Path, per_project: int, excluded: set[str]) -> dict:
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in [*codex_index(codex), *zcode_index(zcode)]:
        group = family(row)
        if group and row["id"] not in excluded:
            groups[group].append(row)
    selected = []
    seen = set()
    for group in sorted(groups):
        count = 0
        for row in sorted(groups[group], key=lambda item: item["updated_at"], reverse=True):
            trace = (
                zcode_trace(zcode, row["id"])
                if row["source"] == "zcode"
                else codex_trace(Path(row["rollout_path"]))
            )
            requests = []
            for index, item in enumerate(trace):
                if item["kind"] == "user" and (text := request_text(item["text"])):
                    requests.append({"index": index, "text": text})
            if not requests:
                continue
            first_hash = hashlib.sha256(requests[0]["text"].strip().encode()).hexdigest()
            if first_hash in seen:
                continue
            seen.add(first_hash)
            selected.append(
                {
                    "case_id": f"C{len(selected) + 1:02d}",
                    "project": group,
                    "source_id": row["source"] + ":" + row["id"],
                    "chat_updated_at": row["updated_at"],
                    "request_count": len(requests),
                    "requests": requests,
                    "requests_sha256": hashlib.sha256(
                        json.dumps(requests, sort_keys=True).encode()
                    ).hexdigest(),
                }
            )
            count += 1
            if count == per_project:
                break
    return {
        "schema_version": 1,
        "observed_at_utc": datetime.now(timezone.utc).isoformat(),
        "method": (
            "Most recently updated root chats; project-balanced; "
            "exact first-request deduplication; before outcome inspection"
        ),
        "per_project": per_project,
        "excluded_ids": sorted(excluded),
        "eligible_root_chats_by_project": {group: len(rows) for group, rows in groups.items()},
        "case_count": len(selected),
        "cases": selected,
        "limits": [
            "Root chats may contain multiple tasks, forks and repeated work.",
            "Exact first-request deduplication is not semantic or lineage deduplication.",
            "No outcome labels are read; coverage cannot estimate benchmark/outcome correlation.",
            "Equal project slots are not population weights or a success-rate denominator.",
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--codex", type=Path, default=Path.home() / ".codex/state_5.sqlite")
    parser.add_argument("--zcode", type=Path, default=Path.home() / ".zcode/cli/db/db.sqlite")
    parser.add_argument("--per-project", type=int, default=6)
    parser.add_argument("--exclude", action="append", default=[])
    args = parser.parse_args()
    if args.per_project < 1:
        parser.error("--per-project must be positive")
    result = sample(
        args.codex.resolve(), args.zcode.resolve(), args.per_project, set(args.exclude)
    )
    args.output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with args.output.open("x") as handle:
        os.chmod(args.output, 0o600)
        json.dump(result, handle, indent=2)
    print(
        json.dumps(
            {
                "output": str(args.output),
                "case_count": result["case_count"],
                "eligible_root_chats_by_project": result["eligible_root_chats_by_project"],
            }
        )
    )
