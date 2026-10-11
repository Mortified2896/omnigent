"""Local, aggregate-only audit of Omnigent SQLite evidence. No provider calls.

Reads one transaction through SQLite mode=ro; excludes forks' copied events,
folds review revisions by append position, and uses response-scoped attribution.
Output contains no prompts, comments, user IDs, session IDs, or secrets.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path


def identity(value):
    """Match SQLAlchemy's SQLite GUID storage to the API's compact UUIDs."""
    if isinstance(value, bytes) and len(value) == 16:
        return uuid.UUID(bytes=value).hex
    if isinstance(value, str):
        try:
            return uuid.UUID(value).hex
        except ValueError:
            return value
    return value


def audit(path: Path) -> dict:
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("BEGIN")
    sessions = {
        identity(r["id"]): dict(r) for r in connection.execute("SELECT * FROM conversations")
    }
    labels: dict[str, dict] = defaultdict(dict)
    for row in connection.execute("SELECT conversation_id,key,value FROM conversation_labels"):
        labels[identity(row["conversation_id"])][row["key"]] = row["value"]
    excluded = {
        sid
        for sid in sessions
        if "omnigent.test.run_id" in labels[sid]
        or labels[sid].get("omnigent.scoring.eligible", "true") != "true"
    }
    events, messages, attributions, bindings = [], [], {}, {}
    terminal_responses = set()
    item_types, resource_types = Counter(), Counter()
    malformed = copied = 0
    first_prompts: dict[str, str] = {}
    dates = []
    for row in connection.execute(
        "SELECT * FROM conversation_items ORDER BY conversation_id,position"
    ):
        item_types[str(row["type"])] += 1
        dates.append(row["created_at"])
        try:
            data = json.loads(row["data"])
        except (ValueError, TypeError):
            malformed += 1
            continue
        sid = identity(row["conversation_id"])
        if data.get("role"):
            item = {
                **dict(row),
                "conversation_id": sid,
                "response_id": identity(row["response_id"]),
                "parsed": data,
            }
            messages.append(item)
            if data.get("role") == "user" and sid not in first_prompts:
                # Exact content hash for duplicate screening only; never emit it.
                first_prompts[sid] = hashlib.sha256(
                    json.dumps(data.get("content"), sort_keys=True).encode()
                ).hexdigest()
        if "resource_type" not in data:
            continue
        resource_types[data.get("event_type", "unknown")] += 1
        payload = data.get("resource") or {}
        if not isinstance(payload, dict):
            malformed += 1
            continue
        if data["resource_type"] not in {
            "task-success-experiment",
            "response-execution",
            "response-advisor-binding",
        }:
            continue
        if identity(payload.get("conversation_id")) != sid:
            copied += 1
            continue
        rid = identity(row["response_id"])
        if data["resource_type"] == "task-success-experiment":
            events.append({**payload, "response_id": rid, "actor": row["created_by"], "sid": sid})
        elif identity(payload.get("response_id")) != rid:
            malformed += 1
        elif data["resource_type"] == "response-execution":
            attributions[sid, rid] = payload
            if data.get("event_type") == "response.execution.completed":
                terminal_responses.add((sid, rid))
        else:
            bindings[sid, rid] = payload
    latest, eligibility = {}, {}
    for event in events:
        key = event["sid"], event["response_id"], event["actor"]
        if event.get("kind") == "outcome" and event.get("review_source", "human") == "human":
            latest[key] = event
        elif event.get("kind") == "scoring_eligibility":
            eligibility[key] = event.get("score_eligible") is True
    message_complete = {
        (m["conversation_id"], m["response_id"])
        for m in messages
        if m["parsed"]["role"] == "assistant"
        and m["status"] in (1, "completed")
        and not m["parsed"].get("is_meta", False)
        and not m["parsed"].get("interrupted", False)
    }
    # Native interim text chunks also have completed message status. The
    # response execution event is persisted on the terminal idle edge. Missing
    # attribution/terminal records leave completion unconfirmed, not failed.
    completed = message_complete & terminal_responses
    included = {
        key: event
        for key, event in latest.items()
        if key[0] not in excluded and eligibility.get(key, True)
    }
    known = {
        key: event
        for key, event in included.items()
        if event.get("outcome") in {"success", "partial", "failed"}
    }
    joined = {key: event for key, event in known.items() if key[:2] in completed}
    coverage = Counter()
    models, efforts, lanes, sources = Counter(), Counter(), Counter(), Counter()
    for key in joined:
        attr = attributions.get(key[:2], {})
        for field in ("actual_model", "reasoning_effort", "access_lane", "advisor_round_id"):
            if attr.get(field):
                coverage[field] += 1
        models[attr.get("actual_model") or "unknown"] += 1
        efforts[attr.get("reasoning_effort") or "unknown"] += 1
        lanes[attr.get("access_lane") or "unknown"] += 1
        sources[attr.get("model_source") or "unknown"] += 1
        if key[:2] in bindings:
            coverage["response_binding"] += 1
    advisor_states, advisor_coverage = Counter(), Counter()
    overheads, rounds = [], {}
    tables = {
        r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "model_advisor_records" in tables:
        for row in connection.execute("SELECT state,payload FROM model_advisor_records"):
            advisor_states[row["state"]] += 1
            payload = json.loads(row["payload"])
            frozen = payload.get("frozen") or {}
            if frozen.get("round_id"):
                rounds[frozen["round_id"]] = payload
            overhead = payload.get("advisor_overhead")
            if isinstance(overhead, dict):
                overheads.append(overhead)
                advisor_coverage.update(k for k, v in overhead.items() if v is not None)
    matched_rounds = 0
    for key in joined:
        attr = attributions.get(key[:2], {})
        record = rounds.get(attr.get("advisor_round_id"))
        if record:
            matched_rounds += 1
    session_fields = Counter()
    if "omnigent_conversation_metadata" in tables:
        for row in connection.execute("SELECT * FROM omnigent_conversation_metadata"):
            session_fields.update(
                k
                for k in (
                    "host_id",
                    "runner_id",
                    "external_session_id",
                    "session_usage",
                    "inference_snapshot",
                )
                if row[k]
            )
    n = len(joined)
    connection.rollback()
    connection.close()
    return {
        "schema_version": 1,
        "audited_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_kind": "single SQLite read transaction; not an all-host collection",
        "sessions": len(sessions),
        "excluded_sessions": len(excluded),
        "test_sessions": sum("omnigent.test.run_id" in labels[s] for s in sessions),
        "child_sessions": sum(bool(s.get("parent_conversation_id")) for s in sessions.values()),
        "items_by_storage_type": dict(item_types),
        "resource_events": dict(resource_types),
        "message_roles": dict(Counter(m["parsed"]["role"] for m in messages)),
        "completed_response_keys": len(completed),
        "message_complete_response_keys": len(message_complete),
        "completion_unconfirmed_response_keys": len(message_complete - completed),
        "eligible_completed_response_keys": sum(sid not in excluded for sid, _ in completed),
        "raw_human_outcome_events": sum(
            e.get("kind") == "outcome" and e.get("review_source", "human") == "human"
            for e in events
        ),
        "latest_actor_response_outcomes": len(latest),
        "latest_all_outcomes": dict(Counter(e.get("outcome") for e in latest.values())),
        "latest_included_outcomes": dict(Counter(e.get("outcome") for e in included.values())),
        "eligible_known_outcomes": len(known),
        "joined_known_outcomes": n,
        "known_outcomes_without_completed_answer": len(known) - n,
        "eligible_responses_without_known_outcome": len(
            {k for k in completed if k[0] not in excluded} - {k[:2] for k in joined}
        ),
        "joined_field_coverage": {
            k: {"present": coverage[k], "missing": n - coverage[k]}
            for k in (
                "actual_model",
                "reasoning_effort",
                "access_lane",
                "advisor_round_id",
                "response_binding",
            )
        },
        "joined_models": dict(models),
        "joined_efforts": dict(efforts),
        "joined_lanes": dict(lanes),
        "joined_model_sources": dict(sources),
        "response_attribution_records": len(attributions),
        "advisor_binding_records": len(bindings),
        "advisor_record_states": dict(advisor_states),
        "advisor_overhead_field_coverage": dict(advisor_coverage),
        "advisor_latency_ms": sorted(
            o["latency_ms"] for o in overheads if isinstance(o.get("latency_ms"), (int, float))
        ),
        "known_outcomes_joined_to_advisor_round": matched_rounds,
        "session_metadata_field_coverage": dict(session_fields),
        "duplicate_first_prompt_groups": sum(
            v > 1 for v in Counter(first_prompts.values()).values()
        ),
        "sessions_in_duplicate_first_prompt_groups": sum(
            v for v in Counter(first_prompts.values()).values() if v > 1
        ),
        "copied_events_ignored": copied,
        "malformed_records": malformed,
        "item_time_range_utc": [
            datetime.fromtimestamp(t, timezone.utc).isoformat() for t in (min(dates), max(dates))
        ]
        if dates
        else [],
        "limits": [
            "Outcome units are caller/response observations, not independent tasks.",
            "Completed response does not imply user task completion.",
            "Model source session_reported is weaker than response_usage.",
            "Session metadata describes current state, not historical turn state.",
            "Exact prompt duplication is a lower bound; semantic task families need review.",
            "No counterfactual model outcomes or causal routing benefit can be inferred.",
            "No raw prompts or review text are exported; no automated scorer is enabled.",
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    args = parser.parse_args()
    print(json.dumps(audit(args.database), indent=2, sort_keys=True))
