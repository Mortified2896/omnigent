"""Supported-HTTP-only OmniRoute adapter for O3 routing review."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx

from .models import (
    CandidateEvaluation,
    CandidateProfile,
    CandidateSnapshot,
    ExecutionProvenance,
    ExecutionTokenUsage,
)
from .registry import SOURCE_POOL_NAME

OMNIROUTE_BASE_URL_ENV = "OMNIGENT_O3_OMNIROUTE_BASE_URL"
OMNIROUTE_KEY_ENV = "OMNIROUTE_O3_KEY"
DERIVED_COMBO_PREFIX = "custom/o3-route-"
_DERIVED_NAME_RE = re.compile(r"^custom/o3-route-[a-f0-9]{8,20}$")
_CALL_LOG_PAGE_SIZE = 100
_MAX_CALL_LOG_PAGES = 20
_MAX_EXECUTION_RECORDS = 100
_CALL_LOG_CLOCK_SKEW = timedelta(minutes=5)


def _target_pair(provider_id: str, model: str) -> tuple[str, str]:
    """Return the semantic pair behind OmniRoute's qualified model read-back."""
    prefix = f"{provider_id}/"
    return provider_id, model[len(prefix) :] if model.startswith(prefix) else model


class OmniRouteError(RuntimeError):
    """Sanitized error at the OmniRoute boundary."""


@dataclass(frozen=True)
class OmniRouteResponse:
    body: dict[str, object]
    headers: dict[str, str]


class OmniRouteClient:
    """Narrow client for models, Responses, and persisted Combo APIs."""

    def __init__(self, base_url: str, api_key: str, *, timeout: float = 60.0) -> None:
        if not base_url.startswith(("http://127.0.0.1:", "http://localhost:")):
            raise ValueError("O3 OmniRoute must use a loopback HTTP endpoint")
        if not api_key.strip():
            raise ValueError("an OmniRoute O3 credential is required")
        self.base_url = base_url.rstrip("/")
        self._authorization = (
            api_key.strip()
            if api_key.strip().lower().startswith("bearer ")
            else f"Bearer {api_key}"
        )
        self.timeout = timeout

    @classmethod
    def from_env(cls) -> OmniRouteClient:
        base_url = os.environ.get(OMNIROUTE_BASE_URL_ENV, "http://127.0.0.1:20128")
        key = os.environ.get(OMNIROUTE_KEY_ENV)
        if key is None:
            raise ValueError(f"{OMNIROUTE_KEY_ENV} is required while O3 routing review is enabled")
        return cls(base_url, key)

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, object] | None = None,
        timeout: float | None = None,
    ) -> tuple[object, dict[str, str]]:
        headers = {"Authorization": self._authorization, "Accept": "application/json"}
        async with httpx.AsyncClient(timeout=timeout or self.timeout) as client:
            response = await client.request(
                method,
                self.base_url + path,
                headers=headers,
                json=body,
            )
        if response.status_code < 200 or response.status_code >= 300:
            try:
                payload = response.json()
                detail = json.dumps(payload, sort_keys=True)[:500]
            except ValueError:
                detail = response.text[:500]
            raise OmniRouteError(
                f"OmniRoute {method} {path} returned {response.status_code}: {detail}"
            )
        try:
            decoded = response.json()
        except ValueError as exc:
            raise OmniRouteError(f"OmniRoute {method} {path} returned non-JSON") from exc
        return decoded, {key.lower(): value for key, value in response.headers.items()}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, object] | None = None,
        timeout: float | None = None,
    ) -> OmniRouteResponse:
        decoded, headers = await self._request_json(
            method,
            path,
            body=body,
            timeout=timeout,
        )
        if not isinstance(decoded, dict):
            raise OmniRouteError(f"OmniRoute {method} {path} returned an unexpected JSON shape")
        return OmniRouteResponse(body=decoded, headers=headers)

    async def list_combos(self) -> list[dict[str, object]]:
        response = await self._request("GET", "/api/combos")
        raw = response.body.get("combos")
        if not isinstance(raw, list):
            raise OmniRouteError("GET /api/combos omitted the combos list")
        return [item for item in raw if isinstance(item, dict)]

    async def get_combo(self, name: str) -> dict[str, object] | None:
        matches = [item for item in await self.list_combos() if item.get("name") == name]
        if len(matches) > 1:
            raise OmniRouteError(f"multiple persisted Combos are named {name!r}")
        return matches[0] if matches else None

    async def model_ids(self) -> set[str]:
        response = await self._request("GET", "/v1/models")
        raw = response.body.get("data")
        if not isinstance(raw, list):
            raise OmniRouteError("GET /v1/models omitted the data list")
        return {
            str(item["id"])
            for item in raw
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }

    async def live_candidates(self, profiles: list[CandidateProfile]) -> list[CandidateSnapshot]:
        """Cross-check every profile against the persisted source pool and catalogue."""
        pool = await self.get_combo(SOURCE_POOL_NAME)
        if pool is None:
            raise OmniRouteError(f"required source pool {SOURCE_POOL_NAME!r} is not persisted")
        raw_models = pool.get("models")
        if not isinstance(raw_models, list):
            raise OmniRouteError(f"source pool {SOURCE_POOL_NAME!r} has no explicit models list")
        pool_pairs = {
            _target_pair(str(item.get("providerId")), str(item.get("model")))
            for item in raw_models
            if isinstance(item, dict)
            and isinstance(item.get("providerId"), str)
            and isinstance(item.get("model"), str)
        }
        profile_pairs = {(item.provider_id, item.model) for item in profiles}
        if pool_pairs != profile_pairs:
            missing = sorted(pool_pairs - profile_pairs)
            extra = sorted(profile_pairs - pool_pairs)
            raise OmniRouteError(
                "source-pool/profile mismatch; every source-pool target must be evaluated "
                f"(unprofiled={missing}, absent_from_pool={extra})"
            )
        ids = await self.model_ids()
        return [
            CandidateSnapshot(
                **profile.model_dump(),
                provider_usable=profile.catalogue_model_id in ids,
                model_present=profile.catalogue_model_id in ids,
                quota_available=None,
                quota_remaining_percent=None,
                quota_reset_at=None,
                recent_success_rate=None,
                recent_retry_rate=None,
                latency_ms=None,
            )
            for profile in profiles
        ]

    async def create_derived_combo(
        self,
        proposal_id: str,
        evaluations: list[CandidateEvaluation],
        *,
        reasoning_effort: str,
    ) -> tuple[str, dict[str, object]]:
        short_id = proposal_id.replace("-", "")[:12].lower()
        name = DERIVED_COMBO_PREFIX + short_id
        if not _DERIVED_NAME_RE.fullmatch(name):
            raise OmniRouteError("proposal id cannot produce a safe derived Combo name")
        pool = await self.get_combo(SOURCE_POOL_NAME)
        if pool is None:
            raise OmniRouteError(f"source pool {SOURCE_POOL_NAME!r} disappeared")
        raw_models = pool.get("models")
        if not isinstance(raw_models, list):
            raise OmniRouteError("source pool has no explicit models list")
        pool_models = [item for item in raw_models if isinstance(item, dict)]
        pool_by_pair = {
            _target_pair(str(item.get("providerId")), str(item.get("model"))): item
            for item in pool_models
        }
        requested_pairs = {
            (item.candidate.provider_id, item.candidate.model) for item in evaluations
        }
        if not requested_pairs:
            raise OmniRouteError("cannot create an empty derived Combo")
        if not requested_pairs < set(pool_by_pair):
            raise OmniRouteError("derived Combo targets must be a strict source-pool subset")
        models: list[dict[str, object]] = []
        for index, pair in enumerate(sorted(requested_pairs), start=1):
            source = pool_by_pair[pair]
            model = {
                key: value
                for key, value in source.items()
                if key in {"kind", "providerId", "model", "connectionId", "label", "weight"}
            }
            model.update(
                {
                    "id": f"o3-route-{short_id}-{index}",
                    "kind": "model",
                    "providerId": pair[0],
                    "model": pair[1],
                    "weight": 0,
                }
            )
            models.append(model)
        body: dict[str, object] = {
            "name": name,
            "displayName": f"O3 approved route {short_id}",
            "type": "auto",
            "strategy": "auto",
            "description": (
                f"Ephemeral O3 route owned by proposal {proposal_id}; approved effort "
                f"{reasoning_effort}. Targets are a strict subset of {SOURCE_POOL_NAME}."
            ),
            "explorationRate": 0,
            "fallbackEnabled": True,
            "useLkgp": False,
            "models": models,
            "config": {
                "auto": {
                    "routerStrategy": "rules",
                    "explorationRate": 0,
                    "candidatePool": sorted({pair[0] for pair in requested_pairs}),
                    "weights": {
                        "taskFit": 0.0,
                        "health": 0.30,
                        "stability": 0.20,
                        "quota": 0.20,
                        "costInv": 0.15,
                        "latencyInv": 0.10,
                        "tierPriority": 0.0,
                        "tierAffinity": 0.0,
                        "specificityMatch": 0.0,
                        "contextAffinity": 0.0,
                        "cacheAffinity": 0.0,
                        "sessionAvailability": 0.05,
                        "resetWindowAffinity": 0.0,
                    },
                }
            },
        }
        existing = await self.get_combo(name)
        if existing is not None:
            if self._combo_matches(existing, body):
                return name, body
            raise OmniRouteError(f"refusing to overwrite non-identical existing Combo {name!r}")
        await self._request("POST", "/api/combos", body=body)
        created = await self.get_combo(name)
        if created is None or not self._combo_matches(created, body):
            raise OmniRouteError(
                f"derived Combo {name!r} did not persist with the requested targets"
            )
        return name, body

    @staticmethod
    def _combo_matches(existing: dict[str, object], wanted: dict[str, object]) -> bool:
        def pairs(value: object) -> list[tuple[str, str]]:
            if not isinstance(value, list):
                return []
            return sorted(
                _target_pair(str(item.get("providerId")), str(item.get("model")))
                for item in value
                if isinstance(item, dict)
            )

        existing_auto = existing.get("config")
        wanted_auto = wanted.get("config")
        return (
            existing.get("name") == wanted.get("name")
            and existing.get("strategy") == "auto"
            and pairs(existing.get("models")) == pairs(wanted.get("models"))
            and isinstance(existing_auto, dict)
            and isinstance(wanted_auto, dict)
            and existing_auto.get("auto") == wanted_auto.get("auto")
        )

    async def delete_derived_combo(self, name: str) -> bool:
        if not _DERIVED_NAME_RE.fullmatch(name):
            raise OmniRouteError(f"refusing to delete Combo outside {DERIVED_COMBO_PREFIX!r}")
        combo = await self.get_combo(name)
        if combo is None:
            return False
        combo_id = combo.get("id")
        if not isinstance(combo_id, str) or not combo_id:
            raise OmniRouteError(f"derived Combo {name!r} has no deletable id")
        await self._request("DELETE", f"/api/combos/{combo_id}")
        return True

    async def list_call_logs(self, *, limit: int, offset: int) -> list[dict[str, object]]:
        """Return one call-log page without exposing request or response bodies."""
        decoded, _headers = await self._request_json(
            "GET",
            f"/api/usage/call-logs?limit={limit}&offset={offset}",
        )
        if not isinstance(decoded, list):
            raise OmniRouteError("GET /api/usage/call-logs returned an unexpected JSON shape")
        return [item for item in decoded if isinstance(item, dict)]

    async def get_call_log(self, call_log_id: str) -> dict[str, object]:
        decoded, _headers = await self._request_json(
            "GET",
            f"/api/usage/call-logs/{quote(call_log_id, safe='')}",
        )
        if not isinstance(decoded, dict):
            raise OmniRouteError("GET /api/usage/call-logs/{id} returned an unexpected JSON shape")
        return decoded

    async def execution_provenance_for_session(
        self,
        *,
        derived_combo_name: str,
        proposal_created_at: datetime,
    ) -> list[ExecutionProvenance]:
        """Match and sanitize Responses call logs owned by one approved proposal.

        OmniRoute derives ``sessionTag`` from the Responses conversation, so a
        Codex tool round trip can legitimately use multiple opaque conversation
        tags. The per-proposal derived Combo is the stable ownership boundary:
        its name is unique, namespace-restricted, and linked to only one
        Omnigent session.
        """
        cutoff = proposal_created_at.astimezone(timezone.utc) - _CALL_LOG_CLOCK_SKEW
        matched: dict[str, tuple[dict[str, object], datetime]] = {}
        for page_index in range(_MAX_CALL_LOG_PAGES):
            rows = await self.list_call_logs(
                limit=_CALL_LOG_PAGE_SIZE,
                offset=page_index * _CALL_LOG_PAGE_SIZE,
            )
            if not rows:
                break
            page_timestamps: list[datetime] = []
            for row in rows:
                timestamp = self._parse_timestamp(row.get("timestamp"))
                if timestamp is not None:
                    page_timestamps.append(timestamp)
                if timestamp is None or timestamp < cutoff:
                    continue
                if row.get("path") != "/v1/responses" or row.get("method") != "POST":
                    continue
                if derived_combo_name not in {row.get("comboName"), row.get("requestedModel")}:
                    continue
                call_log_id = row.get("id")
                if isinstance(call_log_id, str) and call_log_id:
                    matched[call_log_id] = (row, timestamp)
            if len(rows) < _CALL_LOG_PAGE_SIZE:
                break
            if page_timestamps and min(page_timestamps) < cutoff:
                break

        ordered = sorted(matched.values(), key=lambda value: (value[1], str(value[0]["id"])))
        ordered = ordered[-_MAX_EXECUTION_RECORDS:]
        provenance: list[ExecutionProvenance] = []
        for row, timestamp in ordered:
            call_log_id = str(row["id"])
            try:
                detail = await self.get_call_log(call_log_id)
            except OmniRouteError:
                detail = {}
            record = self._sanitize_execution(row, detail, timestamp=timestamp)
            if record is not None:
                provenance.append(record)
        return provenance

    @staticmethod
    def _parse_timestamp(value: object) -> datetime | None:
        if not isinstance(value, str) or not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _nonnegative_int(value: object) -> int | None:
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
        return None

    @staticmethod
    def _nonnegative_float(value: object) -> float | None:
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
            return float(value)
        return None

    @classmethod
    def _sanitize_execution(
        cls,
        row: dict[str, object],
        detail: dict[str, object],
        *,
        timestamp: datetime,
    ) -> ExecutionProvenance | None:
        def required_string(source: str) -> str | None:
            value = row.get(source)
            return value if isinstance(value, str) and value else None

        call_log_id = required_string("id")
        path = required_string("path")
        method = required_string("method")
        session_tag = required_string("sessionTag")
        provider = required_string("provider")
        model = required_string("model")
        if (
            call_log_id is None
            or path is None
            or method is None
            or session_tag is None
            or provider is None
            or model is None
        ):
            return None
        status = cls._nonnegative_int(row.get("status"))
        if status is None:
            return None
        raw_tokens = row.get("tokens")
        tokens = raw_tokens if isinstance(raw_tokens, dict) else {}
        request_body = detail.get("requestBody")
        reasoning = request_body.get("reasoning") if isinstance(request_body, dict) else None
        effort = reasoning.get("effort") if isinstance(reasoning, dict) else None
        cost = cls._nonnegative_float(detail.get("costUsd"))
        if cost is None:
            cost = cls._nonnegative_float(row.get("costUsd"))

        def optional_string(source: str) -> str | None:
            value = row.get(source)
            return value if isinstance(value, str) and value else None

        return ExecutionProvenance(
            call_log_id=call_log_id,
            path=path,
            method=method,
            session_tag=session_tag,
            provider=provider,
            model=model,
            timestamp=timestamp,
            combo_name=optional_string("comboName"),
            requested_model=optional_string("requestedModel"),
            connection_id=optional_string("connectionId"),
            correlation_id=optional_string("correlationId"),
            http_status=status,
            duration_ms=cls._nonnegative_float(row.get("duration")),
            token_usage=ExecutionTokenUsage(
                input_tokens=cls._nonnegative_int(tokens.get("in")),
                output_tokens=cls._nonnegative_int(tokens.get("out")),
                reasoning_tokens=cls._nonnegative_int(tokens.get("reasoning")),
                cache_read_tokens=cls._nonnegative_int(tokens.get("cacheRead")),
                cache_write_tokens=cls._nonnegative_int(tokens.get("cacheWrite")),
            ),
            reasoning_effort=effort if isinstance(effort, str) and effort else None,
            estimated_cost_usd=cost,
        )

    async def create_response(self, body: dict[str, object]) -> OmniRouteResponse:
        return await self._request("POST", "/v1/responses", body=body, timeout=120.0)
