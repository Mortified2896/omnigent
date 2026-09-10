#!/usr/bin/env python3
"""Exercise the production OTTL and privacy rules with the pinned Collector."""

import json
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def kv(key, value):
    kind = "intValue" if isinstance(value, int) else "stringValue"
    return {"key": key, "value": {kind: value}}


def boilerplate():
    return [
        kv("code.file.path", "internal.rs"),
        kv("code.module.name", "internal"),
        kv("code.line.number", 1),
        kv("thread.id", 7),
        kv("thread.name", "worker"),
        kv("target", "internal"),
        kv("busy_ns", 1),
        kv("idle_ns", 2),
        kv("codex.request.reasoning_effort", "high"),
    ]


def span(number, name, attributes=None, **extra):
    item = {
        "traceId": "00112233445566778899aabbccddeeff",
        "spanId": f"{number:016x}",
        "name": name,
        "startTimeUnixNano": "1000000000",
        "endTimeUnixNano": "2000000000",
        "attributes": attributes or boilerplate(),
    }
    item.update(extra)
    return item


def main():
    collector = Path(sys.argv[1])
    with tempfile.TemporaryDirectory() as temp_name:
        temp = Path(temp_name)
        inputs = temp / "input"
        output = temp / "output"
        inputs.mkdir()
        output.mkdir()
        spans = [
            span(1, "receiving"),
            span(2, "append_items"),
            span(3, "persist_rollout_items"),
            span(4, "handle_responses"),
            span(5, "handle_responses", [*boilerplate(), kv("gen_ai.usage.input_tokens", 10)]),
            span(6, "handle_responses", [*boilerplate(), kv("from", "model")]),
            span(
                7,
                "handle_responses",
                events=[
                    {
                        "name": "valuable",
                        "timeUnixNano": "1500000000",
                        "attributes": [
                            kv("error.message", "sensitive event error"),
                            kv("error.type", "event_failure"),
                            kv("error.category", "tool"),
                        ],
                    }
                ],
            ),
            span(
                8,
                "handle_responses",
                [
                    *boilerplate(),
                    kv("error.message", "sensitive span error"),
                    kv("error.type", "request_failure"),
                    kv("error.category", "transport"),
                    kv("http.response.status_code", 503),
                    kv("outcome", "failure"),
                ],
                status={"code": 2},
            ),
            span(
                9,
                "session_task.turn",
                [
                    *boilerplate(),
                    kv("thread.id", "session-id"),
                    kv("turn.id", "turn-id"),
                    kv("model", "test-model"),
                ],
            ),
        ]
        spans.extend(
            [
                span(
                    10,
                    "producer-test",
                    [
                        kv("app_server.client_name", "omnigent-codex-native-runner"),
                        kv("arguments", "SYNTHETIC_PRIVATE_ARGUMENTS"),
                    ],
                ),
                span(
                    11,
                    "producer-test",
                    [kv("originator", "Codex_Desktop"), kv("prompt", "SYNTHETIC_PRIVATE_PROMPT")],
                ),
                span(12, "producer-test", [kv("originator", "unrecognized-client")]),
            ]
        )
        trace_request = {
            "resourceSpans": [
                {
                    "resource": {"attributes": [kv("service.name", "filter-test")]},
                    "scopeSpans": [{"scope": {"name": "test"}, "spans": spans}],
                }
            ]
        }
        (inputs / "traces.json").write_text(json.dumps(trace_request) + "\n")
        log_attributes = [
            kv("originator", "Codex_Desktop"),
            kv("event.name", "codex.tool_result"),
            kv("conversation.id", "session-id"),
            kv("model", "test-model"),
            kv("tool_name", "shell"),
            kv("duration_ms", 5),
            kv("success", "true"),
            kv("outcome", "allowed"),
            kv("prompt", "SYNTHETIC_PRIVATE_PROMPT"),
            kv("arguments", "sensitive argument"),
            kv("output", "sensitive output"),
            kv("user.email", "private@example.invalid"),
            kv("user.account_id", "private-id"),
            kv("error.message", "sensitive error"),
        ]
        log_request = {
            "resourceLogs": [
                {
                    "resource": {"attributes": [kv("service.name", "filter-test")]},
                    "scopeLogs": [
                        {
                            "scope": {"name": "test"},
                            "logRecords": [
                                {"timeUnixNano": "1000000000", "attributes": log_attributes}
                            ],
                        }
                    ],
                }
            ]
        }
        (inputs / "logs.json").write_text(json.dumps(log_request) + "\n")
        # Load the actual candidate processors; the fixture opens no network listeners.
        import yaml

        production = yaml.safe_load(
            Path(__file__).with_name("config").joinpath("otelcol-macos.yaml").read_text()
        )
        processors = production["processors"]
        processors.pop("resource/control_room")
        processors["attributes/replay_input"] = {
            "actions": [{"key": "log.file.name", "action": "delete"}]
        }
        config = {
            "receivers": {
                "otlp_json_file/traces": {
                    "include": [str(inputs / "traces.json")],
                    "start_at": "beginning",
                },
                "otlp_json_file/logs": {
                    "include": [str(inputs / "logs.json")],
                    "start_at": "beginning",
                },
            },
            "processors": processors,
            "exporters": {
                "file/traces": {"path": str(output / "traces.json"), "format": "json"},
                "file/logs": {"path": str(output / "logs.json"), "format": "json"},
            },
            "service": {
                "telemetry": {"metrics": {"level": "none"}},
                "pipelines": {
                    signal: {
                        "receivers": ["otlp_json_file/" + signal],
                        "processors": ["attributes/replay_input"]
                        + [
                            p
                            for p in production["service"]["pipelines"][pipeline]["processors"]
                            if p != "resource/control_room"
                        ],
                        "exporters": ["file/" + signal],
                    }
                    for signal, pipeline in [("logs", "logs"), ("traces", "traces/lean")]
                },
            },
        }
        config = yaml.safe_dump(config)
        config_path = temp / "collector.yaml"
        config_path.write_text(config)
        process = subprocess.Popen(
            [str(collector), "--config", str(config_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            last = None
            for _ in range(100):
                sizes = tuple(
                    p.stat().st_size if p.exists() else 0
                    for p in (output / "traces.json", output / "logs.json")
                )
                if min(sizes) > 0 and sizes == last:
                    break
                last = sizes
                time.sleep(0.05)
            else:
                raise AssertionError("Collector fixture output did not stabilize")
        finally:
            process.send_signal(signal.SIGINT)
            _, stderr = process.communicate(timeout=5)
        if process.returncode != 0:
            raise AssertionError(stderr)

        trace_rows = [json.loads(line) for line in (output / "traces.json").open()]
        kept = [
            s
            for row in trace_rows
            for rs in row["resourceSpans"]
            for ss in rs["scopeSpans"]
            for s in ss["spans"]
        ]
        by_id = {s["spanId"]: s for s in kept}
        for number, producer in [(10, "omnigent"), (11, "desktop"), (12, None)]:
            attributes = {
                a["key"]: a["value"] for a in by_id[f"{number:016x}"].get("attributes", [])
            }
            assert attributes.get("codex.producer") == (
                {"stringValue": producer} if producer else None
            )
        assert "SYNTHETIC_PRIVATE" not in (output / "traces.json").read_text()
        names = [s["name"] for s in kept]
        assert names.count("handle_responses") == 4 and names.count("session_task.turn") == 1, (
            names
        )
        session = next(s for s in kept if s["name"] == "session_task.turn")
        attrs = {a["key"]: a["value"] for a in session.get("attributes", [])}
        assert attrs["thread.id"] == {"stringValue": "session-id"}
        assert attrs["turn.id"] == {"stringValue": "turn-id"}
        for key in (
            "code.file.path",
            "code.module.name",
            "code.line.number",
            "thread.name",
            "target",
            "busy_ns",
            "idle_ns",
        ):
            assert key not in attrs

        error_span = next(s for s in kept if s.get("status", {}).get("code") == 2)
        error_attrs = {a["key"]: a["value"] for a in error_span.get("attributes", [])}
        assert "error.message" not in error_attrs
        assert error_span["status"] == {"code": 2}
        assert error_attrs["error.type"] == {"stringValue": "request_failure"}
        assert error_attrs["error.category"] == {"stringValue": "transport"}
        assert error_attrs["http.response.status_code"] == {"intValue": "503"}
        assert error_attrs["outcome"] == {"stringValue": "failure"}

        event_span = next(s for s in kept if s.get("events"))
        event = next(e for e in event_span["events"] if e["name"] == "valuable")
        event_attrs = {a["key"]: a["value"] for a in event.get("attributes", [])}
        assert "error.message" not in event_attrs
        assert event_attrs["error.type"] == {"stringValue": "event_failure"}
        assert event_attrs["error.category"] == {"stringValue": "tool"}

        log_rows = [json.loads(line) for line in (output / "logs.json").open()]
        record = next(
            r
            for row in log_rows
            for rl in row["resourceLogs"]
            for sl in rl["scopeLogs"]
            for r in sl["logRecords"]
        )
        attrs = {a["key"]: a["value"] for a in record.get("attributes", [])}
        for key in ("arguments", "output", "user.email", "user.account_id", "error.message"):
            assert key not in attrs
        for key in (
            "event.name",
            "conversation.id",
            "model",
            "tool_name",
            "duration_ms",
            "success",
            "outcome",
        ):
            assert key in attrs
        assert "prompt" not in attrs
        assert attrs["codex.producer"] == {"stringValue": "desktop"}
        assert "SYNTHETIC_PRIVATE_PROMPT" not in (output / "logs.json").read_text()
        print("Collector filtering/privacy fixture passed")


if __name__ == "__main__":
    main()
