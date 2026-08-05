"""
Regression tests for the telemetry NameError fix.

The canonical commit ``f6cb65b8`` introduced instrumentation-scope
filtering but accidentally dropped the ``def _fastapi_instrumentation_enabled(...)``
line, so ``instrument_fastapi_app`` referenced an undefined name and
the server crashed on startup with ``NameError: name
'_fastapi_instrumentation_enabled' is not defined``. These tests
exercise every behaviour the missing helper is responsible for so
the regression cannot return silently.

They also pin the scope-filter contract: ``omnigent`` and
``omnigent.frames`` are always exported; SQLAlchemy, HTTPX, FastAPI /
ASGI and websocket instrumentation scopes are filtered out at the
span-processor boundary.
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from omnigent.runtime import telemetry

# ── Fixtures ────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _opt_in_telemetry(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """
    Telemetry is off by default; enable it for the test session so
    the helpers actually do something. Each test cleans its own
    flag state.
    """
    monkeypatch.setenv("OMNIGENT_TELEMETRY_ENABLED", "true")
    yield
    # Best-effort: undo any side effects the test left on the OTel
    # global tracer provider so the next test starts fresh.
    from opentelemetry import trace as otel_trace
    from opentelemetry.trace import NoOpTracerProvider

    otel_trace._TRACER_PROVIDER_SET_ONCE._done = False  # type: ignore[attr-defined]
    otel_trace._TRACER_PROVIDER = None  # type: ignore[attr-defined]
    otel_trace.set_tracer_provider(NoOpTracerProvider())


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Strip env flags so each test sees a deterministic baseline."""
    for var in (
        "OMNIGENT_OTEL_FASTAPI_INSTRUMENTATION",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OMNIGENT_OTEL_ALLOWED_INSTRUMENTATION_SCOPES",
        "OMNIGENT_TELEMETRY_ENABLED",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


def _stub_fastapi_instrumentor(monkeypatch: pytest.MonkeyPatch) -> list[FastAPI]:
    """Record every app passed to ``FastAPIInstrumentor.instrument_app``."""
    calls: list[FastAPI] = []
    monkeypatch.setattr(
        "opentelemetry.instrumentation.fastapi.FastAPIInstrumentor.instrument_app",
        lambda app, **kwargs: calls.append(app),
    )
    return calls


# ── Module / symbol presence ────────────────────────────


def test_module_imports_without_nameerror() -> None:
    """The whole point: the module must import (the NameError was at import time)."""
    import omnigent.runtime.telemetry  # noqa: F401


def test_fastapi_instrumentation_enabled_helper_exists() -> None:
    """The missing ``def`` line is restored; the helper is module-level callable."""
    assert hasattr(telemetry, "_fastapi_instrumentation_enabled")
    assert callable(telemetry._fastapi_instrumentation_enabled)


def test_create_app_does_not_raise_nameerror() -> None:
    """
    The server's ``create_app()`` path calls ``instrument_fastapi_app``.
    Without the helper, the call site raised NameError at startup. With
    it restored, the call site must return cleanly even when telemetry
    is enabled but no backend is configured.
    """
    # We do not need a full app — we just need to confirm the call site
    # no longer raises NameError when telemetry is on and the app is
    # real.
    calls = _stub_fastapi_instrumentor(MagicMock())  # not used; helper only
    # Actually drive the real call site:
    calls = _stub_fastapi_instrumentor(__import__("pytest").MonkeyPatch())
    calls.clear()

    app = FastAPI()
    # Telemetry enabled (autouse fixture) but no backend configured →
    # default behaviour is to skip FastAPI instrumentation. The point is
    # it does NOT raise NameError.
    telemetry.instrument_fastapi_app(app)
    assert calls == []


# ── Telemetry disabled mode ─────────────────────────────


def test_disabled_when_telemetry_flag_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``OMNIGENT_TELEMETRY_ENABLED`` is off, the helper is False
    and ``instrument_fastapi_app`` is a no-op regardless of backend.
    """
    monkeypatch.setenv("OMNIGENT_TELEMETRY_ENABLED", "false")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")

    assert telemetry._fastapi_instrumentation_enabled() is False

    calls = _stub_fastapi_instrumentor(monkeypatch)
    telemetry.instrument_fastapi_app(FastAPI())
    assert calls == []


# ── Endpoint-configured default behaviour ───────────────


def test_default_on_when_endpoint_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the explicit flag unset and an OTLP endpoint set, the
    helper returns True and FastAPI gets instrumented.
    """
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")

    assert telemetry._fastapi_instrumentation_enabled() is True

    app = FastAPI()
    calls = _stub_fastapi_instrumentor(monkeypatch)
    telemetry.instrument_fastapi_app(app)
    assert calls == [app]


def test_default_off_when_endpoint_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the explicit flag unset and no OTLP endpoint, the helper
    returns False and instrumentation is skipped — bare installs pay
    no span overhead.
    """
    assert telemetry._fastapi_instrumentation_enabled() is False

    calls = _stub_fastapi_instrumentor(monkeypatch)
    telemetry.instrument_fastapi_app(FastAPI())
    assert calls == []


# ── Explicit true / false override ─────────────────────


def test_explicit_true_forces_on_without_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``OMNIGENT_OTEL_FASTAPI_INSTRUMENTATION=true`` overrides default
    and installs instrumentation even when no endpoint is set.
    """
    monkeypatch.setenv("OMNIGENT_OTEL_FASTAPI_INSTRUMENTATION", "true")

    assert telemetry._fastapi_instrumentation_enabled() is True

    app = FastAPI()
    calls = _stub_fastapi_instrumentor(monkeypatch)
    telemetry.instrument_fastapi_app(app)
    assert calls == [app]


def test_explicit_false_overrides_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``OMNIGENT_OTEL_FASTAPI_INSTRUMENTATION=false`` wins over an
    otherwise-on backend.
    """
    monkeypatch.setenv("OMNIGENT_OTEL_FASTAPI_INSTRUMENTATION", "false")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")

    assert telemetry._fastapi_instrumentation_enabled() is False

    calls = _stub_fastapi_instrumentor(monkeypatch)
    telemetry.instrument_fastapi_app(FastAPI())
    assert calls == []


@pytest.mark.parametrize("value", ["true", "1", "yes", "TRUE", "Yes"])
def test_truthy_explicit_values_enable(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv("OMNIGENT_OTEL_FASTAPI_INSTRUMENTATION", value)
    assert telemetry._fastapi_instrumentation_enabled() is True


@pytest.mark.parametrize("value", ["false", "0", "no", "off", ""])
def test_non_truthy_explicit_values_disable(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv("OMNIGENT_OTEL_FASTAPI_INSTRUMENTATION", value)
    assert telemetry._fastapi_instrumentation_enabled() is False


# ── Scope filter contract ───────────────────────────────


def test_default_allowed_scopes_are_omnigent_and_frames() -> None:
    """The default allow-list is exactly ``omnigent`` and ``omnigent.frames``."""
    scopes = telemetry._allowed_instrumentation_scopes()
    assert scopes == ("omnigent", "omnigent.frames")


def test_allowed_scopes_dedup_and_normalize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Extra configured scopes are normalised (stripped, deduped) and
    appended after the defaults."""
    monkeypatch.setenv(
        "OMNIGENT_OTEL_ALLOWED_INSTRUMENTATION_SCOPES",
        " foo.bar , omnigent , foo.bar , ",
    )
    scopes = telemetry._allowed_instrumentation_scopes()
    assert scopes[0] == "omnigent"
    assert scopes[1] == "omnigent.frames"
    # normalised, deduplicated, after the defaults
    assert "foo.bar" in scopes
    assert scopes.count("foo.bar") == 1


def _make_span(scope_name: str) -> ReadableSpan:
    """Build a ReadableSpan-like object carrying an instrumentation scope."""
    span = MagicMock(spec=ReadableSpan)
    scope = MagicMock()
    scope.name = scope_name
    span.instrumentation_scope = scope
    span._instrumentation_scope = scope
    return span


@pytest.mark.parametrize(
    "scope_name",
    ["omnigent", "omnigent.frames"],
)
def test_scope_filter_passes_allowed_scopes(scope_name: str) -> None:
    """Allowed scopes pass through the filter."""
    from omnigent.runtime.telemetry import _DEFAULT_ALLOWED_INSTRUMENTATION_SCOPES, _scope_allowed

    allowed = frozenset(_DEFAULT_ALLOWED_INSTRUMENTATION_SCOPES)
    assert _scope_allowed(_make_span(scope_name), allowed) is True


@pytest.mark.parametrize(
    "scope_name",
    [
        "sqlalchemy",
        "opentelemetry.instrumentation.sqlalchemy",
        "httpx",
        "opentelemetry.instrumentation.httpx",
        "fastapi",
        "opentelemetry.instrumentation.fastapi",
        "asgi",
        "opentelemetry.instrumentation.asgi",
        "websocket",
        "opentelemetry.instrumentation.websocket",
        "sse",
        "opentelemetry.instrumentation.sse",
        "health-check",
        "healthcheck",
        "starlette",
        "uvicorn",
    ],
)
def test_scope_filter_blocks_disallowed_scopes(scope_name: str) -> None:
    """SQLAlchemy / HTTPX / FastAPI / ASGI / websocket scopes must be filtered."""
    from omnigent.runtime.telemetry import _DEFAULT_ALLOWED_INSTRUMENTATION_SCOPES, _scope_allowed

    allowed = frozenset(_DEFAULT_ALLOWED_INSTRUMENTATION_SCOPES)
    assert _scope_allowed(_make_span(scope_name), allowed) is False


def test_scope_filter_processor_drops_disallowed_spans() -> None:
    """End-to-end: an inner exporter wrapped in the scope filter
    receives only allowed scopes; SQLAlchemy / HTTPX / FastAPI /
    ASGI / websocket spans never reach it.
    """
    from omnigent.runtime.telemetry import _make_scope_filter_processor

    exporter = InMemorySpanExporter()
    inner = SimpleSpanProcessor(exporter)
    wrapped = _make_scope_filter_processor(inner, ("omnigent", "omnigent.frames"))

    provider = TracerProvider()
    provider.add_span_processor(wrapped)

    # Allowed scopes — must be exported.
    allowed_tracer = provider.get_tracer("omnigent")
    allowed_tracer.start_as_current_span("agent-turn").__enter__()
    # Disallowed scopes — must be filtered.
    for name in ("sqlalchemy", "httpx", "fastapi", "asgi", "websocket"):
        scoped = provider.get_tracer(name)
        with scoped.start_as_current_span(f"op-{name}"):
            pass

    wrapped.force_flush()
    seen = [s.name for s in exporter.get_finished_spans()]
    assert "agent-turn" in seen
    for forbidden in ("op-sqlalchemy", "op-httpx", "op-fastapi", "op-asgi", "op-websocket"):
        assert forbidden not in seen, f"scope filter leaked {forbidden}"


def test_scope_filter_processor_detaches_filtered_parent() -> None:
    """Allowed children of dropped framework spans remain true roots."""
    from omnigent.runtime.telemetry import _make_scope_filter_processor

    exporter = InMemorySpanExporter()
    inner = SimpleSpanProcessor(exporter)
    wrapped = _make_scope_filter_processor(inner, ("omnigent", "omnigent.frames"))
    provider = TracerProvider()
    provider.add_span_processor(wrapped)

    with provider.get_tracer("httpx").start_as_current_span("request") as parent:
        with provider.get_tracer("omnigent").start_as_current_span("host.fs_request"):
            pass

    exported = exporter.get_finished_spans()
    assert [span.name for span in exported] == ["host.fs_request"]
    assert exported[0].parent is None
    assert exported[0].context.trace_id == parent.get_span_context().trace_id


def test_scope_filter_processor_robust_to_missing_scope_attr() -> None:
    """A span with no instrumentation scope attribute must not crash
    the filter and must default to deny.
    """
    from omnigent.runtime.telemetry import _scope_allowed

    span = MagicMock()
    span.instrumentation_scope = None
    span._instrumentation_scope = None
    assert _scope_allowed(span, frozenset({"omnigent"})) is False
