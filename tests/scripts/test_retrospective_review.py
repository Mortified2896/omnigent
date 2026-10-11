"""Regression coverage for evidence that must not become task answers."""

import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2] / "scripts/retrospective_review.py"
SPEC = importlib.util.spec_from_file_location("retrospective_review", SOURCE)
REVIEW = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REVIEW)


class TraceTest(unittest.TestCase):
    def test_binary_tool_media_is_not_rendered_as_base64(self):
        value = [
            {"type": "input_text", "text": "Verified"},
            {"type": "image", "data": "fixture-binary"},
        ]
        visible = REVIEW.tool_text(value)
        self.assertIn("Verified", visible)
        self.assertNotIn("fixture-binary", visible)
        self.assertIn("original trace", visible)

    def test_native_phases_and_injected_goal_context(self):
        def message(role, text, **extra):
            return {
                "type": "response_item",
                "timestamp": "2026-10-11T00:00:00Z",
                "payload": {
                    "type": "message",
                    "role": role,
                    "content": [{"type": "input_text", "text": text}],
                    **extra,
                },
            }

        records = [
            {
                "type": "turn_context",
                "payload": {"model": "fixture", "effort": "high", "turn_id": "t1"},
            },
            message("user", "Fix the fixture"),
            message("user", '\n<codex_internal_context source="goal">injected continuation'),
            message("user", "<recommended_plugins>injected recommendations"),
            message("assistant", "Working on it", phase="commentary"),
            message("assistant", "private reasoning", channel="analysis"),
            message("assistant", "Fixed and verified", phase="final_answer"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout.jsonl"
            path.write_text("".join(json.dumps(r) + "\n" for r in records))
            trace = REVIEW.codex_trace(path)
        self.assertEqual(
            [r["text"] for r in trace],
            [
                "Fix the fixture",
                "<recommended_plugins>injected recommendations",
                "Working on it",
                "Fixed and verified",
            ],
        )
        self.assertEqual(trace[1]["kind"], "runtime notice")
        self.assertEqual(trace[2]["channel"], "commentary")
        self.assertEqual(trace[3]["channel"], "final")
        self.assertEqual(trace[3]["turn_id"], "t1")

    def test_zcode_wal_cannot_be_silently_omitted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "db.sqlite"
            path.with_name("db.sqlite-wal").touch()
            with self.assertRaisesRegex(ValueError, "absent WAL"):
                REVIEW.sqlite_read(path)

    def test_zcode_runtime_notices_keep_positions_without_becoming_user_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "db.sqlite"
            with sqlite3.connect(path) as connection:
                connection.executescript("""
                    CREATE TABLE message(id TEXT, session_id TEXT, sequence INTEGER,
                        time_created INTEGER, data TEXT);
                    CREATE TABLE part(message_id TEXT, sequence INTEGER,
                        time_created INTEGER, data TEXT);
                    CREATE TABLE model_usage(assistant_message_id TEXT, started_at INTEGER,
                        model_id TEXT, variant TEXT, turn_id TEXT);
                """)
                fixtures = [
                    ({"role": "user"}, {"text": "The TodoWrite tool hasn't been used recently."}),
                    ({"role": "user", "synthetic": True}, {"text": "automatic reminder"}),
                    ({"role": "user"}, {"text": "background notice", "synthetic": True}),
                    (
                        {"role": "user"},
                        {"text": "model-only", "metadata": {"visibility": "model-only"}},
                    ),
                    ({"role": "assistant"}, {"text": "completed answer"}),
                ]
                for index, (message, part) in enumerate(fixtures):
                    mid = str(index)
                    connection.execute(
                        "INSERT INTO message VALUES (?,?,?,?,?)",
                        (mid, "session", index, 1, json.dumps(message)),
                    )
                    connection.execute(
                        "INSERT INTO part VALUES (?,?,?,?)",
                        (mid, 0, 1, json.dumps({"type": "text", **part})),
                    )
            trace = REVIEW.zcode_trace(path, "session")
        self.assertEqual(
            [row["kind"] for row in trace],
            ["user", "runtime notice", "runtime notice", "runtime notice", "assistant"],
        )
        self.assertEqual(trace[-1]["text"], "completed answer")

    def test_omnigent_interim_chunks_do_not_become_completed_task_checkpoints(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "db.sqlite"
            session = "12345678123456781234567812345678"
            with sqlite3.connect(path) as connection:
                connection.execute(
                    "CREATE TABLE conversation_items(conversation_id BLOB, "
                    "response_id TEXT,data TEXT)"
                )
                event = {
                    "resource_type": "response-execution",
                    "event_type": "response.execution.completed",
                    "resource": {"conversation_id": session, "response_id": "finished"},
                }
                connection.execute(
                    "INSERT INTO conversation_items VALUES (?,?,?)",
                    (bytes.fromhex(session), "finished", json.dumps(event)),
                )
            trace = [
                {"kind": "assistant", "turn_id": "finished", "text": "interim"},
                {"kind": "tool result", "turn_id": "finished", "text": "verified"},
                {"kind": "assistant", "turn_id": "finished", "text": "final"},
                {"kind": "assistant", "turn_id": "running", "text": "still working"},
            ]
            self.assertEqual(REVIEW.checkpoint_indices("omnigent", session, trace, path), {2})

    def test_zcode_tool_call_messages_and_errors_do_not_become_final_checkpoints(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "db.sqlite"
            with sqlite3.connect(path) as connection:
                connection.execute("CREATE TABLE message(id TEXT,session_id TEXT,data TEXT)")
                for mid, extra in (
                    ("final", {"finish": "stop"}),
                    ("tool", {"finish": "tool-calls"}),
                    ("error", {"finish": "stop", "error": {"name": "cancelled"}}),
                ):
                    data = {"role": "assistant", "time": {"completed": 1}, **extra}
                    connection.execute(
                        "INSERT INTO message VALUES (?,?,?)", (mid, "s", json.dumps(data))
                    )
            trace = [
                {"kind": "assistant", "message_id": mid, "text": "text"}
                for mid in ("final", "final", "tool", "error")
            ]
            self.assertEqual(REVIEW.checkpoint_indices("zcode", "s", trace, path), {1})


if __name__ == "__main__":
    unittest.main()
