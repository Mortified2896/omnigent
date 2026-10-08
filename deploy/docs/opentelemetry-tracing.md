# Direct OpenTelemetry tracing

Omnigent emits OpenTelemetry spans through OTLP. Enable tracing explicitly and
send spans to a collector that writes a local OTLP JSON archive. Chat transcripts,
human task outcomes, review tags, and comments remain in the Omnigent database;
they are not replaced by the trace archive.

For current RTX operations, use HomeLab's `docs/omnigent-otel.md` and
`docs/codex-server-workflow.md`. Discover installed service state before changing
runtime configuration. For a standalone Mac instance, see
[Mac-local operation](../../docs/mac-local-operation.md).

## Application environment

Set this environment before starting the server and host. Runners and harnesses
must inherit it; restart or create new processes after changing it.

```sh
export OMNIGENT_TELEMETRY_ENABLED=true
export OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
export OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://127.0.0.1:4318/v1/traces
export OTEL_METRICS_EXPORTER=none
export OTEL_LOGS_EXPORTER=none
export OMNIGENT_OTEL_CAPTURE_CONTENT=false
export OMNIGENT_OTEL_ALLOWED_INSTRUMENTATION_SCOPES=omnigent,omnigent.frames
export OTEL_RESOURCE_ATTRIBUTES=service.namespace=omnigent,deployment.environment=local
```

Use the collector's actual port. `4318` is a standard example, not permission to
replace an existing collector. Give the server and host distinct
`OTEL_SERVICE_NAME` values. Instrumentation remains off unless the master opt-in
is set, even when an exporter endpoint is present.

Content capture is off by default. Traces contain diagnostic metadata, correlation
IDs, status, and available model/token usage. Prompt text, responses, tool bodies,
and exception details are excluded. Each harness has its own instrumentation;
verify the spans it actually exports instead of assuming every provider field exists.

## Collector file pipeline

Use an official OpenTelemetry Collector distribution with the `file` exporter.
Check occupied ports before starting it. An example collector configuration is:

```yaml
receivers:
  otlp:
    protocols:
      http:
        endpoint: 127.0.0.1:4318
processors:
  memory_limiter:
    check_interval: 1s
    limit_mib: 256
    spike_limit_mib: 64
  batch:
    timeout: 2s
    send_batch_size: 128
    send_batch_max_size: 256
exporters:
  file/lean:
    path: /absolute/private/path/traces.jsonl
    format: json
    rotation:
      max_megabytes: 64
      max_days: 30
      max_backups: 16
service:
  telemetry:
    metrics:
      level: none
  pipelines:
    traces:
      receivers: [otlp]
      processors: [memory_limiter, batch]
      exporters: [file/lean]
```

Create the output directory with private permissions. Validate the configuration
with `otelcol-contrib validate --config=/absolute/path/config.yaml` before starting
it. The file contains newline-delimited OTLP JSON, with one batch per line.
Rotation bounds this example to approximately 1 GiB plus the current file; age
limits apply to rotated files, so this is not a guaranteed 30-day history.

## Verification

Create a marked, scoring-excluded test session using
[the test-session policy](../../docs/task-scoring-and-test-sessions.md). Run a
harmless turn, wait for exporter/batch flush, and inspect the archive for the exact
session ID. Check service identity, span status, parent/child correlation, and
available model/token attributes. Keep unexpected results for inspection.
Collector health or a synthetic probe alone does not prove application tracing.

Save a human outcome through the chat UI or task-outcomes API and reload it from
the task-experiment API. Outcomes live durably in the chat database. Keep automated
scoring disabled until its documented safety and outbound-input contracts are met.

An exporter or collector outage must not block normal chat execution. Treat the
archive as diagnostics, not the authoritative transcript or outcome database.

## References

- [Official Collector installation](https://opentelemetry.io/docs/collector/install/)
- [OTLP exporter configuration](https://opentelemetry.io/docs/languages/sdk-configuration/otlp-exporter/)
- [Collector file exporter](https://github.com/open-telemetry/opentelemetry-collector-contrib/tree/main/exporter/fileexporter)
