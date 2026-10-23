"""On-demand, bounded Responses probes over exact provider routes."""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import time
import uuid
from collections import Counter
from datetime import datetime
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.o3_routing_review.omniroute import OmniRouteClient, OmniRouteError

from .availability_store import ProbeStore
from .service import benchmarks, enabled, rate, readiness

TTL = 86400
OUTPUT_LIMIT = 256


class ProbePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    cost_mode: Literal["free", "included", "metered"] = "included"
    minimum_score: float = Field(default=0, ge=0, le=100)
    include_unscored: bool = True
    max_input_usd_per_million: float = Field(default=1, ge=0, le=1000)
    max_output_usd_per_million: float = Field(default=5, ge=0, le=10000)
    estimated_budget_usd: float = Field(default=1, gt=0, le=10)
    retest: bool = False


def timestamp(value) -> float:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0


def provider_policy(connections: list[dict], evidence: dict, quota_cache=None, plans=None) -> dict:
    """Allowlisted policy projection. Limits are configuration, never remaining credits."""
    result = {}
    for c in connections:
        name = c["provider"]
        p = result.setdefault(
            name,
            {
                "provider": name,
                "enabled_accounts": 0,
                "disabled_accounts": 0,
                "limits": [],
                "quota": [],
                "blocked": False,
                "policy_limits": [],
                "usage": [],
            },
        )
        if not c.get("isActive"):
            p["disabled_accounts"] += 1
            continue
        p["enabled_accounts"] += 1
        cached = (quota_cache or {}).get(c.get("id"), {})
        observed = timestamp(cached.get("fetchedAt"))
        for window, usage in (cached.get("quotas") or {}).items():
            remaining, total = rate(usage.get("remaining")), rate(usage.get("total"))
            if remaining is not None and total is not None and total >= 0.000001:
                p["usage"].append(
                    {
                        "window": str(window)[:80],
                        "remaining": remaining,
                        "total": total,
                        "account": p["enabled_accounts"],
                        "observed_at": observed,
                        "stale": not 0 <= time.time() - observed <= 7200,
                        "source": str(cached.get("source") or "gateway cache")[:80],
                        "reset_at": timestamp(usage.get("resetAt")) or None,
                    }
                )
        for plan_entry in plans or []:
            if plan_entry.get("provider") != name or plan_entry.get("connectionId") not in (
                None,
                c.get("id"),
            ):
                continue
            for dimension in plan_entry.get("dimensions", []):
                limit = rate(dimension.get("limit"))
                # Number.EPSILON denotes an unknown automatically inferred plan limit.
                if limit is not None and limit >= 0.000001:
                    value = {
                        "unit": str(dimension.get("unit"))[:40],
                        "window": str(dimension.get("window"))[:40],
                        "limit": limit,
                        "source": str(plan_entry.get("source") or "gateway policy")[:80],
                    }
                    if value not in p["policy_limits"]:
                        p["policy_limits"].append(value)
        data = c.get("providerSpecificData") or {}
        key_health = (data.get("apiKeyHealth") or {}).get("primary") or {}
        # An invalid enabled account can still be selected by the gateway.
        if key_health.get("status") == "invalid" or rate(c.get("errorCode")) in (401, 403):
            p["blocked"] = True
        if timestamp(c.get("rateLimitedUntil")) > time.time():
            p["blocked"] = True
        limits = {}
        for k in ("maxConcurrent", "healthCheckInterval"):
            if rate(c.get(k)) is not None:
                limits[k] = c[k]
        for k in (
            "requestsPerMinute",
            "tokensPerMinute",
            "requestsPerDay",
            "minTime",
            "maxConcurrent",
        ):
            v = (c.get("rateLimitOverrides") or {}).get(k)
            if rate(v) is not None:
                limits[k] = v
        if limits and limits not in p["limits"]:
            p["limits"].append(limits)
        for scope, state in (data.get("codexQuotaStateByScope") or {}).items():
            observed = timestamp(state.get("observedAt"))
            for window in ("5h", "Weekly"):
                used, limit = rate(state.get("usage" + window)), rate(state.get("limit" + window))
                if used is not None and limit and used <= limit:
                    p["quota"].append(
                        {
                            "account": p["enabled_accounts"],
                            "scope": str(scope),
                            "window": window,
                            "remaining_percent": round(100 * (1 - used / limit), 1),
                            "observed_at": observed,
                            "stale": not 0 <= time.time() - observed <= 1200,
                            "reset_at": timestamp(state.get("resetAt" + window)) or None,
                        }
                    )
    for name, p in result.items():
        p["account_ready"] = (
            bool(evidence.get("providers", {}).get(name, {}).get("active")) and not p["blocked"]
        )
    return result


def inventory(
    catalog: list[dict],
    connections: list[dict],
    evidence: dict,
    registry: dict,
    results: dict,
    quota_cache=None,
    plans=None,
) -> dict:
    policies = provider_policy(connections, evidence, quota_cache, plans)
    by_model = {}
    for b in registry["models"]:
        if "fallback" not in b["configuration"].casefold():
            by_model.setdefault(b["model"].casefold(), []).append(b)
    rows, seen = [], set()
    for item in catalog:
        route = item.get("id", "")
        provider, sep, model = route.partition("/")
        if (
            not sep
            or provider != item.get("owned_by")
            or provider not in policies
            or route in seen
        ):
            continue
        seen.add(route)
        capabilities = item.get("capabilities") or {}
        outputs = item.get("output_modalities") or ["text"]
        if "text" not in outputs or capabilities.get("embedding") is True:
            continue
        tiers = capabilities.get("effort_tiers") or item.get("effort_tiers") or []
        records = by_model.get(model.rsplit("/", 1)[-1].casefold().removesuffix(":free"), [])
        matching = [
            r
            for r in records
            if (r["effort"] == "default" or r["effort"] in tiers)
            and (not r.get("requires_reasoning") or capabilities.get("reasoning") is True)
        ]
        b = max(matching, key=lambda r: r["score"]) if matching else None
        account = evidence.get("providers", {}).get(provider, {})
        cost = "free" if model.endswith(":free") else account.get("cost_class", "unknown")
        pricing = item.get("pricing") or {}
        in_rate, out_rate = rate(pricing.get("input")), rate(pricing.get("output"))
        if cost not in ("free", "subscription"):
            cost = "metered" if in_rate is not None and out_rate is not None else "unknown"
            if in_rate == 0 and out_rate == 0:
                cost = "free"
        effort = b["effort"] if b else "default"
        result = results.get((route, effort))
        fresh = result is not None and 0 <= time.time() - result["checked_at"] <= TTL
        status = result["status"] if fresh else "untested" if result is None else "stale"
        p = policies[provider]
        estimate = (
            0
            if cost in ("free", "subscription")
            else (
                (2048 * in_rate + 512 * out_rate) / 1e6
                if in_rate is not None and out_rate is not None
                else None
            )
        )
        rows.append(
            {
                "route": route,
                "provider": provider,
                "name": b["name"] if b else item.get("name", model),
                "score": b["score"] if b else None,
                "effort": effort,
                "score_note": None if b else "No exact supported TB4 configuration",
                "source_url": b["source_url"] if b else None,
                "context_tokens": rate(item.get("context_length")),
                "cost_class": cost,
                "cost_basis": account.get("basis")
                if cost in ("free", "subscription")
                else "Gateway model pricing",
                "input_usd_per_million": in_rate,
                "output_usd_per_million": out_rate,
                "estimated_probe_usd": estimate,
                "estimated_task_usd": 0
                if cost in ("free", "subscription")
                else (
                    (10000 * in_rate + 1000 * out_rate) / 1e6
                    if in_rate is not None and out_rate is not None
                    else None
                ),
                "account_ready": p["account_ready"],
                "enabled_accounts": p["enabled_accounts"],
                "supports_tools": capabilities.get("tool_calling") is True,
                "status": status,
                "last_test": result,
            }
        )
    rows.sort(key=lambda r: (r["score"] is None, -(r["score"] or 0), r["route"]))
    return {
        "rows": rows,
        "providers": list(policies.values()),
        "catalogue_at": time.time(),
        "account_evidence_at": evidence.get("observed_at"),
        "benchmark": {k: v for k, v in registry.items() if k != "models"},
        "ttl_seconds": TTL,
    }


def plan(rows: list[dict], policy: ProbePolicy) -> tuple[list[dict], dict, float]:
    targets, skipped, estimate = [], Counter(), 0.0
    for row in rows:
        reason = None
        if not row["account_ready"]:
            reason = "Account disabled, blocked or evidence stale"
        elif row["score"] is None and not policy.include_unscored:
            reason = "No published TB4 score"
        elif row["score"] is not None and row["score"] < policy.minimum_score:
            reason = "Below TB4 floor"
        elif not policy.retest and row["status"] not in ("untested", "stale"):
            reason = "Already tested in the last 24 hours"
        else:
            cost = row["cost_class"]
            allowed = cost == "free" or (policy.cost_mode != "free" and cost == "subscription")
            if policy.cost_mode == "metered" and cost == "metered":
                allowed = (
                    row["input_usd_per_million"] <= policy.max_input_usd_per_million
                    and row["output_usd_per_million"] <= policy.max_output_usd_per_million
                )
            if not allowed:
                reason = "Cost outside scope or unknown"
            elif estimate + row["estimated_probe_usd"] > policy.estimated_budget_usd:
                reason = "Estimated batch budget reached"
        if reason:
            skipped[reason] += 1
        else:
            estimate += row["estimated_probe_usd"]
            targets.append(row)
    return targets, dict(skipped), estimate


def output_text(body: dict) -> str:
    return str(body.get("output_text") or "") + "".join(
        str(c.get("text") or "")
        for item in body.get("output", [])
        if item.get("type") == "message"
        for c in item.get("content", [])
        if c.get("type") == "output_text"
    )


async def probe(client: httpx.AsyncClient, row: dict, run_id: str) -> dict:
    start = time.monotonic()
    result: dict = {
        "route": row["route"],
        "effort": row["effort"],
        "run_id": run_id,
        "score": row.get("score"),
        "cost_class": row.get("cost_class"),
        "estimated_cost_usd": row.get("estimated_probe_usd"),
        "checked_at": time.time(),
        "status": "failed",
        "responses_ok": False,
        "tools_ok": False,
        "message": "Probe did not complete",
        "http_status": None,
    }
    token = uuid.uuid4().hex[:10]
    tool = {
        "type": "function",
        "name": "availability_echo",
        "description": "Return a test token unchanged.",
        "parameters": {
            "type": "object",
            "properties": {"token": {"type": "string"}},
            "required": ["token"],
            "additionalProperties": False,
        },
        "strict": True,
    }
    inputs = [
        {
            "role": "user",
            "content": f"Call availability_echo with token {token}. "
            "After its result, reply with that token only.",
        }
    ]
    body = {
        "model": row["route"],
        "input": inputs,
        "max_output_tokens": OUTPUT_LIMIT,
        "store": False,
        "stream": False,
    }
    if row["effort"] != "default":
        body["reasoning"] = {"effort": row["effort"]}
    if row["supports_tools"]:
        body["tools"] = [tool]
        body["tool_choice"] = {"type": "function", "name": "availability_echo"}
    else:
        body["input"] = [{"role": "user", "content": f"Reply with {token} only."}]
    headers = {
        "X-OmniRoute-No-Cache": "true",
        "X-Request-Id": f"availability-{run_id}-{uuid.uuid4().hex}",
        "X-OmniRoute-Compression": "off",
    }

    async def send(payload):
        response = await client.post("/v1/responses", json=payload, headers=headers)
        result["http_status"] = response.status_code
        response.raise_for_status()
        data = response.json()
        served_model = data.get("model")
        if served_model not in (row["route"], row["route"].partition("/")[2]):
            raise ValueError("Response model identity missing or changed")
        result["served_model"] = served_model
        if data.get("error") or data.get("status") in ("failed", "incomplete", "cancelled"):
            raise ValueError("Response did not complete")
        return data

    try:
        async with asyncio.timeout(60):
            first = await send(body)
            result["responses_ok"] = True
            if not row["supports_tools"]:
                if token not in output_text(first):
                    raise ValueError("Expected test reply missing")
                result.update(
                    status="responding", message="Responses reply passed; tools not advertised"
                )
            else:
                calls = [x for x in first.get("output", []) if x.get("type") == "function_call"]
                if (
                    len(calls) != 1
                    or calls[0].get("name") != "availability_echo"
                    or json.loads(calls[0].get("arguments", "{}")) != {"token": token}
                ):
                    raise ValueError("Tool call missing or invalid")
                call = calls[0]
                if not call.get("call_id"):
                    raise ValueError("Tool call ID missing")
                # Echo is simulated locally: no shell, files or user content involved.
                body["input"] = (
                    inputs
                    + first["output"]
                    + [
                        {
                            "type": "function_call_output",
                            "call_id": call["call_id"],
                            "output": token,
                        }
                    ]
                )
                body["tool_choice"] = "none"
                second = await send(body)
                if token not in output_text(second):
                    raise ValueError("Tool continuation reply missing")
                result.update(
                    status="available", tools_ok=True, message="Responses + tool round trip passed"
                )
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        result.update(
            status="limited" if status == 429 else "failed",
            message={
                401: "Authentication failed",
                403: "Access denied",
                404: "Model or endpoint not found",
                429: "Rate limit or quota reached",
            }.get(status, f"HTTP {status}"),
        )
    except (TimeoutError, httpx.TimeoutException):
        result.update(status="timeout", message="No complete round trip within 60 seconds")
    except (ValueError, KeyError, TypeError):
        result.update(
            status="incompatible",
            message="Responses reply or tool continuation was incomplete or invalid",
        )
    except httpx.HTTPError:
        result.update(status="failed", message="Gateway connection failed")
    result["latency_ms"] = round(1000 * (time.monotonic() - start))
    result["checked_at"] = time.time()
    return result


class AvailabilityService:
    def __init__(self, store: ProbeStore | None = None):
        self._store = store
        self._cache: tuple[float, list[dict], list[dict]] | None = None
        self._quota_cache = {}
        self._plans = []
        self._refresh_lock = asyncio.Lock()
        self._task = None

    @property
    def store(self):
        if self._store is None:
            self._store = ProbeStore()
        return self._store

    async def overview(self, refresh=False):
        if not enabled():
            raise OmnigentError("Model availability is not enabled", code=ErrorCode.NOT_FOUND)
        async with self._refresh_lock:
            if refresh or self._cache is None or time.time() - self._cache[0] > 60:
                c = OmniRouteClient.from_env()

                async def optional(path):
                    try:
                        return (await c._request("GET", path, timeout=10)).body
                    except OmniRouteError:
                        return {}

                try:
                    models, providers, quotas, plans = await asyncio.gather(
                        c._request("GET", "/v1/models", timeout=20),
                        c._request("GET", "/api/providers", timeout=20),
                        optional("/api/usage/provider-limits"),
                        optional("/api/quota/plans"),
                    )
                    model_rows, connection_rows = (
                        models.body["data"],
                        providers.body["connections"],
                    )
                    if not isinstance(model_rows, list) or not isinstance(connection_rows, list):
                        raise ValueError("Invalid gateway catalog")
                    self._cache = (time.time(), model_rows, connection_rows)
                    cache_rows, plan_rows = quotas.get("caches"), plans.get("plans")
                    self._quota_cache = cache_rows if isinstance(cache_rows, dict) else {}
                    self._plans = plan_rows if isinstance(plan_rows, list) else []
                except Exception as exc:
                    raise OmnigentError(
                        "Gateway catalogue unavailable; try refresh again", code=ErrorCode.CONFLICT
                    ) from exc
            evidence = readiness()
            data = inventory(
                self._cache[1],
                self._cache[2],
                evidence,
                benchmarks(),
                self.store.results(),
                self._quota_cache,
                self._plans,
            )
            data["catalogue_at"] = self._cache[0]
            data["run"] = self.store.latest()
            if data["run"] and data["run"]["status"] == "running":
                with (self.store.directory / "run.lock").open("a") as lock:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        pass
                    else:
                        data["run"].update(
                            status="interrupted", current=[], finished_at=time.time()
                        )
                        self.store.run(data["run"])
            return data

    async def start(self, policy: ProbePolicy, owner: str | None):
        lock = (self.store.directory / "run.lock").open("a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            lock.close()
            raise OmnigentError(
                "A model test is already running", code=ErrorCode.CONFLICT
            ) from exc
        try:
            data = await self.overview(refresh=True)
            targets, skipped, estimate = plan(data["rows"], policy)
            if not targets:
                raise OmnigentError(
                    "No untested models match this scope. Include recent tests or change filters.",
                    code=ErrorCode.CONFLICT,
                )
            run = {
                "id": uuid.uuid4().hex,
                "owner": owner,
                "started_at": time.time(),
                "status": "running",
                "policy": policy.model_dump(),
                "total": len(targets),
                "completed": 0,
                "available": 0,
                "skipped": skipped,
                "estimated_probe_usd": estimate,
                "current": [],
            }
            self.store.run(run)
            client = OmniRouteClient.from_env()
            self._task = asyncio.create_task(self._execute(run, targets, lock, client))
            return run
        except BaseException:
            lock.close()
            raise

    async def _execute(self, run, targets, lock, c):
        groups = {}
        for row in targets:
            groups.setdefault(row["provider"], []).append(row)
        semaphore = asyncio.Semaphore(2)
        async with httpx.AsyncClient(
            base_url=c.base_url,
            headers={"Authorization": c._authorization},
            timeout=45,
            trust_env=False,
        ) as client:

            async def group(rows):
                async with semaphore:
                    blocked = False
                    for row in rows:
                        # Recheck fresh evidence between probes, without another inference call.
                        fresh = (
                            readiness().get("providers", {}).get(row["provider"], {}).get("active")
                        )
                        if blocked or not fresh:
                            reason = "Provider blocked or account evidence expired during test"
                            run["skipped"][reason] = run["skipped"].get(reason, 0) + 1
                            run["completed"] += 1
                            self.store.run(run)
                            continue
                        run["current"].append(row["route"])
                        self.store.run(run)
                        outcome = await probe(client, row, run["id"])
                        self.store.result(outcome)
                        run["completed"] += 1
                        run["available"] += outcome["status"] == "available"
                        run["current"].remove(row["route"])
                        self.store.run(run)
                        blocked = outcome["http_status"] in (401, 403, 429)
                        await asyncio.sleep(1)

            try:
                async with asyncio.timeout(2700):
                    async with asyncio.TaskGroup() as tasks:
                        for rows in groups.values():
                            tasks.create_task(group(rows))
                run["status"] = "complete"
            except asyncio.CancelledError:
                run["status"] = "cancelled"
            except TimeoutError:
                run["status"] = "time_limit"
            except Exception:
                logging.getLogger(__name__).exception("Availability batch interrupted")
                run["status"] = "interrupted"
            finally:
                run["current"] = []
                run["finished_at"] = time.time()
                self.store.run(run)
                lock.close()

    async def cancel(self):
        if self._task and not self._task.done():
            self._task.cancel()
            await self._task
        return self.store.latest()
