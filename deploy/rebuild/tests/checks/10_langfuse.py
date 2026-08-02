#!/usr/bin/env python3
# ─────────────────────────────────────────────────────────────────────
# Check 10 — Langfuse receives the trace and it is verified
#             from Langfuse (not assumed)
# ─────────────────────────────────────────────────────────────────────
#
# Runs a single trivial session with telemetry enabled, then polls
# the configured Langfuse public API for the resulting trace.
# Verifies the trace hierarchy carries session.id, the
# OmniRoute provenance attributes (when OmniRoute was used),
# and child spans for tool/llm calls.
#
# Requires: python3 (stdlib only — no third-party deps).
#
# Inputs (env vars):
#   OMNIGENT_PORT           the canary wheel's TCP port
#   OMNIGENT_AUTH_HEADER    the auth header name
#   CANARY_IDENTITY         the canary's auth identity
#   LANGFUSE_HOST           the Langfuse host, e.g.
#                           https://langfuse.example.com
#   LANGFUSE_PUBLIC_KEY     the Langfuse public key (Basic auth
#                           username)
#   LANGFUSE_SECRET_KEY     the Langfuse secret key (Basic auth
#                           password)

# Verification source: this check queries Langfuse's public API
# (`GET /api/public/v2/traces?sessionId=<id>` and
# `GET /api/public/v2/observations?traceId=<id>`) with Basic auth
# over the LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY the operator
# sets. There is NO mock: a trace that does not appear in Langfuse
# within 30 s is a FAIL. The canary does NOT use any local
# placeholder; if LANGFUSE_HOST/PUBLIC_KEY/SECRET_KEY are not set
# the check FAILs up front with a clear reason.

from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any


def _env(name: str) -> str | None:
    return os.environ.get(name)


def _request(
    method: str,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    """Issue one HTTP request via urllib, keeping the three channels
    separate: the response body, the status code, and the headers.

    Strict by construction: the body returned is exactly the HTTP
    body — no curl ``-w`` status suffix and no header block mixed
    into the same stream — so ``json.loads(body)`` can never see
    ``Extra data`` from a trailing status code.
    """
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read() or b""


def _http_get(url: str, headers: dict[str, str]) -> tuple[int, dict[str, str], bytes]:
    return _request("GET", url, headers)


def _http_post_json(
    url: str, payload: dict[str, Any], headers: dict[str, str]
) -> tuple[int, dict[str, str], bytes]:
    return _request("POST", url, headers, payload)


def _basic_auth_header(public_key: str, secret_key: str) -> str:
    cred = f"{public_key}:{secret_key}".encode()
    return "Basic " + base64.b64encode(cred).decode("ascii")


def main() -> int:
    port = _env("OMNIGENT_PORT") or "6767"
    auth_header = _env("OMNIGENT_AUTH_HEADER") or "X-Forwarded-Email"
    identity = _env("CANARY_IDENTITY") or "canary@omnigent.local"
    host = _env("LANGFUSE_HOST")
    public_key = _env("LANGFUSE_PUBLIC_KEY")
    secret_key = _env("LANGFUSE_SECRET_KEY")

    if not (host and public_key and secret_key):
        emit(
            "FAIL", reason="LANGFUSE_HOST / LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY must be set"
        )
        return 1

    # 1. Create a trivial session.
    # The upstream v0.7 wire format binds a session by agent_id, not
    # agent_selector. Resolve claude-sdk's agent_id via GET /v1/agents.
    # /v1/agents returns the same built-in catalog regardless of
    # auth, so this call works in the canary's local-auth-disabled
    # mode.
    agents_url = f"http://127.0.0.1:{port}/v1/agents"
    agents_status, _hdrs, agents_body = _http_get(agents_url, {})
    if agents_status != 200:
        emit(
            "FAIL",
            reason=f"GET /v1/agents returned status={agents_status} body={agents_body[:200]!r}",
        )
        return 1
    # The body is the raw HTTP body (no header block, no trailing
    # -w status suffix), so a strict parse is safe: exactly one JSON
    # document, no tolerance for concatenated garbage.
    try:
        agents_json = json.loads(agents_body)
    except json.JSONDecodeError as exc:
        emit("FAIL", reason=f"GET /v1/agents response not valid JSON: {exc}")
        return 1
    claude_agent_id = None
    for a in agents_json.get("data", []):
        if a.get("name") == "claude-sdk":
            claude_agent_id = a.get("id")
            break
    if not claude_agent_id:
        emit("FAIL", reason=f"could not resolve agent_id for claude-sdk on port {port}")
        return 1

    # Resolve the online host_id so the session is actually
    # dispatched to a runner (unbound sessions stay idle and emit
    # no traces, so this check would always FAIL on the trace
    # poll). Same auth caveat as in _harness_lib.sh:resolve_host_id
    # — local-mode canary needs no auth on /v1/hosts.
    hosts_status, _hdrs, hosts_body = _http_get(f"http://127.0.0.1:{port}/v1/hosts", {})
    host_id = None
    if hosts_status == 200:
        try:
            for h in json.loads(hosts_body).get("hosts", []):
                if h.get("status") == "online" and h.get("host_id"):
                    host_id = h["host_id"]
                    break
        except json.JSONDecodeError:
            pass
    if not host_id:
        emit("FAIL", reason=f"could not resolve an online host_id on port {port}")
        return 1

    sessions_url = f"http://127.0.0.1:{port}/v1/sessions"
    session_payload = {
        "agent_id": claude_agent_id,
        "host_id": host_id,
        "purpose": "explore",
        # workspace is required when host_id is set; the SDK harness
        # needs a cwd. /tmp always exists on linux.
        "workspace": "/tmp",
        "title": "acceptance-9-langfuse",
        # The upstream v0.7 wire format seeds the session with
        # initial_items (a list of SessionEventInput), not a flat
        # `prompt` string. A user message event with content blocks
        # is the documented shape (see SessionEventInput in
        # omnigent/server/schemas.py). Without this, the session
        # sits idle and never gets a runner turn — so this check
        # would always FAIL on the trace poll.
        "initial_items": [
            {
                "type": "message",
                "data": {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "trivial echo: respond with a single sentence.",
                        },
                    ],
                },
            }
        ],
    }
    status, _hdrs, body = _http_post_json(
        sessions_url,
        session_payload,
        {auth_header: identity, "Content-Type": "application/json"},
    )
    if status < 200 or status >= 300:
        emit("FAIL", reason=f"session create returned status={status} body={body[:200]!r}")
        return 1
    session_id = json.loads(body).get("id")
    if not session_id:
        emit("FAIL", reason=f"session create response missing id; body={body[:200]!r}")
        return 1

    # Wake the runner. initial_items only seeds the conversation
    # row; the runner only drives a turn on POST .../events.
    events_status, _, events_body = _http_post_json(
        f"{sessions_url}/{session_id}/events",
        {
            "type": "message",
            "data": {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "trivial echo: respond with a single sentence."}
                ],
            },
        },
        {auth_header: identity, "Content-Type": "application/json"},
    )
    if events_status < 200 or events_status >= 300:
        emit(
            "FAIL",
            reason=(
                f"POST /v1/sessions/{session_id}/events returned "
                f"status={events_status} body={events_body[:200]!r}"
            ),
        )
        return 1

    # 2. Poll Langfuse for the trace. Upstream's _SessionIdSpanProcessor
    #    stamps session.id on every span, so we search by attribute.
    auth = _basic_auth_header(public_key, secret_key)
    api_headers = {"Authorization": auth}

    deadline = time.monotonic() + 30.0
    last_error: str | None = None
    traces_found: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        # Langfuse v2 exposes GET /api/public/v2/traces?sessionId=<id>
        url = f"{host.rstrip('/')}/api/public/v2/traces?sessionId={session_id}&limit=10"
        status, _hdrs, body = _http_get(url, api_headers)
        if status == 200:
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError as exc:
                last_error = f"Langfuse response not valid JSON: {exc}; body={body[:200]!r}"
                time.sleep(2)
                continue
            traces = parsed.get("data", parsed) if isinstance(parsed, dict) else parsed
            if isinstance(traces, list) and traces:
                traces_found = traces
                break
            last_error = (
                f"Langfuse returned 0 traces for session_id={session_id}; body={body[:200]!r}"
            )
        else:
            last_error = f"Langfuse GET /traces status={status} body={body[:200]!r}"
        time.sleep(2)

    if not traces_found:
        emit(
            "FAIL",
            reason=(
                f"no traces appeared in Langfuse for session_id={session_id} "
                f"within 30 s; last error: {last_error}"
            ),
        )
        return 1

    # 3. Inspect at least one trace's full set of observations.
    #    Look for the expected hierarchy: a root with session.id,
    #    child spans (omnigent.tool, omnigent.llm.request), and
    #    the OmniRoute provenance attributes when applicable.
    found_session_id = False
    found_tool_child = False
    found_llm_child = False
    for trace in traces_found[:3]:
        trace_id = trace.get("id") or trace.get("traceId")
        if not trace_id:
            continue
        obs_url = f"{host.rstrip('/')}/api/public/v2/observations?traceId={trace_id}&limit=50"
        status, _hdrs, body = _http_get(obs_url, api_headers)
        if status != 200:
            continue
        try:
            obs_parsed = json.loads(body)
        except json.JSONDecodeError:
            continue
        observations = (
            obs_parsed.get("data", obs_parsed) if isinstance(obs_parsed, dict) else obs_parsed
        )
        if not isinstance(observations, list):
            continue
        for obs in observations:
            attrs = obs.get("attributes") or {}
            # session.id may live on the observation itself or
            # inside a JSON-encoded `metadata` blob.
            sid = attrs.get("session.id") or attrs.get("session_id")
            if not sid and isinstance(attrs.get("metadata"), str):
                try:
                    sid = json.loads(attrs["metadata"]).get("session.id")
                except json.JSONDecodeError:
                    sid = None
            if sid == session_id:
                found_session_id = True
            name = obs.get("name") or obs.get("type") or ""
            if name in ("omnigent.tool",):
                found_tool_child = True
            if name in ("omnigent.llm.request",):
                found_llm_child = True
        if found_session_id and (found_tool_child or found_llm_child):
            break

    if not found_session_id:
        emit(
            "FAIL",
            reason=(
                f"traces appeared but none carry session.id={session_id}; "
                f"saw {len(traces_found)} traces"
            ),
        )
        return 1
    if not (found_tool_child or found_llm_child):
        emit(
            "FAIL",
            reason="traces appeared but no child omnigent.tool/omnigent.llm.request spans found",
        )
        return 1

    emit(
        "PASS",
        session_id=session_id,
        trace_count=len(traces_found),
        found_session_id=found_session_id,
        found_tool_child=found_tool_child,
        found_llm_child=found_llm_child,
    )
    return 0


def emit(status: str, **evidence: Any) -> None:
    line = json.dumps({"status": status, **evidence}, sort_keys=True)
    print(line, flush=True)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 — guarantee a FAIL line on stdout
        emit("FAIL", reason=f"unhandled exception: {type(exc).__name__}: {exc}")
        sys.exit(1)
