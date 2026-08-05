"""
Tests for the OTel GenAI semantic-convention attributes on omnigent's
AGENT and TOOL spans (PR #1050).

Each test installs a fresh TracerProvider with an InMemorySpanExporter
through the OTel public API (no mlflow internals, no singleton
poking), exercises the production TracingContext path that the
executor adapter uses, then asserts on the exported span attributes.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from omnigent.inner.tracing import TracingContext, enable_tracing
from omnigent.runtime import telemetry


@pytest.fixture
def exporter() -> Iterator[InMemorySpanExporter]:
    """
    Install a fresh TracerProvider with an in-memory exporter for one
    test. Restores the previous provider on teardown so OTel's
    set-once semantics do not leak into later tests in the same
    process.
    """
    previous = otel_trace._TRACER_PROVIDER  # type: ignore[attr-defined]
    previous_done = otel_trace._TRACER_PROVIDER_SET_ONCE._done  # type: ignore[attr-defined]
    in_mem = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(in_mem))
    otel_trace._TRACER_PROVIDER = provider  # type: ignore[attr-defined]
    otel_trace._TRACER_PROVIDER_SET_ONCE._done = True  # type: ignore[attr-defined]
    enable_tracing()
    try:
        yield in_mem
    finally:
        in_mem.clear()
        with contextlib.suppress(Exception):
            provider.shutdown()
        otel_trace._TRACER_PROVIDER = previous  # type: ignore[attr-defined]
        otel_trace._TRACER_PROVIDER_SET_ONCE._done = previous_done  # type: ignore[attr-defined]


def _spans_by_name(exporter: InMemorySpanExporter, name_prefix: str):
    return [s for s in exporter.get_finished_spans() if s.name.startswith(name_prefix)]


# ---
# AGENT span gen_ai semconv attributes
# ---


def test_agent_span_carries_gen_ai_invoke_agent_attrs(exporter: InMemorySpanExporter):
    ctx = TracingContext()
    span = ctx.start_agent_span(
        agent_name="my-agent",
        user_message="hello",
        model="anthropic/claude-3-5-haiku-20241022",
    )
    ctx.end_agent_span(span, response="hi back")

    agent_spans = _spans_by_name(exporter, "agent:")
    assert len(agent_spans) == 1
    attrs = dict(agent_spans[0].attributes or {})
    assert attrs["gen_ai.operation.name"] == "invoke_agent"
    assert attrs["gen_ai.agent.name"] == "my-agent"
    assert attrs["gen_ai.provider.name"] == "anthropic"
    assert attrs["gen_ai.request.model"] == "claude-3-5-haiku-20241022"
    # OpenInference span-kind and legacy attrs stay alongside
    assert attrs["openinference.span.kind"] == "AGENT"
    assert attrs["agent.name"] == "my-agent"
    assert attrs["llm.model_name"] == "anthropic/claude-3-5-haiku-20241022"


def test_agent_span_without_model_omits_provider_and_request_model(
    exporter: InMemorySpanExporter,
):
    ctx = TracingContext()
    span = ctx.start_agent_span(agent_name="my-agent", user_message="hi")
    ctx.end_agent_span(span, response="ok")

    attrs = dict(_spans_by_name(exporter, "agent:")[0].attributes or {})
    assert attrs["gen_ai.operation.name"] == "invoke_agent"
    assert attrs["gen_ai.agent.name"] == "my-agent"
    assert "gen_ai.provider.name" not in attrs
    assert "gen_ai.request.model" not in attrs


def test_agent_span_provider_only_omits_request_model(
    exporter: InMemorySpanExporter,
):
    """
    Bare model name without provider prefix. parse_provider_name
    returns empty provider; the gen_ai.provider.name attr is omitted.
    """
    ctx = TracingContext()
    span = ctx.start_agent_span(agent_name="my-agent", user_message="hi", model="gpt-5.1")
    ctx.end_agent_span(span, response="ok")

    attrs = dict(_spans_by_name(exporter, "agent:")[0].attributes or {})
    assert "gen_ai.provider.name" not in attrs
    # parse_provider_name treats a single-token string as the model name
    assert attrs["gen_ai.request.model"] == "gpt-5.1"


# ---
# TOOL span gen_ai semconv attributes
# ---


def test_tool_span_carries_gen_ai_execute_tool_attrs(exporter: InMemorySpanExporter):
    ctx = TracingContext()
    agent = ctx.start_agent_span(agent_name="a", user_message="m")
    tool = ctx.start_tool_span(tool_name="calculator", tool_args={"x": 1, "y": 2})
    ctx.end_tool_span(tool, result={"answer": 3})
    ctx.end_agent_span(agent, response="done")

    tool_spans = _spans_by_name(exporter, "tool:")
    assert len(tool_spans) == 1
    attrs = dict(tool_spans[0].attributes or {})
    assert attrs["gen_ai.operation.name"] == "execute_tool"
    assert attrs["tool.name"] == "calculator"
    # GenAI semconv key for tool identity, emitted alongside the legacy
    # OpenInference tool.name so spec-aware backends find it too.
    assert attrs["gen_ai.tool.name"] == "calculator"


# ---
# Content-capture gate
# ---


def test_content_capture_off_by_default_drops_input_and_output(
    exporter: InMemorySpanExporter,
    monkeypatch: pytest.MonkeyPatch,
):
    """
    With OMNIGENT_OTEL_CAPTURE_CONTENT off (the default),
    input.value / output.value are NOT set on agent + tool spans.
    Metadata attrs (agent.name, tool.name, gen_ai.*) still are.
    """
    monkeypatch.setattr("omnigent.runtime.telemetry._capture_content", False)

    ctx = TracingContext()
    agent = ctx.start_agent_span(agent_name="a", user_message="PII: user@example.com")
    tool = ctx.start_tool_span(tool_name="cred-store", tool_args={"secret": "PII: sk-abcdef"})
    policy = ctx.start_policy_span(
        policy_name="egress",
        phase="pre_tool_use",
        content={"payload": "PII: policy@example.com"},
    )
    ctx.end_tool_span(tool, result={"value": "PII: leaked@example.com"})
    ctx.end_policy_span(policy, action="deny", reason="policy@example.com")
    ctx.end_agent_span(agent, response="PII: response with email@example.com")

    for span in exporter.get_finished_spans():
        attrs = dict(span.attributes or {})
        for forbidden in (
            "input.value",
            "output.value",
            "mlflow.spanInputs",
            "mlflow.spanOutputs",
            "error.message",
        ):
            assert forbidden not in attrs, f"{forbidden} leaked on {span.name}: {attrs}"
        for v in attrs.values():
            assert "@example.com" not in str(v), f"PII string leaked via {span.name}: {attrs}"
            assert "sk-abcdef" not in str(v), f"secret leaked via {span.name}: {attrs}"
    policy_attrs = dict(_spans_by_name(exporter, "policy:")[0].attributes or {})
    assert policy_attrs["policy.action"] == "deny"
    assert policy_attrs["policy.name"] == "egress"


def test_content_capture_on_includes_input_and_output(
    exporter: InMemorySpanExporter,
    monkeypatch: pytest.MonkeyPatch,
):
    """
    With OMNIGENT_OTEL_CAPTURE_CONTENT on, input.value and output.value
    appear on agent + tool spans.
    """
    monkeypatch.setattr("omnigent.runtime.telemetry._capture_content", True)

    ctx = TracingContext()
    agent = ctx.start_agent_span(agent_name="a", user_message="explain X")
    tool = ctx.start_tool_span(tool_name="t", tool_args={"q": "explain X"})
    ctx.end_tool_span(tool, result={"answer": "X is ..."})
    ctx.end_agent_span(agent, response="here you go")

    agent_attrs = dict(_spans_by_name(exporter, "agent:")[0].attributes or {})
    tool_attrs = dict(_spans_by_name(exporter, "tool:")[0].attributes or {})

    assert agent_attrs["input.value"] == "explain X"
    assert agent_attrs["output.value"] == "here you go"
    assert "explain X" in tool_attrs["input.value"]
    assert "X is" in tool_attrs["output.value"]


def test_content_capture_off_drops_error_message(
    exporter: InMemorySpanExporter,
    monkeypatch: pytest.MonkeyPatch,
):
    """
    Error text can echo user input or tool payloads, so with content
    capture off it must NOT land on the span (neither as error.message
    nor in the status description). but the span is still flagged
    ERROR so failures stay visible.
    """
    monkeypatch.setattr("omnigent.runtime.telemetry._capture_content", False)

    ctx = TracingContext()
    agent = ctx.start_agent_span(agent_name="a", user_message="hi")
    tool = ctx.start_tool_span(tool_name="t", tool_args={"q": "hi"})
    ctx.end_tool_span(tool, error="tool blew up on user@example.com")
    ctx.end_agent_span(agent, response=None, error="agent failed: sk-abcdef")

    for span in exporter.get_finished_spans():
        attrs = dict(span.attributes or {})
        assert "error.message" not in attrs, f"error.message leaked on {span.name}: {attrs}"
        for v in attrs.values():
            assert "@example.com" not in str(v), f"PII leaked via {span.name}: {attrs}"
            assert "sk-abcdef" not in str(v), f"secret leaked via {span.name}: {attrs}"
        assert span.status.status_code == StatusCode.ERROR
        description = span.status.description or ""
        assert "@example.com" not in description
        assert "sk-abcdef" not in description


def test_content_capture_on_includes_error_message(
    exporter: InMemorySpanExporter,
    monkeypatch: pytest.MonkeyPatch,
):
    """With content capture on, the error text is recorded on the span."""
    monkeypatch.setattr("omnigent.runtime.telemetry._capture_content", True)

    ctx = TracingContext()
    agent = ctx.start_agent_span(agent_name="a", user_message="hi")
    ctx.end_agent_span(agent, response=None, error="boom")

    attrs = dict(_spans_by_name(exporter, "agent:")[0].attributes or {})
    assert attrs["error.message"] == "boom"


@pytest.mark.parametrize("finish_mode", ["success", "cancellation", "exception"])
def test_task_root_finishes_exactly_once_for_each_terminal_path(
    exporter: InMemorySpanExporter,
    monkeypatch: pytest.MonkeyPatch,
    finish_mode: str,
):
    """Every terminal turn path ends its task root once, never twice."""
    monkeypatch.setattr("omnigent.runtime.telemetry._capture_content", False)
    from opentelemetry.sdk.trace import Span as SdkSpan

    end_calls = []
    original_end = SdkSpan.end

    def counted_end(self, *args, **kwargs):
        if self.name == "agent:root":
            end_calls.append(self)
        return original_end(self, *args, **kwargs)

    monkeypatch.setattr(SdkSpan, "end", counted_end)

    ctx = TracingContext()
    root = ctx.start_agent_span(agent_name="root", user_message="safe")
    if finish_mode == "cancellation":
        telemetry.record_cancellation(root)
        ctx.end_agent_span(root, response=None)
    elif finish_mode == "exception":
        telemetry.record_error(root, RuntimeError("sensitive detail"))
        ctx.end_agent_span(root, response=None)
    else:
        ctx.end_agent_span(root, response="safe response")

    assert end_calls == [root]
    assert root.is_recording() is False
    assert [span.name for span in exporter.get_finished_spans()] == ["agent:root"]


def test_response_root_has_response_trace_id_and_no_parent(
    exporter: InMemorySpanExporter,
    monkeypatch: pytest.MonkeyPatch,
):
    """The sentinel-parent correction exports the response span as a true root."""
    monkeypatch.setattr("omnigent.runtime.telemetry._capture_content", False)
    response_id = "resp_" + "b" * 32

    with telemetry.trace_context_for_response(response_id=response_id):
        ctx = TracingContext()
        root = ctx.start_agent_span(agent_name="root", user_message="safe")
        ctx.end_agent_span(root, response="ok")

    exported = [span for span in exporter.get_finished_spans() if span.name == "agent:root"]
    assert len(exported) == 1
    root_span = exported[0]
    assert format(root_span.context.trace_id, "032x") == "b" * 32
    assert root_span.parent is None


def test_subagent_stays_nested_under_response_root(
    exporter: InMemorySpanExporter,
    monkeypatch: pytest.MonkeyPatch,
):
    """Subagent spans share the root trace and retain the task parent edge."""
    monkeypatch.setattr("omnigent.runtime.telemetry._capture_content", False)
    response_id = "resp_" + "c" * 32

    with telemetry.trace_context_for_response(response_id=response_id):
        ctx = TracingContext()
        root = ctx.start_agent_span(agent_name="root", user_message="safe")
        child_ctx = ctx.create_child_context()
        child = child_ctx.start_agent_span(agent_name="subagent", user_message="safe")
        child_ctx.end_agent_span(child, response="ok")
        ctx.end_agent_span(root, response="ok")

    spans = exporter.get_finished_spans()
    root_span = next(span for span in spans if span.name == "agent:root")
    child_span = next(span for span in spans if span.name == "agent:subagent")
    assert root_span.parent is None
    assert child_span.parent is not None
    assert child_span.parent.span_id == root_span.context.span_id
    assert child_span.context.trace_id == root_span.context.trace_id


# ---
# Dead-helper deletion: start_llm_span / end_llm_span removed
# ---


def test_dead_llm_helpers_removed():
    """
    start_llm_span / end_llm_span had zero production callers (LLM
    spans come from inside the spawned executor subprocess via the
    SDK's own tracing). They were deleted as part of this PR. Lock
    that with a test so a future drive-by add gets caught.
    """
    ctx = TracingContext()
    assert not hasattr(ctx, "start_llm_span"), (
        "start_llm_span was deleted as dead-for-production. If you need "
        "LLM-level instrumentation, wire it from where LLM calls actually "
        "happen. inside the spawned subprocess via the SDK, or from "
        "record_llm_usage in TurnComplete."
    )
    assert not hasattr(ctx, "end_llm_span")
