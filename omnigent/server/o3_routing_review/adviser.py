"""Schema-validated LLM adviser that proposes requirements, never model picks."""

from __future__ import annotations

import json
from typing import Protocol

from pydantic import ValidationError

from .models import AdviserAnalysis, CandidateSnapshot
from .omniroute import OmniRouteClient, OmniRouteError
from .registry import ADVISER_COMBO_NAME, BenchmarkRegistry


class RoutingAdviser(Protocol):
    async def analyse(
        self,
        *,
        prompt: str,
        workspace_summary: str,
        registry: BenchmarkRegistry,
        candidates: list[CandidateSnapshot],
    ) -> AdviserAnalysis: ...

    async def decompose(
        self,
        *,
        prompt: str,
        workspace_summary: str,
        registry: BenchmarkRegistry,
        candidates: list[CandidateSnapshot],
        prior: AdviserAnalysis,
        capability_gap: str,
    ) -> AdviserAnalysis: ...


def _json_payload(
    *,
    prompt: str,
    workspace_summary: str,
    registry: BenchmarkRegistry,
    candidates: list[CandidateSnapshot],
    decomposition_context: dict[str, object] | None = None,
) -> str:
    slices = [
        {
            "benchmark_id": item.benchmark_id,
            "version": item.version,
            "slice_id": item.slice_id,
            "label": item.label,
            "interpretation": item.interpretation,
            "manifest_task_count": len(item.task_ids),
        }
        for item in registry.slices
    ]
    evidence = [
        {
            "benchmark_id": item.benchmark_id,
            "version": item.benchmark_version,
            "slice_id": item.slice_id,
            "harness": item.harness,
            "model": item.model,
            "provider_path": item.provider_path,
            "reasoning_effort": item.reasoning_effort,
            "point_score": item.point_score,
            "admission_score": item.admission_score,
            "evidence_class": item.evidence_class,
            "source_reference": item.source_reference,
        }
        for item in registry.evidence
    ]
    live_candidates = [
        {
            "candidate_id": item.candidate_id,
            "capabilities": {
                "terminal": item.terminal,
                "tools": item.tools,
                "vision": item.vision,
                "context_tokens": item.context_tokens,
                "responses_api": item.responses_api,
                "supported_reasoning_efforts": item.supported_reasoning_efforts,
            },
            "live": {
                "provider_usable": item.provider_usable,
                "model_present": item.model_present,
                "quota_available": item.quota_available,
                "quota_remaining_percent": item.quota_remaining_percent,
                "quota_reset_at": item.quota_reset_at,
                "success_rate": item.recent_success_rate,
                "retry_rate": item.recent_retry_rate,
                "latency_ms": item.latency_ms,
            },
            "resource": {
                "monetary_cost_usd": item.monetary_cost_usd,
                "cost_source": item.cost_source,
                "quota_source": item.quota_source,
            },
        }
        for item in candidates
    ]
    return json.dumps(
        {
            "unchanged_user_task": prompt,
            "workspace_summary": workspace_summary,
            "allowed_benchmark_slices": slices,
            "benchmark_evidence": evidence,
            "live_source_pool": live_candidates,
            "decomposition_context": decomposition_context,
        },
        separators=(",", ":"),
    )


def _extract_text(body: dict[str, object]) -> str:
    direct = body.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct
    output = body.get("output")
    if isinstance(output, list):
        chunks: list[str] = []
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict):
                    continue
                text = part.get("text")
                if isinstance(text, str):
                    chunks.append(text)
        if chunks:
            return "".join(chunks)
    raise OmniRouteError("routing adviser response contained no output text")


def _decode_analysis(text: str) -> AdviserAnalysis:
    stripped = text.strip()
    if stripped.startswith("```"):
        first_newline = stripped.find("\n")
        last_fence = stripped.rfind("```")
        if first_newline >= 0 and last_fence > first_newline:
            stripped = stripped[first_newline + 1 : last_fence].strip()
    try:
        raw = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise OmniRouteError("routing adviser returned invalid JSON") from exc
    try:
        return AdviserAnalysis.model_validate(raw)
    except ValidationError as exc:
        raise OmniRouteError("routing adviser output failed schema validation") from exc


class OmniRouteRoutingAdviser:
    """Call the persisted cheap adviser Combo and validate its JSON schema."""

    def __init__(self, client: OmniRouteClient) -> None:
        self.client = client

    async def _call(self, payload: str, *, decomposition: bool) -> AdviserAnalysis:
        instruction = (
            "You are the O3 routing requirements adviser. Interpret the task and return only "
            "the supplied JSON schema. You may choose only a benchmark ID/version/slice that "
            "appears in allowed_benchmark_slices. Never select, recommend, or name a provider "
            "or model. Never invent benchmark scores, availability, context, price, or quota. "
            "Keep risk separate from technical difficulty. Use minimum_context_tokens=0 when "
            "the task does not establish a defensible numeric need. Proxy/advisory/unknown "
            "evidence requires evidence_policy=provisional and must never be called an exact "
            "benchmark pass. Use only these exact categorical values: difficulty is low, "
            "medium, high, or frontier; risk is low, medium, or high; reasoning effort is "
            "low, medium, high, or xhigh; disposition is route, borderline, decompose, or "
            "defer."
        )
        if decomposition:
            instruction += (
                " A monolithic route failed. Propose ordered subtasks only when splitting truly "
                "lowers competence requirements; retain a blocked integration/review subtask "
                "when global reasoning remains necessary."
            )
        schema = AdviserAnalysis.model_json_schema()
        base_body: dict[str, object] = {
            "model": ADVISER_COMBO_NAME,
            "input": [
                {"role": "system", "content": instruction},
                {"role": "user", "content": payload},
            ],
            "reasoning": {"effort": "low"},
            "store": False,
        }
        strict_body = {
            **base_body,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "o3_routing_analysis",
                    "strict": True,
                    "schema": schema,
                }
            },
        }
        try:
            response = await self.client.create_response(strict_body)
            return _decode_analysis(_extract_text(response.body))
        except OmniRouteError:
            # Some empirically Responses-compatible non-OpenAI adapters reject
            # text.format or return JSON that does not honor its vocabulary. A
            # single prompt-level repair retry includes the exact schema and is
            # still accepted only after the same local Pydantic validation.
            repair_body = {
                **base_body,
                "input": [
                    {
                        "role": "system",
                        "content": (
                            instruction
                            + " Return one JSON object matching this schema exactly: "
                            + json.dumps(schema, separators=(",", ":"))
                        ),
                    },
                    {"role": "user", "content": payload},
                ],
            }
            response = await self.client.create_response(repair_body)
            return _decode_analysis(_extract_text(response.body))

    async def analyse(
        self,
        *,
        prompt: str,
        workspace_summary: str,
        registry: BenchmarkRegistry,
        candidates: list[CandidateSnapshot],
    ) -> AdviserAnalysis:
        return await self._call(
            _json_payload(
                prompt=prompt,
                workspace_summary=workspace_summary,
                registry=registry,
                candidates=candidates,
            ),
            decomposition=False,
        )

    async def decompose(
        self,
        *,
        prompt: str,
        workspace_summary: str,
        registry: BenchmarkRegistry,
        candidates: list[CandidateSnapshot],
        prior: AdviserAnalysis,
        capability_gap: str,
    ) -> AdviserAnalysis:
        return await self._call(
            _json_payload(
                prompt=prompt,
                workspace_summary=workspace_summary,
                registry=registry,
                candidates=candidates,
                decomposition_context={
                    "prior_analysis": prior.model_dump(mode="json"),
                    "actual_capability_gap": capability_gap,
                },
            ),
            decomposition=True,
        )
