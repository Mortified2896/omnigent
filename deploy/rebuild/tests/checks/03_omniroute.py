#!/usr/bin/env python3
# ─────────────────────────────────────────────────────────────────────
# Check 3 — Real OmniRoute-routed request with requested and
#             executed model provenance
# ─────────────────────────────────────────────────────────────────────
#
# Sends a real /v1/route POST against the deployed OmniRoute
# gateway. Verifies the response carries the canonical OmniRoute
# provenance headers and a non-empty provider+model pairing.
#
# Requires: python3 (stdlib only — no third-party deps).
#
# Inputs (env vars):
#   OMNIRoute_BASE_URL   the OmniRoute gateway's base URL, e.g.
#                        https://omniroute.example.com/v1
#   OMNIRoute_AUTH_TOKEN the bearer token (default: read from
#                        OMNIROUTE_API_KEY env var)
#   OMNIRoute_ROUTER_NAME  the router name to address
#
# Exit: 0 on PASS, 1 on FAIL. Prints a structured JSON line on
# stdout: {"status": "PASS"|"FAIL", "evidence": {...}}

from __future__ import annotations

import json
import os
import sys
import urllib.request
import urllib.error
from typing import Any


def _env(name: str, default: str | None = None) -> str | None:
    val = os.environ.get(name)
    if val is not None:
        return val
    return default


def _post_json(url: str, payload: dict[str, Any], headers: dict[str, str]) -> tuple[int, dict[str, str], bytes]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read() or b""


def main() -> int:
    base = _env("OMNIROUTE_BASE_URL")
    token = _env("OMNIROUTE_AUTH_TOKEN") or _env("OMNIROUTE_API_KEY")
    router = _env("OMNIROUTE_ROUTER_NAME", "omniroute")

    if not base:
        emit("FAIL", reason="OMNIROUTE_BASE_URL not set")
        return 1
    if not token:
        emit("FAIL", reason="OMNIROUTE_AUTH_TOKEN (or OMNIROUTE_API_KEY) not set")
        return 1

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    payload = {
        "router_name": router,
        "message": "trivial echo: respond with a single sentence.",
        "available_models": [
            "omniroute/auto",
            "anthropic/claude-opus-4-8",
            "openai/gpt-4o-mini",
        ],
    }

    status, resp_headers, body = _post_json(f"{base.rstrip('/')}/routes:select", payload, headers)
    if status != 200:
        emit("FAIL", reason=f"OmniRoute POST /routes:select returned status={status} body={body[:200]!r}")
        return 1

    # Required response headers (canonical OmniRoute provenance).
    canonical_headers = {
        "x-omniroute-request-id",
        "x-omniroute-decision-id",
        "x-omniroute-selected-provider",
        "x-omniroute-selected-model",
        "x-omniroute-requested-model",
    }
    header_keys_lower = {k.lower(): v for k, v in resp_headers.items()}
    missing = [h for h in canonical_headers if h not in header_keys_lower]
    if missing:
        emit("FAIL", reason=f"OmniRoute response missing required headers: {sorted(missing)}; got: {sorted(header_keys_lower)}")
        return 1

    try:
        body_json = json.loads(body)
    except json.JSONDecodeError as exc:
        emit("FAIL", reason=f"OmniRoute response not valid JSON: {exc}; body={body[:200]!r}")
        return 1

    provider = body_json.get("provider") or header_keys_lower["x-omniroute-selected-provider"]
    model = body_json.get("model") or header_keys_lower["x-omniroute-selected-model"]
    decision_id = body_json.get("decision_id") or header_keys_lower["x-omniroute-decision-id"]

    if not provider or not model or not decision_id:
        emit("FAIL", reason=f"OmniRoute response missing provider/model/decision_id: body={body_json} headers={dict(header_keys_lower)}")
        return 1

    # Determinism / cacheability check: same prompt + same catalog +
    # same session window should produce the same decision_id.
    status2, _resp_headers2, body2 = _post_json(f"{base.rstrip('/')}/routes:select", payload, headers)
    if status2 != 200:
        emit("FAIL", reason=f"OmniRoute determinism POST returned status={status2} body={body2[:200]!r}")
        return 1
    try:
        body_json2 = json.loads(body2)
    except json.JSONDecodeError as exc:
        emit("FAIL", reason=f"OmniRoute determinism response not valid JSON: {exc}")
        return 1
    decision_id_2 = body_json2.get("decision_id") or _resp_headers2.get("x-omniroute-decision-id")
    if decision_id_2 != decision_id:
        emit(
            "FAIL",
            reason=f"OmniRoute non-deterministic: decision_id changed between identical POSTs ({decision_id} -> {decision_id_2})",
        )
        return 1

    emit(
        "PASS",
        provider=provider,
        model=model,
        decision_id=decision_id,
        requested_model=header_keys_lower["x-omniroute-requested-model"],
        fallback_used=header_keys_lower.get("x-omniroute-fallback-used"),
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
    except BaseException as exc:
        # Guarantee a structured FAIL line on the canary runner's
        # stdout, even when an unexpected exception escapes main().
        emit("FAIL", reason=f"unhandled exception: {type(exc).__name__}: {exc}")
        sys.exit(1)