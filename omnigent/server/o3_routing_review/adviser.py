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
    """Return requirement-only context; candidate evidence stays out of model context."""
    del candidates
    slices = [
        {
            "benchmark_id": item.benchmark_id,
            "version": item.version,
            "slice_id": item.slice_id,
            "label": item.label,
            "interpretation": item.interpretation,
            "official": item.official,
        }
        for item in registry.slices
    ]
    return json.dumps(
        {
            "unchanged_user_task": prompt,
            "workspace_summary": workspace_summary,
            "allowed_benchmark_slices": slices,
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
            "appears in allowed_benchmark_slices. Candidate identities, model scores, provider "
            "availability, cost, quota, and whether evidence is measured or estimated are "
            "deliberately withheld. Set the competence floor independently; never infer or "
            "recommend a provider or model. Keep risk separate from technical difficulty. Use "
            "minimum_context_tokens=0 when the task does not establish a defensible numeric "
            "need. Use evidence_policy=strict only when the task genuinely requires exact "
            "execution-configuration evidence; otherwise use provisional. Use only these exact "
            "categorical values: difficulty is low, medium, high, or frontier; risk is low, "
            "medium, or high; reasoning effort is low, medium, high, or xhigh; disposition is "
            "route, borderline, decompose, or defer."
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
