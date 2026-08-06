# MLflow Tracing Deployment Guide

This document describes how to wire Omnigent's OpenTelemetry tracing into a local
MLflow 3.x server for production observability of agent turns, tool calls, policy
evaluations, and LLM calls. The same configuration works against any OTLP-compatible
collector — only the endpoint and protocol differ.

## When you need this

Tracing is **opt-in**. By default Omnigent installs no provider, no instrumentation,
and no exporter — bare installs incur zero telemetry cost. To enable:

1. Set `OMNIGENT_TELEMETRY_ENABLED=true` in the production environment.
2. Configure an OTLP trace endpoint and headers (see below).
3. Restart the server, host, and any running runners so each process picks up the env
   vars on init.

The OpenTelemetry exporter reads the same env vars across the server, runner, harness
and host processes — no per-component wiring is needed beyond copying the variables
into `omnigent.env`.

## Protocol and path

MLflow 3.x (and the deployed `ghcr.io/mlflow/mlflow:v3.8.1` image in particular)
exposes OpenTelemetry **HTTP/protobuf** ingestion at:

```
POST /v1/traces
```

It does **not** expose a gRPC OTLP port. The OpenTelemetry HTTP exporter appends
`/v1/traces` to the configured endpoint, so either of the following works:

* `OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:5000` (generic; the HTTP exporter
  appends `/v1/traces`)
* `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://127.0.0.1:5000/v1/traces` (explicit
  signal-specific form)

The signal-specific form is preferred for managed deployments because it lets you
target trace-specific authentication and routing without affecting metrics or logs.

Set the protocol explicitly so the OTel default of `grpc` does not silently produce
empty exports against an HTTP-only receiver:

```bash
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
```

## Authentication and experiment routing

MLflow routes traces to the experiment named in the `x-mlflow-experiment-id` header.
Set this in `OTEL_EXPORTER_OTLP_TRACES_HEADERS` so the trace exporter attaches it
to every batch it sends:

```bash
OTEL_EXPORTER_OTLP_TRACES_HEADERS=x-mlflow-experiment-id=1
```

To target a different experiment, look up its id with
`curl http://127.0.0.1:5000/api/2.0/mlflow/experiments/search` (returns the
experiment id) and substitute it. **Do not hardcode the experiment id into the
application code** — keep it in the deployment env so the same wheel can be promoted
across environments.

## What gets exported

By default, the scope allowlist is `("omnigent", "omnigent.frames")`. Only spans
emitted from these instrumentor scopes reach MLflow:

* `omnigent` — `agent:*`, `tool:*`, `policy:*` spans from `omnigent.inner.tracing`.
* `omnigent.frames` — inter-process frame spans (host � runner ↔ server).

The following are **deliberately blocked** to keep traces compact and reviewable:

* `opentelemetry.instrumentation.asgi` — FastAPI server request/response spans.
* `opentelemetry.instrumentation.fastapi` — same, FastAPI-specific.
* `opentelemetry.instrumentation.httpx` — outbound HTTP client spans.
* `opentelemetry.instrumentation.sqlalchemy` — SQL statement spans.

If a future feature needs additional spans, append the scope name to
`OMNIGENT_OTEL_ALLOWED_INSTRUMENTATION_SCOPES` (comma-separated). Do **not** remove
the defaults — they are the smallest set that carries the full agent-turn tree.

LLM-call spans originate inside the executor SDK (e.g. Claude Agent SDK, OpenAI
Agents SDK, Pi native). The SDK's own instrumentation scope may appear under one of
those names — extend the allowlist to capture them, then verify in MLflow that the
spans carry the expected model / token-usage attributes.

## Metrics and logs

MLflow 3.x only ingests traces via OTLP. Metrics and logs are silently dropped.
Omnigent defaults metrics and logs exporters to `none` to avoid sending unsupported
signals; set `OTEL_METRICS_EXPORTER=none` and `OTEL_LOGS_EXPORTER=none` explicitly in
the env to make the disablement visible.

If you swap MLflow for a stack that *does* support metrics / logs (Tempo, Grafana,
Datadog, Honeycomb), set `OTEL_METRICS_EXPORTER=otlp` and `OTEL_LOGS_EXPORTER=otlp`
and provide the relevant `OTEL_EXPORTER_OTLP_METRICS_ENDPOINT` /
`OTEL_EXPORTER_OTLP_LOGS_ENDPOINT` (and headers).

## Content capture

`OMNIGENT_OTEL_CAPTURE_CONTENT` is **off by default**. With it off, no prompt or
response text, no tool result body, no error description, and no exception stack
trace is recorded on a span — only metadata (model id, token counts, status code,
session id). Operators who need the full trace body for debugging can flip it on
temporarily, but production should leave it off.

## Recommended env snippet

For a production Omnigent instance tracing to a local MLflow server:

```bash
OMNIGENT_TELEMETRY_ENABLED=true
OTEL_SERVICE_NAME=omni-server
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://127.0.0.1:5000/v1/traces
OTEL_EXPORTER_OTLP_TRACES_HEADERS=x-mlflow-experiment-id=1
OTEL_METRICS_EXPORTER=none
OTEL_LOGS_EXPORTER=none
OMNIGENT_OTEL_CAPTURE_CONTENT=false
OMNIGENT_OTEL_ALLOWED_INSTRUMENTATION_SCOPES=omnigent,omnigent.frames
OTEL_RESOURCE_ATTRIBUTES=service.namespace=omnigent,deployment.environment=production
```

### Side-loaded env file (recommended for production)

When the systemd ``EnvironmentFile`` is root-owned and read-only
(the production container layout at
``/etc/omnigent-production/omnigent.env``), operators can drop
telemetry configuration into a separate writable file at
``/var/lib/omnigent-production/mlflow-tracing.env``. The
``omnigent.runtime.telemetry`` module loads this file on every
``init()`` call, after the systemd ``EnvironmentFile`` has been
processed and before any provider is installed.

Existing ``os.environ`` values are not overwritten — only set when
absent — so a process inheriting an env var from its parent still
takes precedence over the file.

The runners and harnesses inherit `OTEL_EXPORTER_OTLP_*` via the
`OMNIGENT_RUNNER_ENV_PASSTHROUGH` allowlist prefix in
`omnigent/host/connect.py`, so the same env block on the server and host covers all
four processes (server, host, runner, harness).

## Verifying it works

After restarting the production services, watch the server log for two INFO lines
from `omnigent.runtime.telemetry`:

```
otel trace scope filter active: allowed=omnigent,omnigent.frames
otel trace exporter configured: protocol=http/protobuf endpoint=http://127.0.0.1:5000/v1/traces service=omni-server
omnigent telemetry initialized (endpoint=http://127.0.0.1:5000/v1/traces, capture_content=False)
```

Then open the MLflow UI at `http://127.0.0.1:5000/` and confirm a new trace appears
under the configured experiment after a real session runs.

If no traces appear, check:

1. The master opt-in: `OMNIGENT_TELEMETRY_ENABLED=true` in the *effective* env of
   every process (use `/proc/<PID>/environ` to verify after start).
2. The OTLP protocol: MLflow 3.x is HTTP/protobuf only.
3. The header: `x-mlflow-experiment-id` must point to an existing experiment.
4. The MLflow container logs for `POST /v1/traces` requests.
