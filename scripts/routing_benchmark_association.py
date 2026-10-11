"""Describe public benchmark associations with frozen retrospective proposals.

This is an offline research calculation. It does not fit a router, calibrate
completion probabilities, alter labels or establish causal model improvements.
Public score crosswalks must explicitly bind model AND effort; missing scores
remain missing. A model/effort match does not qualify harness or backend parity.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

try:
    from scripts.routing_review_aggregate import aggregate, digest, load_trace
except ModuleNotFoundError:
    from routing_review_aggregate import aggregate, digest, load_trace


def collect(manifest: dict) -> list[dict]:
    coverage, proposals = aggregate(manifest)
    if coverage["invalid"] or coverage["pending"]:
        raise ValueError("Only a complete, validated frozen proposal inventory is accepted")
    entries = {e["source_id"]: e for e in manifest["entries"]}
    rows = []
    for proposal in proposals:
        if proposal["checkpoint"] is None:
            continue
        entry = entries[proposal["source_id"]]
        trace = load_trace(entry)
        checkpoint = trace[proposal["checkpoint"]]
        evidence_profiles = sorted(
            {
                (trace[ref["index"]].get("model"), trace[ref["index"]].get("effort"))
                for ref in proposal["evidence"]
                if trace[ref["index"]]["kind"] not in {"user", "runtime notice"}
            },
            key=lambda pair: (pair[0] or "", pair[1] or ""),
        )
        profile = (checkpoint.get("model"), checkpoint.get("effort"))
        rows.append(
            {
                "source_id": proposal["source_id"],
                "shard": entry["shard"],
                "checkpoint": proposal["checkpoint"],
                "checkpoint_digest": proposal["checkpoint_digest"],
                "proposal_digest": digest(proposal),
                "recorded_model": profile[0],
                "recorded_effort": profile[1],
                "timestamp": checkpoint.get("timestamp"),
                "outcome": proposal["outcome"],
                "confidence": proposal["confidence"],
                "review_source": proposal["review_source"],
                "human_reviewed": proposal["human_reviewed"],
                "score_eligible": proposal["score_eligible"],
                "capabilities": proposal["required_capabilities"],
                "evidence_profiles": evidence_profiles,
                "evidence_profile_mismatch": any(p != profile for p in evidence_profiles),
                "attribution_source": {
                    "codex": "turn_context configuration",
                    "zcode": "latest model_usage linked to assistant_message_id",
                    "omnigent": "response-execution session-reported attribution",
                }[proposal["source_id"].partition(":")[0]],
            }
        )
    return rows


def ranks(values: list[float]) -> list[float]:
    """Average ranks for ties; correlations are at model/effort-profile grain."""
    order = sorted(range(len(values)), key=values.__getitem__)
    result = [0.0] * len(values)
    start = 0
    while start < len(order):
        end = start + 1
        while end < len(order) and values[order[end]] == values[order[start]]:
            end += 1
        rank = (start + 1 + end) / 2
        for index in order[start:end]:
            result[index] = rank
        start = end
    return result


def spearman(x: list[float], y: list[float]) -> float | None:
    if len(x) != len(y):
        raise ValueError("Different correlation lengths")
    if len(x) < 3 or len(set(x)) < 2 or len(set(y)) < 2:
        return None
    rx, ry = ranks(x), ranks(y)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    dx, dy = [v - mx for v in rx], [v - my for v in ry]
    return sum(a * b for a, b in zip(dx, dy, strict=True)) / math.sqrt(
        sum(v * v for v in dx) * sum(v * v for v in dy)
    )


def profile_summary(rows: list[dict]) -> list[dict]:
    groups = defaultdict(list)
    for row in rows:
        groups[(row["recorded_model"], row["recorded_effort"])].append(row)
    result = []
    for (model, effort), group in groups.items():
        outcomes = Counter(row["outcome"] for row in group)
        known = len(group) - outcomes["not_sure"]
        result.append(
            {
                "model": model,
                "effort": effort,
                "n": len(group),
                "proposed_outcomes": dict(outcomes),
                "known_proposals": known,
                "proposal_completion_rate": outcomes["success"] / known if known else None,
                "shards": dict(Counter(row["shard"] for row in group)),
                "date_min": min(
                    (row["timestamp"] for row in group if row["timestamp"]), default=None
                ),
                "date_max": max(
                    (row["timestamp"] for row in group if row["timestamp"]), default=None
                ),
                "evidence_profile_mismatch": sum(
                    row["evidence_profile_mismatch"] for row in group
                ),
            }
        )
    return sorted(
        result, key=lambda group: (-group["n"], group["model"] or "", group["effort"] or "")
    )


def associations(rows: list[dict], catalog: dict) -> list[dict]:
    """Use explicit source-backed crosswalks; never borrow another effort's score."""
    by_benchmark = defaultdict(dict)
    for profile in catalog["profiles"]:
        if profile.get("match_basis") != "documented_model_and_effort":
            continue
        key = (profile["trace_model"], profile["trace_effort"])
        metric = profile["benchmark"]
        if key in by_benchmark[metric]:
            raise ValueError("Ambiguous public score for model/effort and benchmark")
        if not profile.get("source_url", "").startswith("https://"):
            raise ValueError("Public score requires an inspectable primary source")
        score = profile.get("score")
        if score is not None and (type(score) not in {float, int} or not math.isfinite(score)):
            raise ValueError("Public score must be finite or missing")
        by_benchmark[metric][key] = profile
    result = []
    for metric, profiles in sorted(by_benchmark.items()):
        matched = []
        missing = Counter()
        for row in rows:
            key = (row["recorded_model"], row["recorded_effort"])
            profile = profiles.get(key)
            if profile is None:
                missing["no_documented_model_effort_match"] += 1
            elif profile.get("score") is None:
                missing["published_score_missing"] += 1
            elif profile.get("estimated", False):
                missing["estimated_public_score"] += 1
            else:
                matched.append(row)
        sensitivities = []
        for variant, filtered in (
            ("all_known_proposals", [r for r in matched if r["outcome"] != "not_sure"]),
            (
                "medium_high_confidence_unmixed_evidence",
                [
                    r
                    for r in matched
                    if r["outcome"] != "not_sure"
                    and r["confidence"] in {"medium", "high"}
                    and not r["evidence_profile_mismatch"]
                ],
            ),
        ):
            points = profile_summary(filtered)
            for point in points:
                public = profiles[(point["model"], point["effort"])]
                point.update(
                    public_score=public["score"],
                    source_url=public["source_url"],
                    published_profile_id=public["published_profile_id"],
                    harness=public.get("harness"),
                    version=public["version"],
                )
            thresholds = []
            for minimum in (1, 5, 10):
                selected = [p for p in points if p["known_proposals"] >= minimum]
                thresholds.append(
                    {
                        "minimum_proposals_per_profile": minimum,
                        "model_effort_profiles": len(selected),
                        "raw_model_keys": len({p["model"] for p in selected}),
                        "proposals": sum(p["known_proposals"] for p in selected),
                        "descriptive_profile_spearman": spearman(
                            [p["public_score"] for p in selected],
                            [p["proposal_completion_rate"] for p in selected],
                        ),
                    }
                )
            sensitivities.append({"variant": variant, "points": points, "thresholds": thresholds})
        result.append(
            {
                "benchmark": metric,
                "model_effort_matched_proposals": len(matched),
                "missing": dict(missing),
                "sensitivities": sensitivities,
                "predictive_validation": False,
                "difficulty_adjustment": "not_estimated",
                "usable_for_router_selection": False,
                "causal_completion_improvement": None,
                "calibrated_completion_probability": None,
            }
        )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("public_score_crosswalk", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text())
    catalog = json.loads(args.public_score_crosswalk.read_text())
    rows = collect(manifest)
    report = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "manifest_digest": digest(manifest),
        "public_score_crosswalk_digest": digest(catalog),
        "calculator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "checkpoint_proposals": len(rows),
        "raw_model_keys": len({r["recorded_model"] for r in rows}),
        "model_effort_profiles": len(profile_summary(rows)),
        "profiles": profile_summary(rows),
        "associations": associations(rows, catalog),
        "predictive_validation": False,
        "difficulty_adjustment": "not_estimated",
        "usable_for_router_selection": False,
        "router_weights": None,
        "limits": [
            "Outcomes are bounded assistant proposals, not independently validated ground truth.",
            "Recorded model/effort configuration is not a backend response identity receipt.",
            "Published model/effort matches do not establish harness, tools or provider parity.",
            "Different profiles served different tasks, projects and model-release eras.",
            "Forks, shared histories and subagent work are not independent trials.",
            "Correlations collapse to model/effort profiles; their sizes differ and are small.",
            "Public scores captured now are retrospective features, not historical routing data.",
            "No held-out predictive evaluation or causal routing improvement is claimed.",
        ],
    }
    args.output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(report, handle, indent=2)
        handle.write("\n")
    print(
        json.dumps(
            {
                k: report[k]
                for k in ("checkpoint_proposals", "raw_model_keys", "model_effort_profiles")
            }
        )
    )


if __name__ == "__main__":
    main()
