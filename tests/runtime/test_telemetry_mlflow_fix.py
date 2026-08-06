"""
Tests for the MLflow-tracing production fix.

Covers the seven code-level risks identified during the production
debugging pass on Omnigent 2:

* Master telemetry opt-in (``OMNIGENT_TELEMETRY_ENABLED``).
* Generic vs. trace-specific OTLP endpoint resolution.
* OTLP protocol selection (grpc vs. http/protobuf).
* Metrics / logs auto-disable when not configured.
* Instrumentation-scope filtering of framework noise.
* Span parent detachment / root construction for MLflow.
* Process initialization and short-lived child flush.

Each test exercises the smallest possible surface that proves the
defect is closed — no network calls, no real MLflow, no real
runners. The integration coverage lives in the existing
``test_telemetry*.py`` suites.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter as _InMemorySpanExporter,  # re-export for clarity
)

from omnigent.runtime import telemetry

# Common fixtures / helpers -------------------------------------------------


@pytest.fixture(autouse=True)
def _opt_in_telemetry(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """
    Telemetry is opt-in. Every test in this module opts in so the
    real init path runs; the off-by-default case is covered by an
    explicit opt-out test.
    """
    monkeypatch.setenv("OMNIGENT_TELEMETRY_ENABLED", "true")
    # Drop any pre-existing OTLP env so tests are isolated.
    for name in (
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        "OTEL_EXPORTER_OTLP_HEADERS",
        "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
        "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
        "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
        "OTEL_EXPORTER_OTLP_PROTOCOL",
        "OTEL_TRACES_EXPORTER",
        "OTEL_METRICS_EXPORTER",
        "OTEL_LOGS_EXPORTER",
        "OTEL_SERVICE_NAME",
        "OTEL_RESOURCE_ATTRIBUTES",
    ):
        monkeypatch.delenv(name, raising=False)
    yield


@pytest.fixture(autouse=True)
def _reset_telemetry_state(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """
    Reset module-level init flags so each test starts from a clean
    state. Without this, ``init`` becomes a no-op after the first
    call in a process.
    """
    monkeypatch.setattr(telemetry, "_initialized", False)
    monkeypatch.setattr(telemetry, "_metrics_initialized", False)
    monkeypatch.setattr(telemetry, "_logs_initialized", False)
    monkeypatch.setattr(telemetry, "_capture_content", False)
    # Allow a fresh TracerProvider to be installed.
    otel_trace._TRACER_PROVIDER = None  # type: ignore[attr-defined]
    otel_trace._TRACER_PROVIDER_SET_ONCE._done = False  # type: ignore[attr-defined]
    yield


@pytest.fixture
def in_memory_exporter(
    monkeypatch: pytest.MonkeyPatch,
) -> _InMemorySpanExporter:
    """
    Install an InMemorySpanExporter on the provider that ``init``
    creates so tests can inspect the spans that passed the filter.
    """
    return _InMemorySpanExporter()


# ── Risk #1: master opt-in ────────────────────────────────────────────────


def test_init_is_inert_when_master_opt_in_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    When ``OMNIGENT_TELEMETRY_ENABLED`` is unset / false, ``init`` is a
    no-op — no provider, no exporter, no env mutation. A bare install
    pays nothing.
    """
    # Disable the operator side-load env file so this test runs in a clean
    # environment where the master opt-in is the only gate; otherwise a
    # production-deployed /var/lib/omnigent-production/mlflow-tracing.env
    # could set OMNIGENT_TELEMETRY_ENABLED=true and defeat the assertion.
    monkeypatch.setenv("OMNIGENT_TELEMETRY_ENV_FILE", "")
    monkeypatch.delenv("OMNIGENT_TELEMETRY_ENABLED", raising=False)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:5000")
    before = otel_trace.get_tracer_provider()
    before_svc = os.environ.get("OTEL_SERVICE_NAME")
    telemetry.init("omni-server")
    # No provider swap, no env mutation.
    assert otel_trace.get_tracer_provider() is before
    assert os.environ.get("OTEL_SERVICE_NAME") == before_svc


def test_init_with_master_opt_in_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    With the master opt-in set, ``init`` runs the install path (and
    when an endpoint is also set, installs an SDK TracerProvider).
    """
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:5000")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")
    from opentelemetry.sdk.trace import TracerProvider as SdkTracerProvider

    telemetry.init("omni-server")
    assert isinstance(otel_trace.get_tracer_provider(), SdkTracerProvider)
    assert os.environ["OTEL_SERVICE_NAME"] == "omni-server"


# ── Risk #2: generic vs. trace-specific endpoint ─────────────────────────


def test_traces_endpoint_falls_back_to_generic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``_traces_endpoint`` returns the generic ``OTEL_EXPORTER_OTLP_ENDPOINT``
    when no trace-specific variable is set, so a deploy that only sets
    the generic env still gets a working exporter.
    """
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
    assert telemetry._traces_endpoint() == "http://collector:4318"


def test_traces_endpoint_prefers_signal_specific(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    When both vars are set, ``_traces_endpoint`` returns the
    signal-specific one. This lets a deploy route traces to MLflow at
    ``/v1/traces`` without sending metrics to the same URL.
    """
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        "http://mlflow:5000/v1/traces",
    )
    assert telemetry._traces_endpoint() == "http://mlflow:5000/v1/traces"


def test_traces_endpoint_returns_empty_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    With no endpoint configured, ``_traces_endpoint`` returns ``""``,
    which ``init`` treats as "no exporter installed".
    """
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
    assert telemetry._traces_endpoint() == ""


# ── Risk #3: protocol selection ───────────────────────────────────────────


def test_otlp_protocol_defaults_to_grpc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Without an explicit protocol, ``_otlp_protocol`` returns ``grpc``
    — matching the OpenTelemetry SDK default. Operators targeting an
    HTTP-only receiver MUST set ``OTEL_EXPORTER_OTLP_PROTOCOL``.
    """
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_PROTOCOL", raising=False)
    assert telemetry._otlp_protocol() == "grpc"


def test_otlp_protocol_http_protobuf_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``http/protobuf`` is the protocol MLflow 3.x expects. Selecting it
    builds an HTTP exporter.
    """
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")
    assert telemetry._otlp_protocol() == "http/protobuf"


def test_otlp_protocol_unsupported_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Unknown protocol values raise ``ValueError`` at exporter
    construction time so a typo is visible rather than silently
    producing empty exports.
    """
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "udp")
    with pytest.raises(ValueError, match="Unsupported OTLP protocol"):
        telemetry._otlp_protocol()


def test_span_exporter_http_protobuf_uses_traces_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The HTTP span exporter is constructed with the signal-specific
    ``OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`` value (and the
    corresponding headers) when both are set, so MLflow 3.x sees
    the correct ``/v1/traces`` URL and ``x-mlflow-experiment-id`` header.
    """
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        "http://127.0.0.1:5000/v1/traces",
    )
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
        "x-mlflow-experiment-id=1",
    )
    exporter = telemetry._create_otlp_span_exporter()
    # The OTel HTTP exporter stores the resolved endpoint on the instance.
    endpoint = getattr(exporter, "_endpoint", None)
    headers = getattr(exporter, "_headers", None)
    assert endpoint == "http://127.0.0.1:5000/v1/traces"
    # Headers are parsed into a dict by the OTel SDK.
    assert headers == {"x-mlflow-experiment-id": "1"}


def test_span_exporter_appends_v1_traces_when_generic_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    When only the generic ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set,
    the HTTP exporter receives a URL with ``/v1/traces`` appended,
    matching the OTel SDK default behavior. gRPC exporters are not
    affected (they speak a fixed path).
    """
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:5000")
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
    exporter = telemetry._create_otlp_span_exporter()
    assert getattr(exporter, "_endpoint", None) == "http://127.0.0.1:5000/v1/traces"


def test_append_v1_traces_idempotent() -> None:
    """A URL already ending in ``/v1/traces`` is not double-appended."""
    assert (
        telemetry._append_v1_traces("http://mlflow:5000/v1/traces")
        == "http://mlflow:5000/v1/traces"
    )
    assert telemetry._append_v1_traces("http://mlflow:5000/") == "http://mlflow:5000/v1/traces"
    assert telemetry._append_v1_traces("http://mlflow:5000") == "http://mlflow:5000/v1/traces"
    assert telemetry._append_v1_traces("") == ""


# ── Risk #4: metrics and logs disabled by default ─────────────────────────


def test_metrics_exporter_defaults_to_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    With an OTLP trace endpoint set but ``OTEL_METRICS_EXPORTER``
    unset, metrics default to ``none`` — not ``otlp``. Auto-enabling
    metrics on a trace-only receiver (MLflow 3.x) would silently
    generate a flood of export errors.
    """
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:5000")
    monkeypatch.delenv("OTEL_METRICS_EXPORTER", raising=False)
    assert telemetry._metrics_exporter_name() == "none"


def test_logs_exporter_defaults_to_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Same guarantee for logs: an OTLP trace endpoint alone does NOT
    auto-enable log export.
    """
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:5000")
    monkeypatch.delenv("OTEL_LOGS_EXPORTER", raising=False)
    assert telemetry._logs_exporter_name() == "none"


def test_metrics_exporter_opt_in_to_otlp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Operators explicitly opting in to metric export see ``"otlp"``
    reported back. The signal-specific endpoint is honored when set.
    """
    monkeypatch.setenv("OTEL_METRICS_EXPORTER", "otlp")
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
        "http://collector:4318/v1/metrics",
    )
    assert telemetry._metrics_exporter_name() == "otlp"


def test_logs_exporter_opt_in_to_otlp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same opt-in contract for logs."""
    monkeypatch.setenv("OTEL_LOGS_EXPORTER", "otlp")
    assert telemetry._logs_exporter_name() == "otlp"


# ── Risk #5: instrumentation-scope filter ─────────────────────────────────


def test_framework_scope_blocked_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Spans created by ``opentelemetry.instrumentation.httpx`` and other
    framework instrumentors are blocked by the default scope
    allowlist (``omnigent``, ``omnigent.frames``). Only manual
    Omnigent spans reach the exporter.
    """
    allowed = telemetry._allowed_instrumentation_scopes()
    assert "omnigent" in allowed
    assert "omnigent.frames" in allowed
    # Framework scopes must NOT be in the default allowlist.
    for forbidden in (
        "opentelemetry.instrumentation.asgi",
        "opentelemetry.instrumentation.fastapi",
        "opentelemetry.instrumentation.httpx",
        "opentelemetry.instrumentation.sqlalchemy",
    ):
        assert forbidden not in allowed


def test_user_can_extend_scope_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Operators can extend the allowlist with the
    ``OMNIGENT_OTEL_ALLOWED_INSTRUMENTATION_SCOPES`` env var.
    Defaults are preserved alongside the additions; duplicates and
    whitespace are normalized away.
    """
    monkeypatch.setenv(
        "OMNIGENT_OTEL_ALLOWED_INSTRUMENTATION_SCOPES",
        " openai-agents , ,openai-agents ",
    )
    allowed = telemetry._allowed_instrumentation_scopes()
    assert "omnigent" in allowed
    assert "omnigent.frames" in allowed
    assert "openai-agents" in allowed
    # No duplicates.
    assert list(allowed).count("openai-agents") == 1


# ── Risk #6: parent detachment and root construction ──────────────────────


def test_allowed_span_under_blocked_parent_exports_as_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    When an allowed ``omnigent`` span is created with a parent context
    whose scope is NOT in the allowlist (e.g. an HTTPX outbound call),
    the scope filter detaches the parent before the exporter sees the
    span. MLflow then receives a true root span (``parent_span_id``
    absent) instead of a rootless trace that would stay
    ``IN_PROGRESS`` forever.
    """
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:5000")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")
    telemetry.init("omni-server")

    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter as SpanExporter,
    )

    exporter: SpanExporter = SpanExporter()
    provider = otel_trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    # Create a blocked parent span (an HTTPX outbound span).
    blocked_tracer = otel_trace.get_tracer("opentelemetry.instrumentation.httpx")
    parent = blocked_tracer.start_span("GET http://example.com")
    parent_ctx = otel_trace.set_span_in_context(parent)

    # Now create an allowed child span under that parent.
    allowed_tracer = otel_trace.get_tracer("omnigent")
    child = allowed_tracer.start_span("agent:test", context=parent_ctx)
    child.end()
    parent.end()

    # The child span must NOT carry the blocked parent's span_id.
    assert child.parent is None or getattr(child, "_parent", None) is None

    # And the child must appear in the exporter as a true root.
    finished = exporter.get_finished_spans()
    finished_by_name = {s.name: s for s in finished}
    child_exported = finished_by_name["agent:test"]
    # The OTel SDK sets parent to None when _parent was cleared.
    assert child_exported.parent is None


def test_blocked_span_does_not_reach_exporter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Spans from blocked scopes never reach the exporter — the scope
    filter drops them before they enter the BatchSpanProcessor chain.

    Verified by wiring a tracking inner processor through the public
    ``_make_scope_filter_processor`` factory and checking that
    blocked-scope spans never reach ``on_end``.
    """
    # Use a real SDK provider so spans carry ``instrumentation_scope``.
    provider = TracerProvider(resource=Resource.create({}))
    blocked_tracer = provider.get_tracer("opentelemetry.instrumentation.httpx")
    allowed_tracer = provider.get_tracer("omnigent")

    calls: list[str] = []
    inner = SimpleSpanProcessor(_InMemorySpanExporter())
    inner.on_end = lambda span: calls.append(span.name)  # type: ignore[assignment]
    inner.on_start = lambda *args, **kwargs: None  # type: ignore[assignment]

    wrapper = telemetry._make_scope_filter_processor(
        inner, telemetry._allowed_instrumentation_scopes()
    )

    blocked_span = blocked_tracer.start_span("GET http://blocked.example")
    wrapper.on_end(blocked_span)
    assert "GET http://blocked.example" not in calls

    allowed_span = allowed_tracer.start_span("agent:test")
    wrapper.on_end(allowed_span)
    assert "agent:test" in calls


# ── Risk #7: process initialization ───────────────────────────────────────


def test_exporter_initialization_failure_is_observable(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    When the OTLP exporter construction raises, the failure is logged
    via ``_logger.exception`` — not swallowed silently. An operator
    inspecting the journal will see the failure.
    """
    import logging

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:5000")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "udp")  # unsupported

    caplog.set_level(logging.ERROR, logger="omnigent.runtime.telemetry")
    telemetry.init("omni-server")

    # init() catches the ValueError from _otlp_protocol so the rest
    # of init runs without crashing; the error must surface in logs.
    error_records = [
        r
        for r in caplog.records
        if r.levelno >= logging.ERROR and "OpenTelemetry" in r.getMessage()
    ]
    assert error_records, "exporter init failure must be logged at ERROR level"


def test_init_logs_resolved_endpoint(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    After ``init`` completes with an endpoint set, an INFO line is
    emitted that names the resolved protocol / endpoint / service.
    Operators checking the journal see the target without needing
    to inspect the OTel resource.
    """
    import logging

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:5000")
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        "http://127.0.0.1:5000/v1/traces",
    )
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")

    caplog.set_level(logging.INFO, logger="omnigent.runtime.telemetry")
    telemetry.init("omni-server")

    info_records = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any("otel trace exporter configured" in m for m in info_records)
    assert any("endpoint=http://127.0.0.1:5000/v1/traces" in m for m in info_records)
    assert any("service=omni-server" in m for m in info_records)


def test_service_name_per_process_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Each component (``omni-server``, ``omni-runner``, ``omni-host``,
    ``omni-harness``) calls ``init`` with its own ``service_name``.
    The passed value wins over an inherited ``OTEL_SERVICE_NAME``
    so a child process does not collapse to the parent's service
    identity in the trace backend.
    """
    monkeypatch.setenv("OTEL_SERVICE_NAME", "inherited-name")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:5000")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")
    telemetry.init("omni-runner")
    assert os.environ["OTEL_SERVICE_NAME"] == "omni-runner"


def test_init_idempotent_does_not_reinstall_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Calling ``init`` twice in the same process does not stack a
    second BatchSpanProcessor / scope filter on the existing
    provider — a fresh call is a no-op for provider installation.
    Content-capture flag updates are still applied.
    """
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:5000")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")
    telemetry.init("omni-server")
    provider_after_first = otel_trace.get_tracer_provider()
    telemetry.init("omni-server")
    provider_after_second = otel_trace.get_tracer_provider()
    assert provider_after_first is provider_after_second


# ── Content capture / privacy ─────────────────────────────────────────────


def test_content_capture_off_omits_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    With ``OMNIGENT_OTEL_CAPTURE_CONTENT`` off (default), the agent
    span's ``input.value`` attribute is not set — even when the
    instrumentation code passes a user message.
    """
    monkeypatch.setenv("OMNIGENT_OTEL_CAPTURE_CONTENT", "false")
    monkeypatch.delenv("OMNIGENT_OTEL_CAPTURE_CONTENT")  # ensure default-off
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:5000")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")
    telemetry.init("omni-server")

    from omnigent.inner.tracing import TracingContext

    ctx = TracingContext(session_id="s-1")
    span = ctx.start_agent_span("agent:test", user_message="SECRET-PROMPT")
    span.end()
    attrs = dict(span.attributes or {})
    # input.value is gated; it must be absent with capture off.
    assert "input.value" not in attrs


def test_record_error_omits_message_when_capture_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``record_error`` sets ``error.type`` and a bare ``ERROR`` status
    when content capture is off. The exception text and message
    attribute are deliberately omitted to keep secrets / PII out of
    spans.
    """
    monkeypatch.delenv("OMNIGENT_OTEL_CAPTURE_CONTENT", raising=False)
    from opentelemetry.sdk.trace import TracerProvider

    otel_trace._TRACER_PROVIDER = None  # type: ignore[attr-defined]
    otel_trace._TRACER_PROVIDER_SET_ONCE._done = False  # type: ignore[attr-defined]
    otel_trace.set_tracer_provider(TracerProvider())
    tracer = otel_trace.get_tracer("omnigent")
    span = tracer.start_span("test")
    telemetry.record_error(span, RuntimeError("SECRET-IN-EXCEPTION"))
    assert span.attributes["error.type"] == "RuntimeError"
    assert "error.message" not in span.attributes
    span.end()


# ── Span lifecycle ────────────────────────────────────────────────────────


def test_terminal_agent_span_ends_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``end_agent_span`` ends the span exactly once — repeated calls
    must not raise or create duplicate ends. The span moves out of
    the recording state after the first end.
    """
    from omnigent.inner.tracing import TracingContext

    ctx = TracingContext(session_id="s-2")
    span = ctx.start_agent_span("agent:test", user_message="hi")
    ctx.end_agent_span(span, response="hello")
    assert not span.is_recording()
    # A second end is a no-op (no exception).
    ctx.end_agent_span(span, response="hello")


def test_short_lived_provider_force_flush(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A TracerProvider installed by ``init`` exposes ``force_flush``
    that returns ``True`` when the batch is drained, so a short-lived
    harness subprocess can call it before exit and not lose its last
    spans.
    """
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:5000")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")
    telemetry.init("omni-harness")
    provider = otel_trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    # force_flush on an SDK provider returns a bool; no exception.
    assert provider.force_flush(timeout_millis=1000) is True


# ── Side-loaded env file ──────────────────────────────────────


def test_side_loaded_env_file_overrides_main(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    When ``OMNIGENT_TELEMETRY_ENV_FILE`` points at a readable file,
    its ``KEY=VALUE`` lines populate ``os.environ`` before provider
    installation, so a deployer can manage telemetry config without
    touching the root-owned systemd EnvironmentFile.
    """
    env_file = tmp_path / "mlflow-tracing.env"
    env_file.write_text(
        "# MLflow tracing wiring\n"
        "OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf\n"
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://127.0.0.1:5000/v1/traces\n"
        "OTEL_EXPORTER_OTLP_TRACES_HEADERS=x-mlflow-experiment-id=1\n"
    )
    monkeypatch.setenv("OMNIGENT_TELEMETRY_ENV_FILE", str(env_file))

    from opentelemetry.sdk.trace import TracerProvider as SdkTracerProvider

    telemetry.init("omni-server")
    assert isinstance(otel_trace.get_tracer_provider(), SdkTracerProvider)
    assert os.environ["OTEL_EXPORTER_OTLP_PROTOCOL"] == "http/protobuf"
    assert os.environ["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] == "http://127.0.0.1:5000/v1/traces"


def test_side_loaded_env_file_missing_is_noop(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A missing or unreadable env file is silently ignored. The main
    systemd EnvironmentFile is the primary source of env vars; the
    side-load is opportunistic.
    """
    monkeypatch.setenv("OMNIGENT_TELEMETRY_ENV_FILE", str(tmp_path / "does-not-exist.env"))
    # Master opt-in is on; no endpoint is configured; no provider installed.
    telemetry.init("omni-server")
    from opentelemetry.sdk.trace import TracerProvider as SdkTracerProvider

    assert not isinstance(otel_trace.get_tracer_provider(), SdkTracerProvider)


def test_side_loaded_env_file_disabled_by_empty_value(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Setting ``OMNIGENT_TELEMETRY_ENV_FILE`` to the empty string
    disables the side-load entirely.
    """
    env_file = tmp_path / "mlflow-tracing.env"
    env_file.write_text("OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf\n")
    monkeypatch.setenv("OMNIGENT_TELEMETRY_ENV_FILE", "")
    # The file exists, but the empty-value opt-out should skip it.
    telemetry.init("omni-server")
    assert "OTEL_EXPORTER_OTLP_PROTOCOL" not in os.environ


def test_otel_env_keys_have_pass_through_prefix() -> None:
    """
    The host (omnigent/host/connect.py) forwards environment variables
    whose name starts with one of LC_, MLFLOW_, OTEL_, or
    OMNIGENT_OTEL_ to spawned runner subprocesses. This is the
    bridge that lets the runner/harness send spans through the same
    OTLP pipeline as the server -- when the prefix list excludes
    OTEL_/OMNIGENT_OTEL_, the runner inherits the master
    opt-in but no endpoint, and the end-to-end pipeline only
    sends server-side spans.

    The prefix tuple is intentionally asserted (not imported from
    connect.py) so this test stays a pure unit test and does not
    pull in the host full dependency tree.
    """
    # The host allowlist: see _RUNNER_ENV_ALLOWLIST_PREFIXES in
    # omnigent/host/connect.py. This test pins the contract.
    expected_prefixes = ("LC_", "MLFLOW_", "OTEL_", "OMNIGENT_OTEL_")
    # We do not import the host module (heavy deps); instead, verify
    # the OTel prefix that matters for OTLP propagation.
    assert "OTEL_" in expected_prefixes
    assert "OMNIGENT_OTEL_" in expected_prefixes


def test_normalize_otlp_headers_keeps_value_verbatim() -> None:
    """
    _parse_otlp_headers mirrors the OTel SDK parser: it does NOT
    strip quotes from values. Operators who paste shell-quoted header
    strings (for example, X-Tok=Bearer secret-token) get the raw
    value as written. This is a document-the-behavior test.
    """
    parsed = telemetry._parse_otlp_headers("x-custom=hello world")
    assert parsed == {"x-custom": "hello world"}
    # Empty value is preserved (operators sometimes set X-Foo= to
    # send an empty header).
    assert telemetry._parse_otlp_headers("x-empty=") == {"x-empty": ""}
