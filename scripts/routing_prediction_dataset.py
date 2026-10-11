"""Freeze pre-execution packets and provisional targets from saved review evidence.

This offline exporter never sends outcome/model metadata to a difficulty worker.
Declared single-request scopes form the primary dataset. An evidence-reference
fallback is explicitly sensitivity-only, not an invented authoritative scope.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

try:
    from scripts.routing_review_aggregate import aggregate, digest, load_trace
except ModuleNotFoundError:
    from routing_review_aggregate import aggregate, digest, load_trace

AXES = (
    "acceptance_scope",
    "systems_coupling",
    "novelty_ambiguity",
    "verification_burden",
    "context_dependence",
    "environment_constraints",
)
AMBIENT = (
    "<subagent_notification>",
    "<recommended_plugins>",
    "<skills_instructions>",
    "<codex_internal_context",
    "# AGENTS.md instructions",
)
MODEL = re.compile(r"\b(?:gpt-[\w.-]+|GLM-[\w.-]+|openai-ab)\b", re.I)


def canonical_hash(value: object) -> str:
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(data.encode()).hexdigest()


def owner_request(item: dict) -> bool:
    return item.get("kind") == "user" and not item.get("text", "").lstrip().startswith(AMBIENT)


def request_start(proposal: dict, trace: list[dict]) -> tuple[int | None, str]:
    end = proposal["checkpoint"]
    scope, coverage = proposal.get("scope"), proposal.get("coverage")
    if isinstance(scope, dict) and type(scope.get("start_index")) is int:
        return scope["start_index"], "declared_scope"
    if isinstance(coverage, dict) and isinstance(coverage.get("inspected"), dict):
        span = coverage["inspected"].get("latest_span")
        if isinstance(span, list) and len(span) == 2 and type(span[0]) is int:
            return span[0], "declared_inspected_span"
    users = [i for i, item in enumerate(trace[: end + 1]) if owner_request(item)]
    if len(users) == 1:
        return users[0], "single_request_prefix"
    refs = [r["index"] for r in proposal["evidence"] if owner_request(trace[r["index"]])]
    if len(set(refs)) == 1 and users and refs[0] == users[-1]:
        return refs[0], "inferred_evidence_request_sensitivity_only"
    return None, "unresolved_scope"


def packet(trace: list[dict], start: int, case_id: str) -> dict:
    def view(index: int, cap: int) -> dict:
        item = trace[index]
        text = item.get("text", "")
        return {
            "index": index,
            "kind": item["kind"],
            "text": MODEL.sub("[MODEL_ID]", text[:cap]),
            "truncated": len(text) > cap,
            "source_characters": len(text),
        }

    # Only preceding visible conversation is available at the decision episode.
    visible = [
        i
        for i, item in enumerate(trace[:start])
        if owner_request(item)
        or (item.get("kind") == "assistant" and item.get("channel") != "commentary")
    ]
    chosen = visible[-4:]
    return {
        "case_id": case_id,
        "task_request": view(start, 12000),
        "context_available_before_request": [view(i, 4500) for i in chosen],
        "context_coverage": {
            "visible_prior_items": len(visible),
            "included_prior_items": len(chosen),
            "older_context_omitted": len(visible) > len(chosen),
            "future_execution_and_outcomes_included": False,
            "model_literal_redaction": True,
        },
    }


def profile(row: dict) -> tuple:
    return row.get("model"), row.get("effort")


def normalize(text: str) -> str:
    return " ".join(text.lower().split())


def build_record(entry: dict, proposal: dict, trace: list[dict]) -> tuple[dict | None, str]:
    end = proposal["checkpoint"]
    start, basis = request_start(proposal, trace)
    if start is None or not 0 <= start < end:
        return None, basis
    if not owner_request(trace[start]):
        return None, "declared_scope_does_not_start_at_owner_request"
    requests = [i for i in range(start, end + 1) if owner_request(trace[i])]
    if requests != [start]:
        return None, "later_owner_steering_or_multiple_requests"
    models = {profile(row) for row in trace[start : end + 1] if row.get("kind") == "assistant"}
    chosen_profile = profile(trace[end])
    if models != {chosen_profile} or None in chosen_profile:
        return None, "mixed_or_missing_executor_configuration"
    if any(
        "spawn_agent" in row.get("kind", "")
        or "collaboration" in row.get("kind", "")
        or row.get("kind") in {"agent", "task", "delegate"}
        for row in trace[start : end + 1]
    ):
        return None, "observed_delegated_execution"
    request = trace[start].get("text", "").strip()
    if len(request) < 80:
        return None, "short_request_needs_scope_reconstruction"
    case_id = digest({"source": entry["source_id"], "request": start, "checkpoint": end})[:20]
    blind = packet(trace, start, case_id)
    first = next(item["text"] for item in trace[: end + 1] if owner_request(item))
    # Group exact shared original prompts across forks/sessions without outcome input.
    lineage_text = normalize(first) if len(first.strip()) >= 80 else normalize(request)
    task_group = hashlib.sha256(lineage_text.encode()).hexdigest()
    return {
        "case_id": case_id,
        "source_id": entry["source_id"],
        "shard": entry["shard"],
        "harness": entry["source_id"].partition(":")[0],
        "request_index": start,
        "checkpoint": end,
        "scope_basis": basis,
        "primary_scope": basis != "inferred_evidence_request_sensitivity_only",
        "task_group": task_group,
        "request_sha256": hashlib.sha256(normalize(request).encode()).hexdigest(),
        "dispatch_timestamp": trace[start].get("timestamp"),
        "recorded_model": chosen_profile[0],
        "recorded_effort": chosen_profile[1],
        "configuration_identity_confirmed": True,
        "backend_identity_confirmed": False,
        "input_packet": blind,
        "input_packet_sha256": canonical_hash(blind),
        "target": {
            "outcome": proposal["outcome"],
            "confidence": proposal["confidence"],
            "proposal_digest": digest(proposal),
            "checkpoint_digest": proposal["checkpoint_digest"],
            "review_source": "assistant_proposal",
            "human_validated": False,
        },
    }, "included"


def save(path: Path, value: object) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(value, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("score_crosswalk", type=Path)
    parser.add_argument("output_directory", type=Path)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text())
    coverage, proposals = aggregate(manifest)
    if coverage["pending"] or coverage["invalid"]:
        parser.error("Frozen proposals must have complete valid checkpoint coverage")
    entries = {e["source_id"]: e for e in manifest["entries"]}
    catalog = json.loads(args.score_crosswalk.read_text())
    public_keys = {
        (r["trace_model"], r["trace_effort"])
        for r in catalog["profiles"]
        if r.get("match_basis") == "documented_model_and_effort"
        and r.get("score") is not None
        and not r.get("estimated", False)
    }
    rows, omitted = [], Counter()
    for proposal in proposals:
        if proposal["checkpoint"] is None:
            continue
        entry = entries[proposal["source_id"]]
        row, reason = build_record(entry, proposal, load_trace(entry))
        if row is None:
            omitted[reason] += 1
        else:
            row["public_feature_available"] = (
                row["recorded_model"],
                row["recorded_effort"],
            ) in public_keys
            rows.append(row)
    rows.sort(key=lambda r: r["case_id"])
    # Feature extraction selection does not inspect outcomes or confidence.
    candidates = [r for r in rows if r["public_feature_available"]]
    for number in range(2):
        save(
            args.output_directory / f"difficulty-packets-{number + 1}.json",
            [r["input_packet"] for r in candidates[number::2]],
        )
    save(args.output_directory / "episodes.json", rows)
    summary = {
        "schema_version": 1,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "corpus_manifest_digest": digest(manifest),
        "exporter_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "selected_checkpoints": coverage["reviewed_checkpoints"],
        "included_episodes": len(rows),
        "primary_scope": sum(r["primary_scope"] for r in rows),
        "sensitivity_only_scope": sum(not r["primary_scope"] for r in rows),
        "omitted": dict(omitted),
        "public_feature_difficulty_packets": len(candidates),
        "primary_public_feature_packets": sum(r["primary_scope"] for r in candidates),
        "by_shard": dict(Counter(r["shard"] for r in candidates)),
        "packet_files": {
            name: hashlib.sha256((args.output_directory / name).read_bytes()).hexdigest()
            for name in ("difficulty-packets-1.json", "difficulty-packets-2.json")
        },
        "target_provenance": "Frozen v0 assistant proposals, not independently validated truth",
        "selection_reads_target_labels": False,
        "labels_sent_to_difficulty_worker": False,
        "router_eligible": False,
    }
    save(args.output_directory / "manifest.json", summary)
    print(json.dumps({k: v for k, v in summary.items() if k != "packet_files"}))


if __name__ == "__main__":
    main()
