"""Evaluate conditional outcome predictors on frozen, provisional saved-work targets.

The fixed comparison is task/setup features versus those features plus one
public benchmark. Grouped and chronological held-outs are retrospective proxy
tests, never a causal routing backtest or calibrated counterfactual prediction.
NumPy, when already available, accelerates the same regularized softmax fit.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
from collections import Counter, defaultdict
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

try:
    from scripts.routing_prediction_dataset import AXES, canonical_hash
except ModuleNotFoundError:
    from routing_prediction_dataset import AXES, canonical_hash

LABELS = ("success", "partial", "failed")
TASK_TYPES = (
    "coding",
    "terminal",
    "research",
    "editorial_visual",
    "browser_native",
    "planning_explanation",
    "mixed",
    "unknown",
)
HARNESSES = ("codex", "zcode", "omnigent")
EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultra", "other")
BENCHMARKS = (
    "terminal-bench-4.0-official",
    "aa-coding-agent-index",
    "aa-intelligence",
)


def softmax(values: list[float]) -> list[float]:
    top = max(values)
    exp = [math.exp(v - top) for v in values]
    return [v / sum(exp) for v in exp]


def probabilities(weights: list[list[float]], row: list[float]) -> list[float]:
    return softmax([sum(v * w[k] for v, w in zip(row, weights, strict=True)) for k in range(3)])


def fit(
    x: list[list[float]],
    y: list[int],
    *,
    penalty: float = 10.0,
    accelerated: bool = True,
    maximum_iterations: int = 1500,
) -> dict:
    """L2 softmax, with one intercept-only pseudocount per outcome category."""
    if (
        not x
        or not x[0]
        or len(x) != len(y)
        or not math.isfinite(penalty)
        or penalty <= 0
        or maximum_iterations < 1
        or any(type(k) is not int or k not in range(3) for k in y)
    ):
        raise ValueError("Invalid predictor fitting inputs")
    width, n = len(x[0]), len(x)
    if any(len(row) != width or row[0] != 1 or not all(map(math.isfinite, row)) for row in x):
        raise ValueError("Fit requires finite equal-width rows with an intercept")
    counts = Counter(y)
    weights = [[0.0] * 3 for _ in range(width)]
    weights[0] = [math.log((counts[k] + 1) / (n + 3)) for k in range(3)]
    np = None
    if accelerated:
        with suppress(ImportError):
            import numpy as np

    if np is not None:
        matrix = np.asarray(x, dtype=float)
        targets = np.eye(3)[y]

        def loss_gradient(value: list[list[float]]) -> tuple[float, list[list[float]]]:
            theta = np.asarray(value, dtype=float)
            logits = matrix @ theta
            logits -= logits.max(axis=1, keepdims=True)
            logp = logits - np.log(np.exp(logits).sum(axis=1, keepdims=True))
            p = np.exp(logp)
            prior = np.asarray(softmax(value[0]))
            loss = float(-(targets * logp).sum() / n)
            loss += penalty * float((theta[1:] ** 2).sum()) / (2 * n)
            loss -= sum(math.log(v) for v in prior) / n
            gradient = matrix.T @ (p - targets) / n
            gradient[1:] += penalty * theta[1:] / n
            gradient[0] += (3 * prior - 1) / n
            return loss, gradient.tolist()

    else:

        def loss_gradient(value: list[list[float]]) -> tuple[float, list[list[float]]]:
            gradient, loss = [[0.0] * 3 for _ in range(width)], 0.0
            for row, target in zip(x, y, strict=True):
                p = probabilities(value, row)
                loss -= math.log(max(p[target], 1e-300)) / n
                for i, feature in enumerate(row):
                    for k in range(3):
                        gradient[i][k] += feature * (p[k] - (k == target)) / n
            for i in range(1, width):
                for k in range(3):
                    loss += penalty * value[i][k] ** 2 / (2 * n)
                    gradient[i][k] += penalty * value[i][k] / n
            prior = softmax(value[0])
            loss -= sum(math.log(v) for v in prior) / n
            for k in range(3):
                gradient[0][k] += (3 * prior[k] - 1) / n
            return loss, gradient

    step, converged = 1.0, False
    loss, gradient = loss_gradient(weights)
    for _iteration in range(1, maximum_iterations + 1):
        norm = sum(v * v for row in gradient for v in row)
        if math.sqrt(norm) < 1e-6:
            converged = True
            break
        trial_step = min(step * 1.5, 4.0)
        for _ in range(40):
            candidate = [
                [v - trial_step * g for v, g in zip(row, gr, strict=True)]
                for row, gr in zip(weights, gradient, strict=True)
            ]
            candidate_loss, candidate_gradient = loss_gradient(candidate)
            if candidate_loss <= loss - 0.0001 * trial_step * norm:
                break
            trial_step /= 2
        else:
            raise ValueError("Predictor optimizer failed to find a descent step")
        weights, loss, gradient, step = candidate, candidate_loss, candidate_gradient, trial_step
    gradient_norm = math.sqrt(sum(v * v for row in gradient for v in row))
    return {
        "weights": weights,
        "penalty": penalty,
        "intercept_pseudocount_per_class": 1,
        "iterations": _iteration,
        "converged": converged or gradient_norm < 1e-6,
        "gradient_norm": gradient_norm,
        "training_objective": loss,
        "training_class_counts": {LABELS[k]: counts[k] for k in range(3)},
        "optimizer_backend": "numpy" if np is not None else "standard_library",
        "calibrated": False,
    }


def numeric(row: dict, metric: str | list[str] | None) -> list[float | None]:
    values = [row["difficulty"]["axes"][axis] for axis in AXES]
    values.append(sum(v is None for v in values) / len(AXES))
    if metric is not None:
        for key in [metric] if isinstance(metric, str) else metric:
            values.append(row["public_scores"][key]["score"])
    return values


def prepare(training: list[dict], metric: str | list[str] | None) -> dict:
    columns = list(zip(*(numeric(row, metric) for row in training), strict=True))
    medians, means, scales = [], [], []
    for column in columns:
        observed = [v for v in column if v is not None]
        middle = median(observed) if observed else 0.0
        filled = [v if v is not None else middle for v in column]
        mean = sum(filled) / len(filled)
        scale = math.sqrt(sum((v - mean) ** 2 for v in filled) / len(filled)) or 1.0
        medians.append(middle)
        means.append(mean)
        scales.append(scale)
    return {"metric": metric, "medians": medians, "means": means, "scales": scales}


def vector(row: dict, preparation: dict) -> list[float]:
    values = numeric(row, preparation["metric"])
    standardized = [
        ((value if value is not None else middle) - mean) / scale
        for value, middle, mean, scale in zip(
            values,
            preparation["medians"],
            preparation["means"],
            preparation["scales"],
            strict=True,
        )
    ]
    effort = row["recorded_effort"] if row["recorded_effort"] in EFFORTS else "other"
    # Harness and effort are known configuration covariates, never outcome-derived features.
    return [
        1.0,
        *standardized,
        *(float(row["difficulty"]["task_type"] == kind) for kind in TASK_TYPES),
        *(float(row["harness"] == harness) for harness in HARNESSES),
        *(float(effort == level) for level in EFFORTS),
    ]


def date(row: dict) -> datetime:
    value = datetime.fromisoformat(row["dispatch_timestamp"].replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("Dispatch time requires an explicit timezone")
    return value


def splits(rows: list[dict], method: str, cutoff: datetime) -> list[tuple]:
    result = []
    if method == "chronological":
        # A shared task family crossing the cutoff is not allowed in both sides.
        bounds = defaultdict(list)
        for row in rows:
            bounds[row["task_group"]].append(date(row))
        train = [r for r in rows if max(bounds[r["task_group"]]) < cutoff]
        test = [r for r in rows if min(bounds[r["task_group"]]) >= cutoff]
        return [("chronological", train, test)]
    if method not in {"project_family", "model_effort_profile"}:
        raise ValueError("Unknown held-out scheme")
    for key in sorted(
        {
            row["shard"]
            if method == "project_family"
            else (row["recorded_model"], row["recorded_effort"])
            for row in rows
        }
    ):
        test = [
            row
            for row in rows
            if (
                row["shard"]
                if method == "project_family"
                else (row["recorded_model"], row["recorded_effort"])
            )
            == key
        ]
        groups = {row["task_group"] for row in test}
        test_ids = {row["case_id"] for row in test}
        train = [
            row
            for row in rows
            if row["case_id"] not in test_ids and row["task_group"] not in groups
        ]
        result.append((str(key), train, test))
    return result


def losses(p: list[float], target: str) -> dict:
    index = LABELS.index(target)
    return {
        "log_loss": -math.log(max(p[index], 1e-15)),
        "brier": sum((value - (k == index)) ** 2 for k, value in enumerate(p)),
    }


def interval(records: list[dict], cluster: str, field: str) -> list[float] | None:
    grouped = defaultdict(list)
    for row in records:
        grouped[row[cluster]].append(row[field])
    if len(grouped) < 3:
        return None
    groups, generator, means = list(grouped.values()), random.Random(20261012), []
    for _ in range(2000):
        selected = [groups[generator.randrange(len(groups))] for _ in groups]
        means.append(sum(sum(group) for group in selected) / sum(map(len, selected)))
    means.sort()
    return [means[49], means[1949]]


def evaluate(
    rows: list[dict], metric: str | list[str] | None, method: str, cutoff: datetime
) -> dict:
    records, optimizer, omitted_folds = [], [], []
    for key, train, test in splits(rows, method, cutoff):
        if len(train) < 3 or not test:
            omitted_folds.append(
                {"fold": key, "training_rows": len(train), "test_rows": len(test)}
            )
            continue
        assert not {r["task_group"] for r in train} & {r["task_group"] for r in test}
        counts = Counter(r["target"]["outcome"] for r in train)
        prior = [(counts[label] + 1) / (len(train) + 3) for label in LABELS]
        models = {}
        for name, feature in (("context", None), ("context_plus_score", metric)):
            prep = prepare(train, feature)
            fitted = fit(
                [vector(r, prep) for r in train],
                [LABELS.index(r["target"]["outcome"]) for r in train],
            )
            if not fitted["converged"]:
                raise ValueError("Predictor optimizer did not converge")
            optimizer.append(
                {
                    "fold": key,
                    "variant": name,
                    **{k: v for k, v in fitted.items() if k != "weights"},
                }
            )
            models[name] = prep, fitted
        for row in test:
            prediction = {"class_prior": prior}
            for name, (prep, fitted) in models.items():
                prediction[name] = probabilities(fitted["weights"], vector(row, prep))
            scored = {name: losses(p, row["target"]["outcome"]) for name, p in prediction.items()}
            records.append(
                {
                    "case_id": row["case_id"],
                    "task_group": row["task_group"],
                    "project_family": row["shard"],
                    "model_effort_profile": row["recorded_model"] + "/" + row["recorded_effort"],
                    "fold": key,
                    "outcome": row["target"]["outcome"],
                    "probabilities": prediction,
                    "losses": scored,
                    "log_loss_improvement": scored["context"]["log_loss"]
                    - scored["context_plus_score"]["log_loss"],
                    "brier_improvement": scored["context"]["brier"]
                    - scored["context_plus_score"]["brier"],
                }
            )
    summary = {
        "heldout_scheme": method,
        "test_predictions": len(records),
        "task_groups": len({r["task_group"] for r in records}),
        "model_effort_profiles": len({r["model_effort_profile"] for r in records}),
        "target_counts": dict(Counter(r["outcome"] for r in records)),
        "omitted_folds": omitted_folds,
        "optimizer": optimizer,
        "calibrated": False,
        "causal_routing_improvement": None,
        "router_eligible": False,
    }
    if records:
        summary["average_loss"] = {
            name: {
                field: sum(r["losses"][name][field] for r in records) / len(records)
                for field in ("log_loss", "brier")
            }
            for name in ("class_prior", "context", "context_plus_score")
        }
        summary["benchmark_increment"] = {
            field: {
                "mean_improvement": sum(r[field] for r in records) / len(records),
                "profile_cluster_95_percent_interval": interval(
                    records, "model_effort_profile", field
                ),
                "task_cluster_95_percent_interval": interval(records, "task_group", field),
            }
            for field in ("log_loss_improvement", "brier_improvement")
        }
    return {"summary": summary, "heldout_predictions": records}


def join(episodes: list[dict], directory: Path, catalog: dict, judge: dict) -> list[dict]:
    rows = {r["case_id"]: r for r in episodes}
    if len(rows) != len(episodes):
        raise ValueError("Duplicate episode case")
    manifest = json.loads((directory / "manifest.json").read_text())
    features = {}
    for number in (1, 2):
        packet_file = directory / f"difficulty-packets-{number}.json"
        packets = json.loads(packet_file.read_text())
        expected = {p["case_id"]: p for p in packets}
        if len(expected) != len(packets) or not set(expected) <= set(rows):
            raise ValueError("Duplicate or unbound difficulty packet")
        file_hash = hashlib.sha256(packet_file.read_bytes()).hexdigest()
        if file_hash != manifest["packet_files"][packet_file.name]:
            raise ValueError("Frozen difficulty packet file changed")
        path = directory / f"difficulty-review-{number}.jsonl"
        if not path.exists():
            raise ValueError("Difficulty feature collection is not complete")
        seen = set()
        for line in path.read_text().splitlines():
            value = json.loads(line)
            case_id = value["case_id"]
            if case_id not in expected or case_id in features:
                raise ValueError("Duplicate or unexpected difficulty case")
            if (
                value["input_packet_sha256"] != canonical_hash(expected[case_id])
                or value["input_packet_file_sha256"] != file_hash
            ):
                raise ValueError("Difficulty input changed")
            if (
                value["judge_model"] != judge["configured_model"]
                or value["judge_effort"] != judge["configured_reasoning"]
                or value["rubric_version"] != "hindsight-evaluator-v1-difficulty"
            ):
                raise ValueError("Difficulty judge profile or rubric changed")
            if (
                value["human_validated"] is not False
                or value["routing_training_eligible"] is not False
            ):
                raise ValueError("Difficulty features must retain provisional provenance")
            if set(value["axes"]) != set(AXES) or any(
                v is not None and (type(v) is not int or not 0 <= v <= 3)
                for v in value["axes"].values()
            ):
                raise ValueError("Invalid difficulty axis")
            if value["task_type"] not in TASK_TYPES:
                raise ValueError("Unknown predeclared task type")
            if value["overall_band"] not in {"easy", "moderate", "hard", "very_hard", "unknown"}:
                raise ValueError("Unknown difficulty band")
            confidence = value["confidence"]
            if (
                type(confidence) not in (int, float)
                or not math.isfinite(confidence)
                or not 0 <= confidence <= 1
            ):
                raise ValueError("Invalid difficulty confidence")
            packet = expected[case_id]
            allowed_indices = {
                packet["task_request"]["index"],
                *(item["index"] for item in packet["context_available_before_request"]),
            }
            indices = value["preexecution_evidence_indices"]
            if not isinstance(indices, list) or any(
                type(index) is not int or index not in allowed_indices for index in indices
            ):
                raise ValueError("Difficulty evidence was not in the permitted packet")
            features[case_id] = value
            seen.add(case_id)
        if seen != set(expected):
            raise ValueError("Difficulty feature collection is incomplete")
    lookup, seen_scores = defaultdict(dict), set()
    for score in catalog["profiles"]:
        if score.get("match_basis") != "documented_model_and_effort":
            continue
        key = score["trace_model"], score["trace_effort"]
        identity = score["benchmark"], key
        if identity in seen_scores:
            raise ValueError("Ambiguous score crosswalk")
        seen_scores.add(identity)
        if score.get("score") is not None and not score.get("estimated", False):
            if not math.isfinite(score["score"]):
                raise ValueError("Non-finite public score")
            lookup[score["benchmark"]][key] = score
    result = []
    for case_id, difficulty in features.items():
        row = rows[case_id]
        key = row["recorded_model"], row["recorded_effort"]
        result.append(
            {
                **row,
                "difficulty": difficulty,
                "public_scores": {
                    metric: lookup[metric][key] for metric in BENCHMARKS if key in lookup[metric]
                },
            }
        )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("score_crosswalk", type=Path)
    parser.add_argument("judge_profile", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    episodes = json.loads((args.directory / "episodes.json").read_text())
    judge = json.loads(args.judge_profile.read_text())
    contract_hash = hashlib.sha256(
        (args.judge_profile.parent / "evaluator-contract.md").read_bytes()
    ).hexdigest()
    if contract_hash != judge["frozen_contract_sha256"]:
        raise ValueError("Frozen evaluator contract changed")
    rows = join(
        episodes,
        args.directory,
        json.loads(args.score_crosswalk.read_text()),
        judge,
    )
    ordered = sorted(date(r) for r in rows if r["primary_scope"])
    cutoff = ordered[int(len(ordered) * 0.8)]
    results = []
    for metric in [*BENCHMARKS, list(BENCHMARKS)]:
        scopes = (
            ("primary", "including_inferred_scope_sensitivity", "primary_common_features")
            if isinstance(metric, str)
            else ("primary_common_features",)
        )
        for scope in scopes:
            required = list(BENCHMARKS) if scope == "primary_common_features" else [metric]
            selected = [
                r
                for r in rows
                if all(key in r["public_scores"] for key in required)
                and r["target"]["outcome"] in LABELS
                and (scope == "including_inferred_scope_sensitivity" or r["primary_scope"])
            ]
            for method in ("project_family", "chronological", "model_effort_profile"):
                evaluation = evaluate(selected, metric, method, cutoff)
                results.append(
                    {
                        "benchmark": metric,
                        "scope": scope,
                        "eligible_rows": len(selected),
                        **evaluation,
                    }
                )
    output = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "episodes": len(episodes),
        "difficulty_packets": len(rows),
        "primary_scope_packets": sum(r["primary_scope"] for r in rows),
        "uncertain_target_labels": sum(r["target"]["outcome"] == "not_sure" for r in rows),
        "chronological_cutoff_utc": cutoff.isoformat(),
        "fit_plan": {
            "l2_penalty": 10.0,
            "intercept_pseudocount_per_class": 1,
            "hyperparameter_search": False,
            "bootstrap_seed": 20261012,
            "bootstrap_draws": 2000,
            "primary_metric": "multiclass_log_loss",
            "secondary_metric": "multiclass_brier_sum",
            "features": (
                "six blind difficulty axes, unknown count, task type, harness and configured "
                "effort; plus one benchmark or the predeclared joint three-score input"
            ),
            "cutoff_selection": (
                "80th percentile of primary packet dispatch times before known-target filtering"
            ),
        },
        "results": results,
        "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "input_hashes": {
            str(path): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (
                args.directory / "episodes.json",
                args.directory / "manifest.json",
                args.score_crosswalk,
                args.judge_profile,
                args.judge_profile.parent / "evaluator-contract.md",
                args.directory / "difficulty-packets-1.json",
                args.directory / "difficulty-packets-2.json",
                args.directory / "difficulty-review-1.jsonl",
                args.directory / "difficulty-review-2.jsonl",
            )
        },
        "target_provenance": "Frozen v0 assistant judgments, not verified executor performance",
        "calibrated": False,
        "counterfactual_validation": False,
        "router_eligible": False,
        "limits": [
            "Labels, scopes and benchmark runtime equivalence remain provisional.",
            (
                "Difficulty is rated blind from bounded pre-execution views; "
                "unknown axes remain missing."
            ),
            "Task grouping catches exact shared original prompts, not all semantic incidents.",
            (
                "Cluster intervals use fixed held-out predictions and do not capture "
                "all fit/evaluator uncertainty."
            ),
            (
                "Three compared features and multiple splits are exploratory comparisons, "
                "not independent confirmations."
            ),
            (
                "Current public scores are retrospective features, "
                "not historically available routing data."
            ),
            (
                "No untried executor outcomes are observed and "
                "no production routing change is admitted."
            ),
        ],
    }
    args.output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(output, handle, indent=2)
        handle.write("\n")
    print(
        json.dumps(
            {
                k: output[k]
                for k in (
                    "difficulty_packets",
                    "primary_scope_packets",
                    "uncertain_target_labels",
                    "chronological_cutoff_utc",
                )
            }
        )
    )
    for item in results:
        print(
            json.dumps(
                {
                    "benchmark": item["benchmark"],
                    "scope": item["scope"],
                    **{k: v for k, v in item["summary"].items() if k not in {"optimizer"}},
                }
            )
        )


if __name__ == "__main__":
    main()
