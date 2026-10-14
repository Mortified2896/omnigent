"""Native usage counts stay additive, attributed and free of transcript text."""

import json

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from omnigent.harnesses.claude_native import bridge
from omnigent.harnesses.claude_native import forwarder as claude
from omnigent.harnesses.codex_native import forwarder as codex
from omnigent.runtime import telemetry


@pytest.fixture
def exporter(monkeypatch):
    monkeypatch.setenv("OMNIGENT_TELEMETRY_ENABLED", "true")
    sink = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(telemetry._make_session_id_processor())
    provider.add_span_processor(SimpleSpanProcessor(sink))
    monkeypatch.setattr(trace, "_TRACER_PROVIDER", provider)
    yield sink
    provider.shutdown()


def test_codex_counts_completed_calls_and_ignores_duplicate_or_cumulative_frames(exporter):
    usage = codex._SessionUsageCoalescer(None, "codex-session", model="gpt-test")
    first = {
        "tokenUsage": {
            "total": {"inputTokens": 1000, "outputTokens": 30},
            "last": {"inputTokens": 100, "outputTokens": 10, "cachedInputTokens": 80},
        }
    }
    usage.record(first)
    usage.record(first)
    usage.record(
        {
            "tokenUsage": {
                "total": {"inputTokens": 1200, "outputTokens": 50},
                "last": {"inputTokens": 200, "outputTokens": 20},
            }
        }
    )
    usage.record({"tokenUsage": {"total": {"inputTokens": 1500, "outputTokens": 60}}})
    spans = exporter.get_finished_spans()
    assert len(spans) == 2
    attrs = [dict(span.attributes) for span in spans]
    assert sum(row["gen_ai.usage.input_tokens"] for row in attrs) == 300
    assert sum(row["gen_ai.usage.output_tokens"] for row in attrs) == 30
    assert attrs[0]["gen_ai.usage.cache_read_input_tokens"] == 80
    assert "gen_ai.usage.cache_read_input_tokens" not in attrs[1]
    assert all(
        row["session.id"] == "codex-session"
        and row["omnigent.harness"] == "codex-native"
        and row["gen_ai.response.model"] == "gpt-test"
        for row in attrs
    )


def test_claude_counts_all_messages_in_a_poll_and_only_growth_for_repeated_blocks(
    tmp_path, exporter
):
    transcript = tmp_path / "session.jsonl"
    entries = [
        {
            "type": "assistant",
            "uuid": f"row-{index}",
            "message": {
                "id": message_id,
                "role": "assistant",
                "model": "claude-test",
                "content": [{"type": "text", "text": "PRIVATE_CONTENT_MARKER"}],
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": output,
                    "cache_read_input_tokens": 20,
                },
            },
        }
        for index, (message_id, output) in enumerate([("msg-a", 10), ("msg-a", 15), ("msg-b", 15)])
    ]
    transcript.write_text("".join(json.dumps(entry) + "\n" for entry in entries))
    parsed = bridge.read_transcript_items_from_offset(
        transcript, 0, start_line=0, agent_name="claude-native-ui"
    )
    assert len(parsed.api_usage) == 2
    dedupe = claude._ForwardDedupeState()
    for record in parsed.api_usage:
        claude._record_claude_api_usage("claude-session", record, dedupe)
    for record in parsed.api_usage:
        claude._record_claude_api_usage("claude-session", record, dedupe)
    claude._record_claude_api_usage(
        "claude-session",
        bridge.ClaudeApiUsage(
            "msg-b",
            {"input_tokens": 100, "output_tokens": 18, "cache_read_input_tokens": 20},
            "claude-test",
        ),
        dedupe,
    )
    attrs = [dict(span.attributes) for span in exporter.get_finished_spans()]
    assert len(attrs) == 3
    assert sum(row["gen_ai.usage.input_tokens"] for row in attrs) == 240
    assert sum(row["gen_ai.usage.output_tokens"] for row in attrs) == 33
    assert sum(row.get("gen_ai.usage.cache_read_input_tokens", 0) for row in attrs) == 40
    assert all(
        row["session.id"] == "claude-session"
        and row["omnigent.harness"] == "claude-native"
        and row["gen_ai.response.model"] == "claude-test"
        for row in attrs
    )
    assert "PRIVATE_CONTENT_MARKER" not in json.dumps(attrs)
