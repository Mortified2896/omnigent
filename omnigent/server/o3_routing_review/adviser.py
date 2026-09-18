"""Schema-validated LLM adviser that proposes requirements, never model picks."""

from __future__ import annotations

import copy
import json
import os
from typing import Protocol

from pydantic import ValidationError

from .models import (
    AdviserAnalysis,
    AdviserExchange,
    CandidateSnapshot,
    ResourceAdvice,
    ResourceSnapshot,
)
from .omniroute import OmniRouteClient, OmniRouteError, OmniRouteResponse
from .registry import ADVISER_COMBO_NAME, BenchmarkRegistry

ADVISER_MODEL_ENV = "OMNIGENT_O3_ADVISER_MODEL"


class RoutingAdviser(Protocol):
    async def analyse(
        self,
        *,
        prompt: str,
        workspace_summary: str,
        registry: BenchmarkRegistry,
        candidates: list[CandidateSnapshot],
        model: str | None = None,
        reasoning_effort: str | None = None,
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
    measured_keys = {
        (item.benchmark_id, item.benchmark_version, item.slice_id) for item in registry.evidence
    }
    available = [
        item
        for item in registry.slices
        if (item.benchmark_id, item.version, item.slice_id) in measured_keys
    ]
    # Development/test registries can deliberately omit evidence. Preserve their
    # declared slice universe rather than sending the adviser an empty allowlist.
    if not available:
        available = registry.slices
    slices = [
        {
            "benchmark_id": item.benchmark_id,
            "version": item.version,
            "slice_id": item.slice_id,
            "label": item.label,
            "interpretation": item.interpretation,
            "official": item.official,
        }
        for item in available
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
            if not isinstance(item, dict) or item.get("type") == "reasoning":
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


def _record_exchange(
    analysis: AdviserAnalysis,
    request: dict[str, object],
    response: OmniRouteResponse,
    attempt: int,
) -> AdviserAnalysis:
    headers = {key.lower(): value for key, value in response.headers.items()}
    summaries = []
    output = response.body.get("output")
    for item in output if isinstance(output, list) else []:
        if not isinstance(item, dict) or item.get("type") != "reasoning":
            continue
        summary = item.get("summary")
        for part in summary if isinstance(summary, list) else []:
            if isinstance(part, dict) and part.get("type") == "summary_text":
                if isinstance(part.get("text"), str):
                    summaries.append(part["text"])
    reasoning = request.get("reasoning")
    effort = reasoning.get("effort", "low") if isinstance(reasoning, dict) else "low"
    analysis._exchanges.append(
        AdviserExchange(
            requested_model=str(request["model"]),
            actual_model=headers.get("x-omniroute-model") or headers.get("x-model"),
            actual_provider=headers.get("x-omniroute-provider") or headers.get("x-provider"),
            reasoning_effort=str(effort),
            request=copy.deepcopy(request),
            explanation=analysis.rationale,
            reasoning_summary="\n".join(summaries) or None,
            attempt=attempt,
        )
    )
    return analysis


class OmniRouteRoutingAdviser:
    """Call the persisted cheap adviser Combo and validate its JSON schema."""

    def __init__(self, client: OmniRouteClient) -> None:
        self.client = client
        self.model = os.environ.get(ADVISER_MODEL_ENV, ADVISER_COMBO_NAME).strip()
        if not self.model:
            raise ValueError(f"{ADVISER_MODEL_ENV} must not be blank")
        self.actual_provider: str | None = None
        self.actual_model: str | None = None

    def _remember_attribution(self, headers: dict[str, str]) -> None:
        self.actual_provider = headers.get("x-omniroute-provider") or headers.get("x-provider")
        self.actual_model = headers.get("x-omniroute-model") or headers.get("x-model")

    async def _call(
        self,
        payload: str,
        *,
        decomposition: bool,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> AdviserAnalysis:
        instruction = (
            "You are the O3 routing requirements adviser. Interpret the task and return only "
            "the supplied JSON schema. You may choose only a benchmark ID/version/slice that "
            "appears in allowed_benchmark_slices. Candidate identities, model scores, provider "
            "availability, cost, quota, and benchmark scores are deliberately withheld. Choose "
            "one applicable benchmark and a categorical task difficulty; never infer or "
            "recommend a provider or model. Keep risk separate from technical difficulty. Use "
            "minimum_context_tokens=0 when the task does not establish a defensible numeric "
            "need. Explicitly set tools=true only when the task requires external tools, "
            "repository or shell access, or connected capabilities; use tools=false for "
            "self-contained text tasks. False means tools are not required, not forbidden. "
            "Image input means model vision; image output means model generation, not images "
            "returned by tools. Tool discovery is not internet search. "
            "Record required input/output modalities, structured output, and defensible "
            "input/output limits; terminal is a harness capability and must not be treated as "
            "a remote model requirement. Use evidence_policy=strict only when the task genuinely "
            "requires exact "
            "execution-configuration evidence; otherwise use provisional. Use only these exact "
            "categorical values: difficulty is easy, normal, moderate, hard, or frontier; "
            "risk is low, "
            "medium, or high; reasoning effort is low, medium, high, or xhigh; disposition is "
            "route, borderline, decompose, or defer. Difficulty definitions: easy means a routine "
            "task weaker competent models should handle; normal means ordinary professional work; "
            "moderate means meaningful reasoning or multi-step work; hard means a strong model is "
            "required; frontier means near the capability boundary of current leading models. "
            "Never emit or forecast a numeric benchmark floor."
        )
        if decomposition:
            instruction += (
                " A monolithic route failed. Propose ordered subtasks only when splitting truly "
                "lowers competence requirements; retain a blocked integration/review subtask "
                "when global reasoning remains necessary."
            )
        schema = AdviserAnalysis.model_json_schema()
        base_body: dict[str, object] = {
            "model": model or self.model,
            "input": [
                {"role": "system", "content": instruction},
                {"role": "user", "content": payload},
            ],
            "reasoning": {"effort": reasoning_effort or "low"},
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
            self._remember_attribution(response.headers)
            return _record_exchange(
                _decode_analysis(_extract_text(response.body)), strict_body, response, 1
            )
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
            self._remember_attribution(response.headers)
            return _record_exchange(
                _decode_analysis(_extract_text(response.body)), repair_body, response, 2
            )

    async def analyse(
        self,
        *,
        prompt: str,
        workspace_summary: str,
        registry: BenchmarkRegistry,
        candidates: list[CandidateSnapshot],
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> AdviserAnalysis:
        return await self._call(
            _json_payload(
                prompt=prompt,
                workspace_summary=workspace_summary,
                registry=registry,
                candidates=candidates,
            ),
            decomposition=False,
            model=model,
            reasoning_effort=reasoning_effort,
        )

    async def advise_resources(
        self,
        *,
        snapshot: ResourceSnapshot,
        model: str,
        reasoning_effort: str,
    ) -> ResourceAdvice:
        schema = ResourceAdvice.model_json_schema()
        body: dict[str, object] = {
            "model": model,
            "input": [
                {
                    "role": "system",
                    "content": (
                        "Return only the supplied JSON schema. Recommend exactly start_now, wait, "
                        "or ask_to_lower_floor from this aggregate operational snapshot. Missing "
                        "coverage is unknown. Do not claim quota suffices to finish and do not "
                        "invent reset times or change hard requirements. Set source=estimator."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(snapshot.model_dump(mode="json"), separators=(",", ":")),
                },
            ],
            "reasoning": {"effort": reasoning_effort},
            "store": False,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "o3_resource_advice",
                    "strict": True,
                    "schema": schema,
                }
            },
        }
        response = await self.client.create_response(body)
        self._remember_attribution(response.headers)
        try:
            advice = ResourceAdvice.model_validate(json.loads(_extract_text(response.body)))
        except (json.JSONDecodeError, ValidationError) as exc:
            raise OmniRouteError("resource adviser output failed schema validation") from exc
        if advice.action == "start_now" and snapshot.usable_routes == 0:
            raise OmniRouteError("resource adviser recommended start without a known usable route")
        return advice

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
