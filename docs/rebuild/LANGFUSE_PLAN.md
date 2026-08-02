# Langfuse integration plan

## Goal

Trace every Omnigent session through Langfuse via OpenTelemetry — no custom
spans, no re-implementation, no core patches — and keep the master
opt-in / opt-out switch invisible to the rest of the runtime.

## 1. Why OpenTelemetry, not the Langfuse SDK

Omnigent v0.7.0 ships a full OTel implementation in
`omnigent/runtime/telemetry.py` (1 155 LOC). The plan is "point Omnigent's
exporter at Langfuse", not "wrap Langfuse around Omnigent". The OTel
exporter is the only component that needs to know Langfuse exists.

Langfuse exposes an OTel-compatible endpoint at
`https://cloud.langfuse.com/api/public/otel` and accepts both gRPC and
HTTP/protobuf. Authentication is `Authorization: Basic <base64(public:secret)>`
in `OTEL_EXPORTER_OTLP_HEADERS`. No Langfuse SDK is required.

## 2. Master opt-in / opt-out switch

`omnigent/runtime/telemetry.py:telemetry_enabled()` reads
`OMNIGENT_TELEMETRY_ENABLED` (default `False`):

```python
def telemetry_enabled() -> bool:
    return _env_bool("OMNIGENT_TELEMETRY_ENABLED")
```

Every instrumentation site (`_init_otel_traces`, `_init_otel_metrics`,
`_init_otel_logs`, `instrument_fastapi_app`, `_instrument_httpx`,
`instrument_httpx_client`, `instrument_sqlalchemy_engine`, `record_message_payload`)
short-circuits when `not telemetry_enabled()`. Setting
`OMNIGENT_TELEMETRY_ENABLED=false` (or leaving it unset) makes the entire
layer a no-op with zero overhead. The Control-Room systemd unit's
environment file (`/etc/omnigent/control-room.env`) is the single
authority for this switch.

## 3. Enablement

```bash
# /etc/omnigent/control-room.env
OMNIGENT_TELEMETRY_ENABLED=true
OTEL_EXPORTER_OTLP_ENDPOINT=https://cloud.langfuse.com/api/public/otel
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
OTEL_EXPORTER_OTLP_HEADERS=Authorization=Basic <base64(lf_pk_public:lf_sk_secret)>
OTEL_SERVICE_NAME=omni-control-room-web
OMNIGENT_OTEL_FASTAPI_INSTRUMENTATION=true
OMNIGENT_OTEL_HTTP_CLIENT_INSTRUMENTATION=true
OMNIGENT_OTEL_CAPTURE_CONTENT=false  # never in prod
```

The same envs apply to the host daemon (one `OTEL_SERVICE_NAME` per
component: `omni-control-room-host`).

## 4. Span hierarchy

Omnigent's `telemetry.py` already provides the structural pieces; the
Langfuse adapter only has to surface the right attributes per the
OmniRoute provenance spec.

```
<OTel root span "session.{session_id}">
  ├── session.id  (set by _SessionIdSpanProcessor; upstream)
  ├── omniroute.requested.model
  ├── omniroute.executed.provider
  ├── omniroute.executed.model
  ├── omniroute.decision.id
  ├── omniroute.request.id
  ├── omniroute.route_approval.state        (approved|pending|denied|default)
  ├── omniroute.fallback.used
  ├── omniroute.billing.class
  ├── omniroute.reasoning.effort
  ├── agent.name                             (parent's canonical agent_name)
  ├── harness.name                           (claude-sdk|pi|opencode-native|...)
  ├── worktree.branch
  ├── worktree.path
  ├── session.child.id                       (for child sessions)
  ├── session.parent.id                      (for child sessions)
  ├── git.commit.sha                         (the commit the worker branched from)
  ├── test.result                            (for delegated work)
  ├── test.summary                           (e.g. "47 passed, 1 failed")
  ├── git.commit.outcome                     (worker commit SHA after push)
  ├── git.pr.url
  ├── review.verdict                         (cross-vendor review)
  └── review.outcome
  │
  ├── [child span: omnigent.tool]
  │     ├── gen_ai.tool.name
  │     ├── gen_ai.tool.args  (omitted when OMNIGENT_OTEL_CAPTURE_CONTENT=false)
  │     └── gen_ai.tool.result
  │
  ├── [child span: omnigent.llm.request]
  │     ├── gen_ai.system
  │     ├── gen_ai.request.model
  │     ├── gen_ai.response.model
  │     ├── gen_ai.usage.input_tokens
  │     ├── gen_ai.usage.output_tokens
  │     └── omniroute.executed.model
  │
  ├── [child span: omnigent.subagent.child]
  │     ├── session.parent.id
  │     ├── session.child.id
  │     └── omnigent.child.worktree.branch
  │
  └── [child span: omnigent.test.run]
        ├── gen_ai.tool.name
        ├── test.result
        └── test.summary
```

Every session-level attribute is set by the Control-Room layer through
the existing `set_session_id(session_id)` + `record_message_payload(...)`
API in `omnigent/runtime/telemetry.py`. No upstream change is needed.

## 5. Secret filter

`omnigent/runtime/telemetry.py:_redact_payload` already replaces secret-looking
keys with `[redacted]`:

```python
_SECRET_KEYS = frozenset({
    "secret", "authorization", "api_key", "auth", "password", "passwd",
    "credentials", "binding_token", "access_token", "refresh_token",
    "*_token", "*_secret", "x-omniroute-fallback-used",  # don't leak route decision
})
```

`OMNIGENT_OTEL_CAPTURE_CONTENT=false` is the default in the control-room
environment. When it is `true` (dev only), the upstream redactor is
additionally filtered against `deploy/control-room/langfuse/deny-headers.txt`
(one header per line, glob syntax), so a deployment can strip
deployment-specific secrets without forking upstream.

## 6. Disable switch (no code change)

| Action | Behavior |
| --- | --- |
| Unset `OMNIGENT_TELEMETRY_ENABLED` | Layer is a no-op (default). |
| Set `OMNIGENT_TELEMETRY_ENABLED=false` | Layer is a no-op. |
| Unset `OTEL_EXPORTER_OTLP_ENDPOINT` | `_init_otel_traces` early-returns; span export is disabled. |
| Set `OMNIGENT_OTEL_FASTAPI_INSTRUMENTATION=false` | `instrument_fastapi_app` no-ops. |
| Set `OMNIGENT_OTEL_HTTP_CLIENT_INSTRUMENTATION=false` | `_instrument_httpx` no-ops. |
| Set `OMNIGENT_OTEL_CAPTURE_CONTENT=false` | `record_message_payload` short-circuits; spans carry no message bodies. |

Every line above is a one-line env var change; **no code change**.

## 7. Acceptance

Phase 8 acceptance test #10 ("Langfuse receives the expected trace
hierarchy") is satisfied by:

- A unit test in `deploy/control-room/tests/test_langfuse_enablement.py`
  that points a `BatchSpanProcessor` at an in-process `InMemorySpanExporter`,
  runs a synthetic session, and asserts the hierarchy above.
- An end-to-end test in `deploy/control-room/tests/test_langfuse_acceptance.py`
  that runs against the public Langfuse sandbox and asserts the same
  hierarchy after the span exporter flushes.
