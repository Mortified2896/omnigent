"""Coverage for the multi-benchmark O3 registry and adviser isolation."""

from __future__ import annotations

import json

from omnigent.server.o3_routing_review.adviser import _json_payload
from omnigent.server.o3_routing_review.models import (
    BenchmarkEvidence,
    CandidateSnapshot,
    EvidenceClass,
)
from omnigent.server.o3_routing_review.registry import (
    BENCHMARK_REGISTRY_ENV,
    BenchmarkRegistry,
    default_slices,
)

_OFFICIAL_KEYS = {
    ("terminal-bench", "4.0.0", "tb4.overall"),
    (
        "swe-bench-pro",
        "public-leaderboard-2026-09-04",
        "swe-bench-pro.public.resolve-rate",
    ),
    ("drbench", "arxiv-2510.00172v2", "drbench.full.harmonic-mean"),
}


def _candidate() -> CandidateSnapshot:
    return CandidateSnapshot(
        candidate_id="openrouter-gpt-5-low",
        provider_id="openrouter",
        model="gpt-5",
        catalogue_model_id="openrouter/gpt-5",
        supported_reasoning_efforts=["low"],
        context_tokens=128_000,
        terminal=True,
        tools=True,
        vision=False,
        responses_api=True,
        monetary_cost_usd=0,
        cost_source="test",
        quota_source="test",
        last_full_probe_at="2026-09-04T00:00:00+08:00",
        probe_reference="test",
        provider_usable=True,
        model_present=True,
        quota_available=True,
        quota_remaining_percent=100,
        quota_reset_at=None,
        recent_success_rate=1,
        recent_retry_rate=0,
        latency_ms=100,
    )


def _evidence(
    benchmark_id: str,
    version: str,
    slice_id: str,
    *,
    model: str,
    harness: str,
    score: float,
) -> BenchmarkEvidence:
    slice_def = next(
        item
        for item in default_slices()
        if (item.benchmark_id, item.version, item.slice_id)
        == (benchmark_id, version, slice_id)
    )
    return BenchmarkEvidence(
        benchmark_id=benchmark_id,
        benchmark_version=version,
        slice_id=slice_id,
        task_manifest_digest=slice_def.task_manifest_digest,
        harness=harness,
        harness_version="published",
        model=model,
        provider_path=model.split("/", 1)[0],
        reasoning_effort="not_reported",
        point_score=score,
        confidence_lower=None,
        confidence_upper=None,
        number_of_tasks=100,
        number_of_attempts=100,
        evidence_class=EvidenceClass.EXACT,
        source_type="official_test_fixture",
        source_reference=f"https://example.test/{benchmark_id}/{model}",
        evaluation_date="2026-09-04",
    )


def test_default_slices_register_all_three_official_benchmarks() -> None:
    slices = default_slices()
    official = {
        (item.benchmark_id, item.version, item.slice_id)
        for item in slices
        if item.official
    }

    assert official == _OFFICIAL_KEYS
    assert all(item.task_ids for item in slices if item.official)
    assert all(item.task_manifest_digest.startswith("sha256:") for item in slices)


def test_adviser_receives_only_measured_benchmark_definitions() -> None:
    evidence = [
        _evidence(
            "terminal-bench",
            "4.0.0",
            "tb4.overall",
            model="openai/gpt-5.6-sol",
            harness="codex",
            score=0.37,
        ),
        _evidence(
            "swe-bench-pro",
            "public-leaderboard-2026-09-04",
            "swe-bench-pro.public.resolve-rate",
            model="openai/gpt-5.4",
            harness="mini-swe-agent",
            score=0.59,
        ),
        _evidence(
            "drbench",
            "arxiv-2510.00172v2",
            "drbench.full.harmonic-mean",
            model="openai/gpt-5",
            harness="drbench-agent-base",
            score=0.64,
        ),
    ]
    registry = BenchmarkRegistry(
        slices=default_slices(),
        evidence=evidence,
        candidates=[],
    )
    payload = json.loads(
        _json_payload(
            prompt="Investigate and fix the repository issue",
            workspace_summary="Workspace: /tmp/repo",
            registry=registry,
            candidates=[_candidate()],
        )
    )

    offered = {
        (item["benchmark_id"], item["version"], item["slice_id"])
        for item in payload["allowed_benchmark_slices"]
    }
    assert offered == _OFFICIAL_KEYS

    serialized = json.dumps(payload)
    for forbidden in (
        "gpt-5.6-sol",
        "gpt-5.4",
        "openai/gpt-5",
        "codex",
        "mini-swe-agent",
        "drbench-agent-base",
        "0.37",
        "0.59",
        "0.64",
        "source_reference",
        "provider_usable",
        "monetary_cost_usd",
    ):
        assert forbidden not in serialized


def test_placeholder_control_room_slices_stay_out_of_measured_allowlist() -> None:
    evidence = [
        _evidence(
            "drbench",
            "arxiv-2510.00172v2",
            "drbench.full.harmonic-mean",
            model="openai/gpt-5",
            harness="drbench-agent-base",
            score=0.64,
        )
    ]
    registry = BenchmarkRegistry(
        slices=default_slices(),
        evidence=evidence,
        candidates=[],
    )
    payload = json.loads(
        _json_payload(
            prompt="Prepare an enterprise research report",
            workspace_summary="",
            registry=registry,
            candidates=[],
        )
    )

    assert [
        item["slice_id"] for item in payload["allowed_benchmark_slices"]
    ] == ["drbench.full.harmonic-mean"]


def test_registry_manifest_loads_all_evidence_files(
    tmp_path, monkeypatch
) -> None:
    slices = {
        (item.benchmark_id, item.version, item.slice_id): item
        for item in default_slices()
        if item.official
    }
    records = [
        _evidence(
            benchmark_id,
            version,
            slice_id,
            model=f"publisher/model-{index}",
            harness="published-agent",
            score=0.2 + index / 10,
        ).model_dump(mode="json")
        for index, (benchmark_id, version, slice_id) in enumerate(
            sorted(_OFFICIAL_KEYS),
            start=1,
        )
    ]
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first.write_text(json.dumps(records[:2]))
    second.write_text(json.dumps(records[2:]))

    manifest = tmp_path / "registry.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "evidence_files": [first.name, second.name],
            }
        )
    )
    monkeypatch.setenv(BENCHMARK_REGISTRY_ENV, str(manifest))

    registry = BenchmarkRegistry(candidates=[])

    assert len(registry.evidence) == 3
    assert {
        (item.benchmark_id, item.benchmark_version, item.slice_id)
        for item in registry.evidence
    } == set(slices)


def test_registry_manifest_rejects_path_traversal(tmp_path, monkeypatch) -> None:
    outside = tmp_path.parent / "outside-evidence.json"
    outside.write_text("[]")
    manifest = tmp_path / "registry.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "evidence_files": [f"../{outside.name}"],
            }
        )
    )
    monkeypatch.setenv(BENCHMARK_REGISTRY_ENV, str(manifest))

    try:
        BenchmarkRegistry(candidates=[])
    except ValueError as exc:
        assert "stay beside the manifest" in str(exc)
    else:
        raise AssertionError("path traversal must be rejected")


def test_compact_evidence_table_expands_to_model_records(
    tmp_path, monkeypatch
) -> None:
    slice_def = next(
        item
        for item in default_slices()
        if item.slice_id == "swe-bench-pro.public.resolve-rate"
    )
    compact = tmp_path / "swe.json"
    compact.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "defaults": {
                    "benchmark_id": "swe-bench-pro",
                    "benchmark_version": "public-leaderboard-2026-09-04",
                    "slice_id": "swe-bench-pro.public.resolve-rate",
                    "task_manifest_digest": slice_def.task_manifest_digest,
                    "harness_version": None,
                    "number_of_tasks": 731,
                    "number_of_attempts": 731,
                    "evidence_class": "exact",
                    "source_type": "official_leaderboard",
                    "source_reference": "https://example.test/swe",
                    "evaluation_date": "not_published",
                },
                "columns": [
                    "model",
                    "provider_path",
                    "harness",
                    "reasoning_effort",
                    "point_score",
                    "confidence_lower",
                    "confidence_upper",
                ],
                "rows": [
                    [
                        "openai/gpt-5.4",
                        "openai",
                        "mini-swe-agent",
                        "xhigh",
                        0.591,
                        0.5554,
                        0.6266,
                    ]
                ],
            }
        )
    )
    monkeypatch.setenv(BENCHMARK_REGISTRY_ENV, str(compact))

    registry = BenchmarkRegistry(candidates=[])

    assert len(registry.evidence) == 1
    record = registry.evidence[0]
    assert record.model == "openai/gpt-5.4"
    assert record.admission_score == 0.5554
    assert record.number_of_tasks == 731
