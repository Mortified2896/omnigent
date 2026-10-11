"""Validate offline review proposals against frozen transcript snapshots.

The private manifest declares each source, its selected checkpoint, an adapter
snapshot and the review journals. This tool verifies identities and evidence
references, counts coverage, and can append proposals to the local review queue.
It does not score tasks, inspect human labels or infer model success rates.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

OUTCOMES = {"success", "partial", "failed", "not_sure"}
CAPABILITIES = {
    "repo_change",
    "terminal_operations",
    "live_deployment",
    "browser_interaction",
    "native_mac_control",
    "research_synthesis",
    "editorial_or_visual_judgment",
    "empirical_ml_evaluation",
    "session_continuity",
}


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def journal(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records = []
    with path.open() as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError as exc:
                raise ValueError(f"Invalid journal row: {path}:{line_number}") from exc
            if not isinstance(record, dict):
                raise ValueError(f"Journal row must be an object: {path}:{line_number}")
            records.append(record)
    return records


def identity(record: dict) -> str:
    value = record["source_id"]
    if not isinstance(value, str):
        raise ValueError("Invalid source identity")
    if ":" not in value:
        value = record["source"] + ":" + value
    if value.partition(":")[0] not in {"codex", "zcode", "omnigent"}:
        raise ValueError("Unknown source identity")
    return value


def load_trace(entry: dict) -> list[dict]:
    path = Path(entry["snapshot"])
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != entry["snapshot_sha256"]:
        raise ValueError("Transcript snapshot changed")
    trace = (
        json.loads(data)
        if entry["format"] == "json"
        else [json.loads(line) for line in data.splitlines() if line.strip()]
    )
    if not isinstance(trace, list):
        raise ValueError("Transcript snapshot must contain an item list")
    # Some snapshot journals carry a display index beside the original item.
    # Only explicitly declared wrapper fields are removed before digest checks.
    return [
        {k: v for k, v in item.items() if k not in entry.get("strip_fields", [])} for item in trace
    ]


def validate(record: dict, entry: dict) -> dict:
    if (
        record.get("review_source") != "assistant_proposal"
        or record.get("human_reviewed") is not False
        or record.get("score_eligible") is not False
        or record.get("outcome") not in OUTCOMES
        or record.get("confidence") not in {"low", "medium", "high"}
    ):
        raise ValueError("Only explicitly unvalidated assistant proposals are accepted")
    if not record.get("reason") or not record.get("scope") or not record.get("coverage"):
        raise ValueError("Missing review scope, reason or inspection coverage")
    capabilities = record.get("required_capabilities", [])
    if not isinstance(capabilities, list) or not set(capabilities) <= CAPABILITIES:
        raise ValueError("Unknown capability annotation")
    checkpoint = record.get("checkpoint")
    if checkpoint is None:
        if (
            entry.get("checkpoint") is not None
            or record.get("checkpoint_digest") is not None
            or record["outcome"] != "not_sure"
        ):
            raise ValueError("Missing checkpoints cannot supply an outcome")
    else:
        if entry.get("excluded_reason"):
            raise ValueError("Excluded source cannot supply a checkpoint proposal")
        if type(checkpoint) is not int or checkpoint != entry.get("checkpoint"):
            raise ValueError("Review does not match the declared checkpoint")
        trace = load_trace(entry)
        if checkpoint < 0 or checkpoint >= len(trace):
            raise ValueError("Checkpoint outside transcript")
        item = trace[checkpoint]
        if item.get("kind") != "assistant" or item.get("channel") == "commentary":
            raise ValueError("Checkpoint is not an assistant answer")
        if record.get("checkpoint_digest") != digest(item):
            raise ValueError("Checkpoint digest does not match frozen evidence")
        if record.get("turn_id") != item.get("turn_id"):
            raise ValueError("Checkpoint turn identity changed")
        evidence = record.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            raise ValueError("Missing evidence references")
        for reference in evidence:
            index = reference.get("index")
            if type(index) is not int or not 0 <= index <= checkpoint:
                raise ValueError("Evidence reference outside inspected checkpoint")
        pending_evidence = record.get("pending_evidence", [])
        if not isinstance(pending_evidence, list):
            raise ValueError("Pending request references must be a list")
        for reference in pending_evidence:
            index = reference.get("index")
            if (
                type(index) is not int
                or not checkpoint < index < len(trace)
                or trace[index].get("kind") != "user"
            ):
                raise ValueError("Pending request reference is not a later owner request")
    return {**record, "source_id": entry["source_id"], "schema_version": 1}


def aggregate(manifest: dict) -> tuple[dict, list[dict]]:
    entries = {entry["source_id"]: entry for entry in manifest["entries"]}
    if len(entries) != len(manifest["entries"]):
        raise ValueError("Duplicate source in coverage manifest")
    latest = {}
    for value in manifest["review_journals"]:
        for record in journal(Path(value)):
            source_id = identity(record)
            if source_id not in entries:
                raise ValueError("Review source is outside the declared inventory")
            latest[source_id] = record
    for value in manifest.get("coverage_correction_journals", []):
        for correction in journal(Path(value)):
            source_id = identity(correction)
            record = latest.get(source_id)
            if record and correction.get("checkpoint_digest") == record.get("checkpoint_digest"):
                record = {**record}
                record["coverage"] = correction["coverage_corrected"]
                record["coverage_correction_digest"] = digest(correction)
                latest[source_id] = record
    proposals, invalid = [], []
    for source_id, record in latest.items():
        try:
            proposals.append(validate(record, entries[source_id]))
        except (ValueError, KeyError, TypeError) as exc:
            invalid.append({"source_id": source_id, "reason": str(exc)})
    reviewed = {r["source_id"] for r in proposals if r["checkpoint"] is not None}
    outcomes = Counter(r["outcome"] for r in proposals if r["checkpoint"] is not None)
    capabilities = Counter(
        capability
        for r in proposals
        if r["checkpoint"] is not None
        for capability in set(r.get("required_capabilities", []))
    )
    excluded = Counter(e["excluded_reason"] for e in entries.values() if e.get("excluded_reason"))
    missing = {
        e["source_id"]
        for e in entries.values()
        if e.get("checkpoint") is None and not e.get("excluded_reason")
    }
    pending = sorted(
        source_id
        for source_id, entry in entries.items()
        if source_id not in reviewed | missing and not entry.get("excluded_reason")
    )
    by_shard = defaultdict(Counter)
    for source_id, entry in entries.items():
        counts = by_shard[entry["shard"]]
        counts["inventoried"] += 1
        counts["reviewed_checkpoints"] += source_id in reviewed
        counts["missing_checkpoint"] += source_id in missing
        counts["excluded"] += bool(entry.get("excluded_reason"))
        counts["pending"] += source_id in pending
    report = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "manifest_digest": digest(manifest),
        "inventoried_roots_or_sessions": len(entries),
        "reviewed_checkpoints": len(reviewed),
        "missing_checkpoint": len(missing),
        "excluded": dict(excluded),
        "pending": len(pending),
        "pending_source_ids": pending,
        "invalid": invalid,
        "proposal_outcomes": dict(outcomes),
        "required_capabilities": dict(capabilities),
        "by_shard": {name: dict(counts) for name, counts in by_shard.items()},
        "human_validated_labels": 0,
        "routing_training_eligible_labels": 0,
        "benchmark_predictive_correlation": None,
        "limits": [
            "One selected checkpoint per root/session, not every task in its history.",
            "Inspection caps and uncertainty remain attached to individual proposals.",
            "Assistant proposals are not independently validated task outcomes.",
            "Capability counts overlap and do not supply benchmark weights.",
            "Roots include forks, repeated work and different model/harness eras.",
        ],
    }
    return report, proposals


def publish(path: Path, proposals: list[dict], manifest_digest: str) -> int:
    """Append accepted proposals; retain previous reviews and earlier revisions."""
    existing = {r.get("proposal_id") for r in journal(path) if r.get("proposal_id")}
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    added = 0
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "a") as handle:
        for record in proposals:
            proposal_id = digest(record)
            if proposal_id in existing:
                continue
            handle.write(
                json.dumps(
                    {
                        **record,
                        "proposal_id": proposal_id,
                        "coverage_manifest_digest": manifest_digest,
                    }
                )
                + "\n"
            )
            existing.add(proposal_id)
            added += 1
        handle.flush()
        os.fsync(handle.fileno())
    return added


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--publish-to", type=Path)
    args = parser.parse_args()
    report, proposals = aggregate(json.loads(args.manifest.read_text()))
    if args.publish_to:
        if report["invalid"]:
            parser.error("Invalid proposals must be resolved before publication")
        report["new_published_proposals"] = publish(
            args.publish_to, proposals, report["manifest_digest"]
        )
    args.output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(report, handle, indent=2)
    temporary.replace(args.output)
    print(
        json.dumps({k: v for k, v in report.items() if k not in {"pending_source_ids", "limits"}})
    )
