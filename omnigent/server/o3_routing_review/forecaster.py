"""Auditable task-blind benchmark forecasts for stable canonical models."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from omnigent.process_logging import data_dir

from .adviser import _extract_text
from .models import BenchmarkEvidence, BenchmarkRequirement, CandidateSnapshot, EvidenceClass
from .omniroute import OmniRouteClient, OmniRouteError
from .registry import ADVISER_COMBO_NAME, BenchmarkRegistry, canonical_model_name

FORECASTER_VERSION = "o3-task-blind-score-forecaster-v1"


class ForecastOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    point_estimate: float = Field(ge=0, le=1)
    plausible_lower: float = Field(ge=0, le=1)
    plausible_upper: float = Field(ge=0, le=1)
    confidence: float = Field(ge=0, le=1)
    rationale: str


class ForecastCache:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or data_dir() / "o3-routing-review" / "forecast-cache-v1.json"

    def get(self, key: str) -> BenchmarkEvidence | None:
        if not self.path.exists():
            return None
        raw = json.loads(self.path.read_text())
        value = raw.get(key) if isinstance(raw, dict) else None
        return BenchmarkEvidence.model_validate(value) if isinstance(value, dict) else None

    def put(self, key: str, value: BenchmarkEvidence) -> None:
        raw: dict[str, object] = {}
        if self.path.exists():
            decoded = json.loads(self.path.read_text())
            if isinstance(decoded, dict):
                raw = decoded
        raw[key] = value.model_dump(mode="json")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n")
        temp.chmod(0o600)
        temp.replace(self.path)


class ModelScoreForecaster:
    def __init__(self, client: OmniRouteClient, cache: ForecastCache | None = None) -> None:
        self.client = client
        self.cache = cache or ForecastCache()

    async def forecast(
        self,
        registry: BenchmarkRegistry,
        requirement: BenchmarkRequirement,
        candidate: CandidateSnapshot,
    ) -> BenchmarkEvidence | None:
        identity = canonical_model_name(candidate.model)
        if identity.startswith("opencode/") or identity == "big-pickle":
            return None
        calibration = requirement.calibration_version or "unknown-calibration"
        key_material = "|".join(
            [
                identity,
                requirement.benchmark_id,
                requirement.version,
                requirement.slice_id,
                FORECASTER_VERSION,
                calibration,
            ]
        )
        cache_key = "sha256:" + hashlib.sha256(key_material.encode()).hexdigest()
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached
        anchors = sorted(
            item.admission_score
            for item in registry.evidence
            if item.benchmark_id == requirement.benchmark_id
            and item.benchmark_version == requirement.version
            and item.slice_id == requirement.slice_id
            and item.evidence_class is not EvidenceClass.FORECAST
        )
        if not anchors:
            return None
        prompt = json.dumps(
            {
                "canonical_model_identity": identity,
                "non_secret_catalogue_metadata": {
                    "provider": candidate.provider_id,
                    "model": candidate.model,
                    "context_tokens": candidate.context_tokens,
                    "supported_reasoning_efforts": candidate.supported_reasoning_efforts,
                },
                "benchmark": {
                    "id": requirement.benchmark_id,
                    "version": requirement.version,
                    "slice": requirement.slice_id,
                },
                "published_reference_admission_scores": anchors,
            },
            separators=(",", ":"),
        )
        schema = ForecastOutput.model_json_schema()
        body: dict[str, object] = {
            "model": ADVISER_COMBO_NAME,
            "input": [
                {
                    "role": "system",
                    "content": (
                        "Estimate benchmark competence for the named stable model only. "
                        "You cannot see the user task and must not choose a route. Return a "
                        "conservative plausible "
                        "range, not a statistical confidence interval and not a measured result. "
                        "Return JSON matching this schema exactly: "
                        + json.dumps(schema, separators=(",", ":"))
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "reasoning": {"effort": "low"},
            "store": False,
        }
        response = await self.client.create_response(body)
        try:
            decoded = ForecastOutput.model_validate_json(_extract_text(response.body))
        except Exception as exc:
            raise OmniRouteError("model score forecaster returned invalid JSON") from exc
        if not decoded.plausible_lower <= decoded.point_estimate <= decoded.plausible_upper:
            raise OmniRouteError("model score forecaster returned an invalid plausible range")
        now = datetime.now(timezone.utc).isoformat()
        result = BenchmarkEvidence(
            benchmark_id=requirement.benchmark_id,
            benchmark_version=requirement.version,
            slice_id=requirement.slice_id,
            task_manifest_digest=registry.require_slice(requirement).task_manifest_digest,
            harness="forecast-advisory",
            model=identity,
            provider_path=candidate.catalogue_model_id,
            reasoning_effort="unknown",
            point_score=decoded.point_estimate,
            confidence_lower=decoded.plausible_lower,
            confidence_upper=decoded.plausible_upper,
            plausible_lower=decoded.plausible_lower,
            plausible_upper=decoded.plausible_upper,
            confidence=decoded.confidence,
            number_of_tasks=1,
            number_of_attempts=1,
            evidence_class=EvidenceClass.FORECAST,
            source_type="llm_forecast",
            source_reference=f"local-forecast-cache:{cache_key}",
            evaluation_date=now[:10],
            forecaster_version=FORECASTER_VERSION,
            created_at=now,
            rationale=decoded.rationale,
        )
        self.cache.put(cache_key, result)
        return result
