"""Quality-first, non-actuating policy for paired repository-task evidence.

Historic chosen-model outcomes and assistant proposals are deliberately not
accepted as counterfactuals. Repeats collapse into task groups. Admission uses
a predeclared coverage floor and a corrected exact sign test, not an invented
task-success probability. These are research admission rules, not calibration.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path


def comparison(
    records: list[dict],
    baseline: str,
    candidate: str,
    family: str,
    expected_profiles: dict[str, dict] | None = None,
) -> dict:
    blocks: dict[tuple, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    rejected = defaultdict(int)
    for row in records:
        profile = row.get("profile", {}).get("id")
        if profile not in {baseline, candidate} or row.get("task_family") != family:
            continue
        if (
            row.get("verifier_validated") is not True
            or row.get("offline_comparison_eligible") is not True
            or type(row.get("success")) is not bool
            or not row.get("run_id")
            or not row.get("task_group")
            or not row.get("task_id")
            or not row.get("task_fingerprint")
            or not row.get("verifier_hashes")
            or not isinstance(row.get("environment"), dict)
            or type(row.get("budget_seconds")) is not int
            or row["budget_seconds"] <= 0
        ):
            rejected["incomplete_or_unverified_record"] += 1
            continue
        if expected_profiles is not None and any(
            row["profile"].get(k) != expected_profiles[profile].get(k) for k in ("model", "effort")
        ):
            rejected["requested_profile_mismatch"] += 1
            continue
        key = (
            row["run_id"],
            row["task_id"],
            row["task_group"],
            row["task_fingerprint"],
            row["budget_seconds"],
            json.dumps(row["environment"], sort_keys=True),
            json.dumps(row["verifier_hashes"], sort_keys=True),
        )
        blocks[key][profile].append(row)
    groups: dict[str, list[tuple[bool, bool]]] = defaultdict(list)
    for key, arms in blocks.items():
        if len(arms.get(baseline, [])) != 1 or len(arms.get(candidate, [])) != 1:
            rejected["unpaired_or_duplicate_treatment"] += 1
            continue
        base, other = arms[baseline][0], arms[candidate][0]
        groups[key[2]].append((base["success"], other["success"]))
    differences, baseline_rates, candidate_rates = [], [], []
    for pairs in groups.values():
        baseline_rates.append(sum(base for base, _ in pairs) / len(pairs))
        candidate_rates.append(sum(other for _, other in pairs) / len(pairs))
        differences.append(candidate_rates[-1] - baseline_rates[-1])
    wins, losses = sum(d > 0 for d in differences), sum(d < 0 for d in differences)
    discordant = wins + losses
    # H0: direction of a non-zero grouped paired effect is equally likely.
    # Independence and task-group validity remain assumptions, not guarantees.
    p_value = (
        sum(math.comb(discordant, k) for k in range(wins, discordant + 1)) / (2**discordant)
        if discordant
        else 1.0
    )
    return {
        "candidate": candidate,
        "baseline": baseline,
        "family": family,
        "task_groups": len(groups),
        "paired_trials": sum(len(pairs) for pairs in groups.values()),
        "candidate_wins": wins,
        "candidate_losses": losses,
        "ties": len(groups) - discordant,
        "group_mean_success_difference": sum(differences) / len(differences)
        if differences
        else None,
        "baseline_group_mean_success": sum(baseline_rates) / len(baseline_rates)
        if baseline_rates
        else None,
        "candidate_group_mean_success": sum(candidate_rates) / len(candidate_rates)
        if candidate_rates
        else None,
        "one_sided_sign_p": p_value,
        "rejected": dict(rejected),
    }


def recommend(
    context: dict,
    profiles: list[dict],
    records: list[dict],
    *,
    minimum_groups: int = 10,
    alpha: float = 0.05,
) -> dict:
    if minimum_groups < 1 or not 0 < alpha < 1:
        raise ValueError("Invalid predeclared evidence admission rule")
    by_id = {profile["id"]: profile for profile in profiles}
    if len(by_id) != len(profiles):
        raise ValueError("Profile IDs must be unique")
    current, baseline = context.get("current_profile_id"), context["baseline_profile_id"]
    result = {
        "schema_version": 1,
        "policy": "paired-completion-shadow-v1",
        "actuated": False,
        "task_family": context["task_family"],
        "admission": {"minimum_task_groups": minimum_groups, "alpha": alpha},
        "comparisons": [],
        "predicted_success_probability": None,
    }

    def finish(profile_id: str | None, reason: str) -> dict:
        return {**result, "recommended_profile_id": profile_id, "reason": reason}

    if context.get("explicit_profile_id") is not None:
        return finish(context["explicit_profile_id"], "preserve_explicit_choice")
    if context.get("has_native_session") and context.get("idle") is not True:
        return finish(current, "preserve_active_native_session")
    allowed = set(context["allowed_profile_ids"])
    qualified = {
        pid
        for pid, profile in by_id.items()
        if pid in allowed
        and profile.get("qualified") is True
        and all(
            profile.get(k) == context.get(k)
            for k in ("harness", "access_lane", "host_id", "workspace_id")
        )
    }
    if context.get("has_native_session"):
        qualified &= set(context.get("verified_continuation_profile_ids", [])) | {current}
    if baseline not in qualified:
        return finish(current, "baseline_unqualified_preserve_current")
    candidates = sorted(qualified - {baseline})
    threshold = alpha / max(1, len(candidates))
    result["admission"]["corrected_sign_p_threshold"] = threshold
    admitted = []
    for candidate in candidates:
        evidence = comparison(records, baseline, candidate, context["task_family"], by_id)
        evidence["admitted"] = (
            evidence["task_groups"] >= minimum_groups
            and (evidence["group_mean_success_difference"] or 0) > 0
            and evidence["one_sided_sign_p"] <= threshold
        )
        result["comparisons"].append(evidence)
        if evidence["admitted"]:
            admitted.append(evidence)
    if not admitted:
        return finish(baseline, "no_supported_completion_gain_use_baseline")
    best = max(
        admitted,
        key=lambda evidence: (
            evidence["candidate_group_mean_success"],
            evidence["group_mean_success_difference"],
        ),
    )
    return finish(best["candidate"], "paired_completion_gain_admitted_for_shadow_only")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("configuration", type=Path)
    parser.add_argument("results", type=Path, nargs="+")
    args = parser.parse_args()
    configuration = json.loads(args.configuration.read_text())
    records = [
        json.loads(line)
        for path in args.results
        for line in path.read_text().splitlines()
        if line.strip()
    ]
    print(
        json.dumps(
            recommend(
                configuration["context"],
                configuration["profiles"],
                records,
                **configuration.get("admission", {}),
            ),
            indent=2,
        )
    )
