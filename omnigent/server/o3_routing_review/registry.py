"""Benchmark definitions, evidence loading, and the audited O3 source universe."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable
from pathlib import Path

from .models import (
    BenchmarkEvidence,
    BenchmarkRequirement,
    BenchmarkSlice,
    CandidateProfile,
    CandidateSnapshot,
    EvidenceClass,
)

BENCHMARK_REGISTRY_ENV = "OMNIGENT_O3_BENCHMARK_REGISTRY"
SOURCE_POOL_NAME = "custom/o3-codex-pool"
ADVISER_COMBO_NAME = "custom/o3-routing-adviser"
DEFAULT_CANDIDATE_PROBE_REFERENCE = "Mac O3 full Responses/tool-continuation probe, 2026-09-03"

OFFICIAL_TB4_DATASET_REF = "dataset:terminal-bench/terminal-bench@v4.0.0"
OFFICIAL_SWE_BENCH_PRO_PUBLIC_REF = "dataset:ScaleAI/SWE-bench_Pro/public@731"
OFFICIAL_DRBENCH_FULL_REF = (
    "dataset:ServiceNow/drbench@"
    "0d699ecf6aa96b1de378595b432e9b16a82f0ed9#FullBenchmark-100"
)

_OPAQUE_MODEL_ALIASES = frozenset({"big-pickle"})


def manifest_digest(task_ids: Iterable[str]) -> str:
    """Hash canonical ordered manifest entries without inventing measurements."""
    payload = json.dumps(sorted(task_ids), separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()


def canonical_model_name(value: str) -> str:
    """Return the public-evidence identity, preserving unresolved opaque aliases."""
    normalized = value.strip().casefold()
    bare = normalized.rsplit("/", 1)[-1]
    if bare in _OPAQUE_MODEL_ALIASES:
        return normalized
    return bare


def is_codex_harness(value: str) -> bool:
    """Return whether a published result was produced by a Codex CLI family harness."""
    normalized = value.strip().casefold().replace("_", "-")
    return normalized in {"codex", "codex-cli", "codex-native", "openai-codex"}


# These are Control Room views, not official Terminal-Bench sub-benchmarks.
# Empty manifests intentionally mean "definition framework present, authoritative
# task mapping not yet imported". They carry the digest of that exact empty
# manifest and therefore cannot accidentally match an official result row.
_SLICE_SPECS: tuple[tuple[str, str, str], ...] = (
    (
        "tb4.cr-systems-db-v1",
        "Control Room systems and databases",
        "Control Room-derived view for systems, infrastructure, and database tasks.",
    ),
    (
        "tb4.cr-apps-data-v1",
        "Control Room applications and data",
        "Control Room-derived view for application engineering and data workflows.",
    ),
    (
        "tb4.cr-ai-v1",
        "Control Room AI systems",
        "Control Room-derived view for agents, inference, evaluation, and routing tasks.",
    ),
)


def _official_slice(
    *,
    benchmark_id: str,
    version: str,
    slice_id: str,
    label: str,
    interpretation: str,
    dataset_ref: str,
) -> BenchmarkSlice:
    manifest = [dataset_ref]
    return BenchmarkSlice(
        benchmark_id=benchmark_id,
        version=version,
        slice_id=slice_id,
        label=label,
        interpretation=interpretation,
        task_ids=manifest,
        task_manifest_digest=manifest_digest(manifest),
        official=True,
    )


def default_slices() -> list[BenchmarkSlice]:
    task_ids: list[str] = []
    digest = manifest_digest(task_ids)
    slices = [
        BenchmarkSlice(
            benchmark_id="terminal-bench",
            version="4.0.0",
            slice_id=slice_id,
            label=label,
            interpretation=interpretation,
            task_ids=task_ids,
            task_manifest_digest=digest,
            official=False,
        )
        for slice_id, label, interpretation in _SLICE_SPECS
    ]
    # Official aggregate rows use pinned dataset anchors rather than fabricated
    # task-ID lists. Every source record retains its published task/trial scope.
    slices.extend(
        [
            _official_slice(
                benchmark_id="terminal-bench",
                version="4.0.0",
                slice_id="tb4.overall",
                label="Terminal-Bench 4 overall",
                interpretation=(
                    "Terminal and agentic execution across the official full "
                    "Terminal-Bench 4.0.0 result."
                ),
                dataset_ref=OFFICIAL_TB4_DATASET_REF,
            ),
            _official_slice(
                benchmark_id="swe-bench-pro",
                version="public-leaderboard-2026-09-04",
                slice_id="swe-bench-pro.public.resolve-rate",
                label="SWE-Bench Pro public resolve rate",
                interpretation=(
                    "Long-horizon repository software-engineering capability on "
                    "the 731-instance public leaderboard snapshot."
                ),
                dataset_ref=OFFICIAL_SWE_BENCH_PRO_PUBLIC_REF,
            ),
            _official_slice(
                benchmark_id="drbench",
                version="arxiv-2510.00172v2",
                slice_id="drbench.full.harmonic-mean",
                label="DRBench FullBenchmark harmonic mean",
                interpretation=(
                    "Enterprise deep-research capability across all 100 tasks, "
                    "using the paper's harmonic mean of recall, factuality, "
                    "distractor avoidance, and report quality."
                ),
                dataset_ref=OFFICIAL_DRBENCH_FULL_REF,
            ),
        ]
    )
    return slices


# Admission records the exact configurations that completed the Mac O3
# Responses + forced-tool + continuation probes. This is compatibility evidence,
# not benchmark evidence, so it never silently clears a benchmark threshold.
def default_candidate_profiles() -> list[CandidateProfile]:
    return [
        CandidateProfile(
            candidate_id="codex-gpt-5.5-low",
            provider_id="codex",
            model="gpt-5.5",
            catalogue_model_id="codex/gpt-5.5",
            supported_reasoning_efforts=["low"],
            context_tokens=None,
            terminal=True,
            tools=True,
            vision=False,
            responses_api=True,
            monetary_cost_usd=None,
            cost_source="ChatGPT subscription; per-request monetary cost unavailable",
            quota_source="Codex subscription quota; live remaining quota may be unavailable",
            last_full_probe_at="2026-09-03T00:00:00+08:00",
            probe_reference=DEFAULT_CANDIDATE_PROBE_REFERENCE,
        ),
        CandidateProfile(
            candidate_id="mistral-small-latest-high",
            provider_id="mistral",
            model="mistral-small-latest",
            catalogue_model_id="mistral/mistral-small-latest",
            supported_reasoning_efforts=["high"],
            context_tokens=None,
            terminal=True,
            tools=True,
            vision=False,
            responses_api=True,
            monetary_cost_usd=None,
            cost_source="Connected Mistral route; current request price unavailable",
            quota_source="Provider API quota; live remaining quota unavailable",
            last_full_probe_at="2026-09-03T00:00:00+08:00",
            probe_reference=DEFAULT_CANDIDATE_PROBE_REFERENCE,
        ),
        CandidateProfile(
            candidate_id="opencode-big-pickle-low",
            provider_id="opencode",
            model="big-pickle",
            catalogue_model_id="opencode/big-pickle",
            supported_reasoning_efforts=["low"],
            context_tokens=None,
            terminal=True,
            tools=True,
            vision=False,
            responses_api=True,
            monetary_cost_usd=0,
            cost_source="OpenCode free route; no monetary request charge observed",
            quota_source="OpenCode shared/free quota; live remaining quota unavailable",
            last_full_probe_at="2026-09-03T00:00:00+08:00",
            probe_reference=DEFAULT_CANDIDATE_PROBE_REFERENCE,
        ),
    ]


class BenchmarkRegistry:
    """Immutable in-process view of approved slices, evidence, and candidates."""

    def __init__(
        self,
        *,
        slices: list[BenchmarkSlice] | None = None,
        evidence: list[BenchmarkEvidence] | None = None,
        candidates: list[CandidateProfile] | None = None,
    ) -> None:
        self.slices = slices if slices is not None else default_slices()
        self.evidence = evidence if evidence is not None else self._load_evidence()
        self.candidates = candidates if candidates is not None else default_candidate_profiles()
        self._slice_by_key = {
            (item.benchmark_id, item.version, item.slice_id): item for item in self.slices
        }
        if len(self._slice_by_key) != len(self.slices):
            raise ValueError("benchmark slice keys must be unique")
        ids = {item.candidate_id for item in self.candidates}
        if len(ids) != len(self.candidates):
            raise ValueError("candidate ids must be unique")

    @staticmethod
    def _load_evidence_file(path: Path) -> list[BenchmarkEvidence]:
        raw = json.loads(path.read_text())
        if isinstance(raw, list):
            return [BenchmarkEvidence.model_validate(item) for item in raw]
        if not isinstance(raw, dict) or raw.get("schema_version") != 1:
            raise ValueError(
                f"benchmark evidence file must contain a JSON list or compact v1 table: {path}"
            )
        defaults = raw.get("defaults")
        columns = raw.get("columns")
        rows = raw.get("rows")
        if not isinstance(defaults, dict):
            raise ValueError(f"compact benchmark evidence defaults must be an object: {path}")
        if not isinstance(columns, list) or not all(
            isinstance(item, str) and item for item in columns
        ):
            raise ValueError(f"compact benchmark evidence columns are invalid: {path}")
        if len(columns) != len(set(columns)) or any(item in defaults for item in columns):
            raise ValueError(f"compact benchmark evidence fields overlap or repeat: {path}")
        if not isinstance(rows, list):
            raise ValueError(f"compact benchmark evidence rows must be a list: {path}")

        evidence: list[BenchmarkEvidence] = []
        for index, row in enumerate(rows):
            if not isinstance(row, list) or len(row) != len(columns):
                raise ValueError(
                    f"compact benchmark evidence row {index} has the wrong width: {path}"
                )
            evidence.append(
                BenchmarkEvidence.model_validate(
                    {**defaults, **dict(zip(columns, row, strict=True))}
                )
            )
        return evidence

    @classmethod
    def _load_evidence(cls) -> list[BenchmarkEvidence]:
        configured = os.environ.get(BENCHMARK_REGISTRY_ENV)
        if not configured:
            return []
        path = Path(configured).expanduser().resolve()
        raw = json.loads(path.read_text())
        if isinstance(raw, list):
            return [BenchmarkEvidence.model_validate(item) for item in raw]
        if not isinstance(raw, dict):
            raise ValueError(
                f"{BENCHMARK_REGISTRY_ENV} must name a JSON list, compact v1 table, "
                "or registry manifest"
            )
        if raw.get("schema_version") == 1:
            return cls._load_evidence_file(path)
        if raw.get("schema_version") != 2:
            raise ValueError("benchmark registry manifest schema_version must be 2")
        evidence_files = raw.get("evidence_files")
        if not isinstance(evidence_files, list) or not evidence_files:
            raise ValueError("benchmark registry manifest requires evidence_files")

        root = path.parent
        evidence: list[BenchmarkEvidence] = []
        for relative in evidence_files:
            if not isinstance(relative, str) or not relative.strip():
                raise ValueError("benchmark registry evidence paths must be non-empty strings")
            source = (root / relative).resolve()
            if source.parent != root:
                raise ValueError("benchmark registry evidence files must stay beside the manifest")
            evidence.extend(cls._load_evidence_file(source))

        seen: set[tuple[str, ...]] = set()
        for item in evidence:
            key = (
                item.benchmark_id,
                item.benchmark_version,
                item.slice_id,
                item.model,
                item.harness,
                item.source_reference,
            )
            if key in seen:
                raise ValueError(f"duplicate benchmark evidence record: {key}")
            seen.add(key)
        return evidence

    def require_slice(self, requirement: BenchmarkRequirement) -> BenchmarkSlice:
        key = (requirement.benchmark_id, requirement.version, requirement.slice_id)
        try:
            return self._slice_by_key[key]
        except KeyError as exc:
            raise ValueError(
                "benchmark requirement is not present in the supplied registry: "
                f"{requirement.benchmark_id}/{requirement.version}/{requirement.slice_id}"
            ) from exc

    def evidence_for(
        self,
        requirement: BenchmarkRequirement,
        candidate: CandidateSnapshot,
        reasoning_effort: str,
    ) -> list[BenchmarkEvidence]:
        """Return same-model records, downgrading execution mismatches to proxy."""
        slice_def = self.require_slice(requirement)
        candidate_model = canonical_model_name(candidate.model)
        relevant: list[BenchmarkEvidence] = []
        for record in self.evidence:
            if record.benchmark_id != requirement.benchmark_id:
                continue
            if record.benchmark_version != requirement.version:
                continue
            if record.slice_id != requirement.slice_id:
                continue
            if canonical_model_name(record.model) != candidate_model:
                continue
            same_manifest = record.task_manifest_digest == slice_def.task_manifest_digest
            same_harness = record.harness == candidate.harness
            same_effort = record.reasoning_effort == reasoning_effort
            same_path = record.provider_path in {
                candidate.provider_id,
                candidate.catalogue_model_id,
            }
            if record.evidence_class is EvidenceClass.EXACT and not all(
                (same_manifest, same_harness, same_effort, same_path)
            ):
                relevant.append(record.model_copy(update={"evidence_class": EvidenceClass.PROXY}))
            else:
                relevant.append(record)
        order = {
            EvidenceClass.EXACT: 0,
            EvidenceClass.PROXY: 1,
            EvidenceClass.ADVISORY: 2,
            EvidenceClass.UNKNOWN: 3,
        }
        return sorted(relevant, key=lambda item: order[item.evidence_class])

    def global_frontier(self, requirement: BenchmarkRequirement) -> float | None:
        exact = [
            item.admission_score
            for item in self.evidence
            if item.benchmark_id == requirement.benchmark_id
            and item.benchmark_version == requirement.version
            and item.slice_id == requirement.slice_id
            and item.evidence_class is EvidenceClass.EXACT
        ]
        return max(exact) if exact else None
