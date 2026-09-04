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


def manifest_digest(task_ids: Iterable[str]) -> str:
    """Hash a canonical ordered task-id manifest without inventing data."""
    payload = json.dumps(sorted(task_ids), separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()


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


def default_slices() -> list[BenchmarkSlice]:
    task_ids: list[str] = []
    digest = manifest_digest(task_ids)
    return [
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


# Admission records the exact configurations that completed the Mac O3
# Responses + forced-tool + continuation probes. This is compatibility evidence,
# not benchmark evidence, so it never silently clears a strict TB4 threshold.
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
    def _load_evidence() -> list[BenchmarkEvidence]:
        configured = os.environ.get(BENCHMARK_REGISTRY_ENV)
        if not configured:
            return []
        path = Path(configured).expanduser()
        raw = json.loads(path.read_text())
        if not isinstance(raw, list):
            raise ValueError(f"{BENCHMARK_REGISTRY_ENV} must name a JSON list")
        return [BenchmarkEvidence.model_validate(item) for item in raw]

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
        """Return relevant records ordered exact, proxy, advisory."""
        slice_def = self.require_slice(requirement)
        relevant: list[BenchmarkEvidence] = []
        for record in self.evidence:
            if record.benchmark_id != requirement.benchmark_id:
                continue
            if record.slice_id != requirement.slice_id:
                continue
            same_version = record.benchmark_version == requirement.version
            same_manifest = record.task_manifest_digest == slice_def.task_manifest_digest
            same_harness = record.harness == candidate.harness
            same_model = record.model in {candidate.model, candidate.catalogue_model_id}
            same_effort = record.reasoning_effort == reasoning_effort
            same_path = record.provider_path in {
                None,
                candidate.provider_id,
                candidate.catalogue_model_id,
            }
            if record.evidence_class is EvidenceClass.EXACT and not all(
                (same_version, same_manifest, same_harness, same_model, same_effort, same_path)
            ):
                # An artifact cannot self-declare exactness for a different
                # execution configuration; keep it as proxy evidence only when
                # the underlying model is still related.
                if same_model:
                    relevant.append(
                        record.model_copy(update={"evidence_class": EvidenceClass.PROXY})
                    )
                continue
            if same_model or record.evidence_class is EvidenceClass.ADVISORY:
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
