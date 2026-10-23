"""Allowlisted client observations, never task scores or a claim of upstream identity."""

import json
import re
import time

from .store import digest


def observations(store) -> list[dict]:
    result = []
    rows = store.db.execute(
        "SELECT e.*,s.harness FROM events e JOIN sessions s "
        "ON s.source=e.source AND s.id=e.session_id "
        "WHERE e.kind='execution' AND e.turn_id IS NOT NULL AND e.timestamp>? "
        "AND json_extract(s.metadata,'$.routing_policy')='tb4' "
        "ORDER BY e.timestamp DESC LIMIT 1000",
        (time.time() - 90 * 86400,),
    )
    for row in rows:
        metadata = json.loads(row["metadata"])
        route = metadata.get("actual_model")
        harness = {"codex": "codex-native", "claude": "claude-native"}.get(row["harness"])
        if (
            not harness
            or not route
            or not re.fullmatch(r"[a-zA-Z0-9_.:/+-]{1,200}", route)
            or "/" not in route
            or route != metadata.get("requested_model")
            or metadata.get("model_status") != "observed"
            or metadata.get("access_lane") != "omniroute"
        ):
            continue
        events = list(
            store.db.execute(
                "SELECT kind,status,timestamp,metadata FROM events "
                "WHERE source=? AND session_id=? AND turn_id=?",
                (row["source"], row["session_id"], row["turn_id"]),
            )
        )
        errors = [e for e in events if e["kind"] == "error" or e["status"] == "failed"]
        responses = [
            e
            for e in events
            if e["kind"] == "message"
            and e["status"] == "completed"
            and json.loads(e["metadata"]).get("role") == "assistant"
        ]
        if not errors and not responses:
            continue
        calls = {
            json.loads(e["metadata"]).get("call_id") for e in events if e["kind"] == "tool_call"
        }
        outputs = {
            json.loads(e["metadata"]).get("call_id")
            for e in events
            if e["kind"] == "tool_result" and e["status"] == "completed"
        }
        calls.discard(None)
        result.append(
            {
                "id": digest([row["source"], row["session_id"], row["turn_id"], route]),
                "route": route,
                "effort": row["effort"] or "default",
                "harness": harness,
                "checked_at": max(e["timestamp"] for e in errors + responses if e["timestamp"]),
                "outcome": "error_observed" if errors else "response_observed",
                "tools_completed": bool(calls) and calls.issubset(outputs) and not errors,
                "source": "omnigent-trace-projection",
            }
        )
    return result
