"""Explicit allowlists: preserve observations, omit streams and private tool I/O."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from .store import Store, digest, source_db, timestamp


def uid(value):
    return uuid.UUID(bytes=value).hex if isinstance(value, bytes) and len(value) == 16 else value


def text_content(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(
            str(x.get("text", ""))
            for x in value
            if isinstance(x, dict) and x.get("type") in ("text", "input_text", "output_text")
        )
    return ""


def omnigent(store: Store, source: str, path: Path):
    from omnigent.db.compression import decode
    from omnigent.db.enum_codecs import decode_item_status
    from omnigent.server.task_scoring import select_scored_outcomes

    with source_db(path) as db:
        labels: dict[str, dict] = {}
        for row in db.execute("SELECT conversation_id,key,value FROM conversation_labels"):
            labels.setdefault(uid(row[0]), {})[row[1]] = row[2]
        for row in db.execute(
            "SELECT c.*,m.external_session_id,m.session_usage FROM conversations c "
            "LEFT JOIN omnigent_conversation_metadata m ON c.id=m.id"
        ):
            sid = uid(row["id"])
            tags = labels.get(sid, {})
            overrides = json.loads(row["session_overrides"] or "{}")
            usage = json.loads(decode(row["session_usage"]) or "{}")
            store.upsert(
                "sessions",
                source=source,
                id=sid,
                started=timestamp(row["created_at"]),
                updated=timestamp(row["updated_at"]),
                harness="codex"
                if any("codex_native" in key for key in tags)
                else "claude"
                if any("claude_native" in key for key in tags)
                else None,
                model=overrides.get("model_override"),
                effort=overrides.get("reasoning_effort"),
                status=None,
                is_test=int("omnigent.test.run_id" in tags),
                parent_id=uid(row["parent_conversation_id"]),
                external_id=row["external_session_id"],
                metadata={
                    "usage_basis": "session_cumulative_estimate",
                    "total_cost_usd": usage.get("total_cost_usd"),
                    "routing_policy": tags.get("omnigent.routing_policy"),
                    "tb4_policy": tags.get("omnigent.tb4.policy"),
                    "tb4_receipt": tags.get("omnigent.tb4.receipt"),
                },
            )
        experiments: dict[str, list] = {}
        for row in db.execute(
            "SELECT * FROM conversation_items WHERE type IN (1,2,3,5,8,9) "
            "ORDER BY conversation_id,position"
        ):
            data = json.loads(row["data"])
            sid, eid = uid(row["conversation_id"]), uid(row["id"])
            fields = {
                "session_id": sid,
                "turn_id": row["response_id"],
                "timestamp": timestamp(row["created_at"]),
            }
            if row["type"] == 8:
                resource = data.get("resource") or {}
                resource_type = data.get("resource_type")
                if (
                    resource_type == "task-success-experiment"
                    and resource.get("conversation_id") == sid
                ):
                    experiments.setdefault(sid, []).append(
                        {
                            **resource,
                            "response_id": row["response_id"],
                            "created_by": row["created_by"],
                        }
                    )
                elif resource_type == "response-execution":
                    # Model identity belongs to the response, not the latest session state.
                    attribution = resource.get("attribution") or resource
                    store.event(
                        source,
                        eid,
                        "execution",
                        **fields,
                        model=attribution.get("actual_model")
                        or attribution.get("requested_model"),
                        effort=attribution.get("reasoning_effort"),
                        metadata={
                            k: attribution.get(k)
                            for k in (
                                "requested_model",
                                "actual_model",
                                "provider",
                                "harness",
                                "reasoning_effort",
                                "model_status",
                                "model_source",
                                "access_lane",
                            )
                        },
                    )
                continue
            if row["type"] == 1:
                role = data.get("role")
                if role not in ("user", "assistant") or data.get("is_meta"):
                    continue
                store.event(
                    source,
                    eid,
                    "message",
                    **fields,
                    status=decode_item_status(row["status"]),
                    metadata={"role": role, "position": row["position"]},
                    detail=text_content(data.get("content")),
                )
            else:
                store.event(
                    source,
                    eid,
                    {2: "tool_call", 3: "tool_result", 5: "error", 9: "routing"}[row["type"]],
                    **fields,
                    status="error" if row["type"] == 5 else decode_item_status(row["status"]),
                    metadata={
                        "tool": data.get("name"),
                        "call_id": data.get("call_id"),
                        "position": row["position"],
                    },
                )
        store.db.execute("DELETE FROM human_outcomes WHERE source=?", [source])
        for sid, events in experiments.items():
            for actor in {e.get("created_by") for e in events}:
                if not actor:
                    continue
                for outcome in select_scored_outcomes(
                    events, actor=actor, conversation_id=sid, labels=labels.get(sid, {})
                ):
                    store.upsert(
                        "human_outcomes",
                        source=source,
                        session_id=sid,
                        turn_id=outcome["response_id"],
                        actor=actor,
                        outcome=outcome["outcome"],
                    )
    return {}


def codex_state(store: Store, source: str, path: Path):
    with source_db(path) as db:
        for row in db.execute("SELECT * FROM threads"):
            row = dict(row)
            store.upsert(
                "sessions",
                source=source,
                id=row["id"],
                started=timestamp(row.get("created_at")),
                updated=timestamp(row.get("updated_at")),
                harness="codex",
                model=row.get("model"),
                effort=row.get("reasoning_effort"),
                status=None,
                is_test=None,
                metadata={
                    "attribution_scope": "latest_session_state",
                    "cli_version": row.get("cli_version"),
                    "tokens_cumulative": row.get("tokens_used"),
                },
            )
    return {}


def codex_history(store: Store, source: str, path: Path):
    with source_db(path) as db:
        for row in db.execute(
            "SELECT thread_id,min(started_at) started,max(completed_at) completed "
            "FROM thread_turns GROUP BY thread_id"
        ):
            store.upsert(
                "sessions",
                source=source,
                id=row["thread_id"],
                harness="codex",
                started=timestamp(row["started"]),
                updated=timestamp(row["completed"]),
                metadata={"grain": "history_projection"},
            )
        for row in db.execute("SELECT * FROM thread_turns"):
            store.event(
                source,
                row["thread_id"] + ":" + row["turn_id"],
                "turn",
                session_id=row["thread_id"],
                turn_id=row["turn_id"],
                timestamp=timestamp(row["started_at"]),
                status=row["status"],
                duration_ms=row["duration_ms"],
                metadata={
                    "completed_at": timestamp(row["completed_at"]),
                    "has_error": bool(row["error_json"]),
                },
            )
        # Only the original task and final answer: no reasoning or intermediary assistant stream.
        for row in db.execute(
            "SELECT i.* FROM thread_items i JOIN thread_turns t "
            "ON t.thread_id=i.thread_id AND t.turn_id=i.turn_id "
            "WHERE i.item_id IN (t.first_user_item_id,t.final_agent_item_id) "
            "OR i.item_type IN ('commandExecution','mcpToolCall','dynamicToolCall','fileChange')"
        ):
            value = json.loads(row["item_json"])
            is_message = row["item_type"] in ("userMessage", "agentMessage")
            store.event(
                source,
                row["thread_id"] + ":" + row["item_id"],
                "message" if is_message else "tool_result",
                session_id=row["thread_id"],
                turn_id=row["turn_id"],
                timestamp=timestamp(row["created_at_ms"]),
                status=value.get("status"),
                duration_ms=value.get("durationMs"),
                metadata={
                    "item_type": row["item_type"],
                    "exit_code": value.get("exitCode"),
                    "tool": value.get("tool"),
                },
                detail=text_content(value.get("content")) or value.get("text")
                if is_message
                else None,
            )
    return {}


def zcode(store: Store, source: str, path: Path):
    with source_db(path) as db:
        for row in db.execute("SELECT * FROM tasks"):
            store.upsert(
                "sessions",
                source=source,
                id=row["task_id"],
                harness="zcode",
                model=row["model"],
                started=timestamp(row["created_at"]),
                updated=timestamp(row["updated_at"]),
                status=row["task_status"],
                is_test=None,
                metadata={"provider": row["provider"], "status_is_not_success": True},
            )
    return {}


def mlflow(store: Store, source: str, path: Path):
    """Backfill trace-level evidence, never the millions of stream spans."""
    checkpoint = store.checkpoint(source)
    if (
        checkpoint.get("size") == path.stat().st_size
        and checkpoint.get("mtime") == path.stat().st_mtime_ns
    ):
        return checkpoint
    with source_db(path) as db:
        for row in db.execute(
            "SELECT request_id,timestamp_ms,execution_time_ms,status,client_request_id "
            "FROM trace_info"
        ):
            store.event(
                source,
                row["request_id"],
                "legacy_trace",
                timestamp=timestamp(row["timestamp_ms"]),
                duration_ms=row["execution_time_ms"],
                status=row["status"],
                metadata={
                    "client_request_id": row["client_request_id"],
                    "artifacts_available": False,
                },
            )
    return {"size": path.stat().st_size, "mtime": path.stat().st_mtime_ns}


_OTLP_KEYS = (
    "event.name",
    "event.timestamp",
    "conversation.id",
    "session.id",
    "turn.id",
    "model",
    "model_reasoning_effort",
    "http.response.status_code",
    "attempt",
    "duration_ms",
    "success",
    "status",
    "status_code",
    "input_token_count",
    "output_token_count",
    "cached_token_count",
    "tool_name",
    "call_id",
    "request_id",
)
_LOG_EVENTS = {
    "codex.api_request",
    "codex.websocket_request",
    "codex.tool_result",
    "codex.user_prompt",
    "codex.conversation_starts",
    "codex.turn_complete",
}


def attrs(values):
    return {a["key"]: next(iter(a.get("value", {}).values()), None) for a in values}


def otlp_record(store: Store, source: str, value: dict):
    for resource in value.get("resourceLogs", []):
        for scope in resource.get("scopeLogs", []):
            for row in scope.get("logRecords", []):
                a = attrs(row.get("attributes", []))
                event = a.get("event.name") or row.get("eventName")
                if event not in _LOG_EVENTS:
                    continue
                a = {k: a[k] for k in _OTLP_KEYS if k in a}
                sid = a.get("conversation.id") or a.get("session.id")
                when = timestamp(
                    a.get("event.timestamp")
                    or row.get("timeUnixNano")
                    or row.get("observedTimeUnixNano")
                )
                store.event(
                    source,
                    digest([row.get("timeUnixNano"), row.get("observedTimeUnixNano"), a]),
                    event,
                    session_id=sid,
                    turn_id=a.get("turn.id"),
                    timestamp=when,
                    model=a.get("model"),
                    effort=a.get("model_reasoning_effort"),
                    status=str(
                        a.get(
                            "http.response.status_code",
                            a.get("status_code", a.get("success", "unknown")),
                        )
                    ),
                    duration_ms=a.get("duration_ms"),
                    input_tokens=a.get("input_token_count"),
                    output_tokens=a.get("output_token_count"),
                    cached_tokens=a.get("cached_token_count"),
                    metadata={
                        "usage_basis": "request_observation",
                        "tool": a.get("tool_name"),
                        "call_id": a.get("call_id"),
                        "request_id": a.get("request_id"),
                    },
                )
                if sid:
                    # Session attribution remains in events: models can change between turns.
                    store.db.execute(
                        "INSERT OR IGNORE INTO sessions(source,id,harness,metadata) "
                        "VALUES (?,?,?,?)",
                        [source, sid, "codex", "{}"],
                    )
    for resource in value.get("resourceSpans", []):
        for scope in resource.get("scopeSpans", []):
            for row in scope.get("spans", []):
                a = attrs(row.get("attributes", []))
                # Exclude routine HTTP/auth spans. Keep only model observations and errors.
                model = a.get("gen_ai.response.model") or a.get("gen_ai.request.model")
                error = (row.get("status") or {}).get("code") == 2
                if not model and not error:
                    continue
                start, end = (
                    timestamp(row.get("startTimeUnixNano")),
                    timestamp(row.get("endTimeUnixNano")),
                )
                store.event(
                    source,
                    row.get("spanId") or digest(row),
                    "model_span" if model else "error_span",
                    session_id=a.get("session.id"),
                    timestamp=start,
                    model=model,
                    status="error" if error else "observed",
                    duration_ms=(end - start) * 1000 if start and end else None,
                    input_tokens=a.get("gen_ai.usage.input_tokens"),
                    output_tokens=a.get("gen_ai.usage.output_tokens"),
                    metadata={
                        "trace_id": row.get("traceId"),
                        "span_name": row.get("name"),
                        "usage_basis": "span_observation",
                    },
                )


def jsonl(store: Store, source: str, path: Path, consume, *, quarantine=False):
    checkpoint = store.checkpoint(source)
    stat = path.stat()
    identity = [stat.st_dev, stat.st_ino]
    # Detect rotation, replacement and truncation. Commit cursor and records together.
    offset = (
        checkpoint.get("offset", 0)
        if checkpoint.get("identity") == identity and checkpoint.get("adapter_version") == 2
        else 0
    )
    if offset > stat.st_size:
        offset = 0
    malformed = checkpoint.get("malformed_lines", 0) if offset else 0
    with path.open("rb") as handle:
        handle.seek(offset)
        while line := handle.readline():
            if not line.endswith(b"\n"):
                break  # An active writer owns this partial record; retry next scan.
            try:
                value = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                if not quarantine:
                    raise
                malformed += 1
                store.event(
                    source,
                    "gap:" + digest([identity, offset]),
                    "ingestion_gap",
                    metadata={
                        "offset": offset,
                        "bytes": len(line),
                        "sha256": digest(line.hex()),
                        "error": "malformed_jsonl",
                    },
                )
            else:
                consume(store, source, value)
            offset = handle.tell()
    return {
        "identity": identity,
        "offset": offset,
        "malformed_lines": malformed,
        "adapter_version": 2,
    }


def zcode_io_record(store: Store, source: str, row: dict):
    if row.get("type") != "model_io":
        return
    model = row.get("model") or {}
    response = row.get("response") or {}
    usage = response.get("usage") or {}
    sid = row.get("sessionId")
    store.event(
        source,
        digest([row.get("requestId"), row.get("attempt"), row.get("startedAt")]),
        "model_request",
        session_id=sid,
        turn_id=row.get("turnId"),
        timestamp=timestamp(row.get("startedAt")),
        model=response.get("modelId") or model.get("modelId"),
        status="error" if row.get("error") else "completed",
        duration_ms=row.get("durationMs"),
        input_tokens=usage.get("inputTokens"),
        output_tokens=usage.get("outputTokens"),
        cached_tokens=usage.get("cacheReadTokens"),
        metadata={
            "provider": model.get("providerId"),
            "requested_model": model.get("modelId"),
            "request_id": row.get("requestId"),
            "finish_reason": response.get("finishReason"),
            "usage_basis": "request_observation",
        },
    )
    if sid:
        store.db.execute(
            "INSERT OR IGNORE INTO sessions(source,id,harness,metadata) VALUES (?,?,?,?)",
            [source, sid, "zcode", "{}"],
        )


def routing_receipt(store: Store, source: str, path: Path):
    row = json.loads(path.read_text())
    selected = row["selected"]
    store.event(
        source,
        row["id"],
        "tb4_selection",
        timestamp=row["created_at"],
        model=selected["route"],
        effort=selected["effort"],
        metadata={
            "policy": row["policy"],
            "score": selected["score"],
            "cost_class": selected["cost_class"],
            "source_url": selected["source_url"],
            "benchmark_at": row["benchmark"]["retrieved_at"],
            "eligible_routes": row["eligible_routes"],
            "receipt_id": row["id"],
        },
    )
    return {}


def codex_rollout_record(store: Store, source: str, row: dict):
    payload = row.get("payload") or {}
    kind = row.get("type")
    when = timestamp(row.get("timestamp"))
    checkpoint = getattr(store, "rollout_context", {})
    if kind == "session_meta":
        checkpoint = {"session": payload.get("id")}
        store.rollout_context = checkpoint
        store.db.execute(
            "INSERT OR IGNORE INTO sessions(source,id,harness,metadata) VALUES (?,?,?,?)",
            [source, payload.get("id"), "codex", "{}"],
        )
    elif kind == "turn_context":
        checkpoint.update(
            model=payload.get("model"), effort=payload.get("effort"), turn=payload.get("turn_id")
        )
    elif kind == "event_msg" and payload.get("type") in (
        "task_started",
        "task_complete",
        "turn_aborted",
        "token_count",
    ):
        store.event(
            source,
            digest(row),
            payload["type"],
            session_id=checkpoint.get("session"),
            turn_id=payload.get("turn_id") or checkpoint.get("turn"),
            timestamp=when,
            model=checkpoint.get("model"),
            effort=checkpoint.get("effort"),
            metadata={
                "usage_basis": "cumulative_snapshot",
                "usage": (payload.get("info") or {}).get("total_token_usage"),
            },
        )


def gateway_record(store: Store, source: str, row: dict):
    # The exporter and importer both allowlist; a remote JSON object is never copied wholesale.
    store.event(
        source,
        str(row["id"]),
        "gateway_request",
        session_id=row.get("session_id"),
        turn_id=row.get("response_id"),
        timestamp=timestamp(row.get("timestamp")),
        model=row.get("model"),
        effort=row.get("upstream_effort"),
        status=str(row.get("status")),
        input_tokens=row.get("input_tokens"),
        output_tokens=row.get("output_tokens"),
        cached_tokens=row.get("cached_tokens"),
        duration_ms=row.get("duration_ms"),
        estimated_cost_usd=row.get("estimated_cost_usd"),
        cost_basis="published_token_price_estimate"
        if row.get("estimated_cost_usd") is not None
        else "unknown",
        metadata={
            k: row.get(k)
            for k in (
                "provider",
                "requested_model",
                "correlation_id",
                "requested_effort",
                "endpoint",
            )
        },
    )


def model_probe(store: Store, source: str, path: Path):
    with source_db(path) as db:
        for record in db.execute("SELECT data FROM history"):
            model_probe_record(store, source, json.loads(record[0]))
    return {}


def model_probe_snapshot(store: Store, source: str, path: Path):
    data = json.loads(path.read_text())
    if data.get("version") != 1 or not isinstance(data.get("history"), list):
        raise ValueError("Invalid availability snapshot")
    for row in data["history"]:
        model_probe_record(store, source, row)
    return {}


def model_probe_record(store: Store, source: str, row: dict):
    store.event(
        source,
        digest([row["run_id"], row["route"], row["effort"]]),
        "model_availability_probe",
        timestamp=row["checked_at"],
        model=row["route"],
        effort=row["effort"],
        status=row["status"],
        duration_ms=row.get("latency_ms"),
        estimated_cost_usd=row.get("estimated_cost_usd"),
        cost_basis="synthetic_probe_budget_estimate",
        metadata={
            "is_test": True,
            "scoring_eligible": False,
            **{
                k: row.get(k)
                for k in (
                    "run_id",
                    "score",
                    "cost_class",
                    "http_status",
                    "served_model",
                    "text_ok",
                    "text_status",
                    "text_http_status",
                    "responses_ok",
                    "tools_ok",
                    "tools_status",
                    "tools_checked_at",
                    "tools_http_status",
                    "profile",
                )
            },
        },
    )


ADAPTERS = {
    "omnigent": omnigent,
    "codex_state": codex_state,
    "codex_history": codex_history,
    "zcode": zcode,
    "mlflow": mlflow,
    "otlp": lambda s, n, p: jsonl(s, n, p, otlp_record),
    # Rollouts may rotate and contain late context; rescan the bounded source, dedup by content ID.
    "gateway": lambda s, n, p: jsonl(s, n, p, gateway_record),
    "zcode_io": lambda s, n, p: jsonl(s, n, p, zcode_io_record),
    "routing_receipt": routing_receipt,
    "model_probe": model_probe,
    "model_probe_snapshot": model_probe_snapshot,
}


def codex_rollout(store: Store, source: str, path: Path):
    store.rollout_context = {}
    with path.open() as handle:
        for line in handle:
            if line.endswith("\n"):
                codex_rollout_record(store, source, json.loads(line))
    return {}


ADAPTERS["codex_rollout"] = codex_rollout
