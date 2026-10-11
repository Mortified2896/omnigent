"""Regression coverage for evidence that must not become task answers."""

import importlib.util
import json
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
            message("assistant", "Working on it", phase="commentary"),
            message("assistant", "private reasoning", channel="analysis"),
            message("assistant", "Fixed and verified", phase="final_answer"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout.jsonl"
            path.write_text("".join(json.dumps(r) + "\n" for r in records))
            trace = REVIEW.codex_trace(path)
        self.assertEqual(
            [r["text"] for r in trace], ["Fix the fixture", "Working on it", "Fixed and verified"]
        )
        self.assertEqual(trace[1]["channel"], "commentary")
        self.assertEqual(trace[2]["channel"], "final")
        self.assertEqual(trace[2]["turn_id"], "t1")

    def test_zcode_wal_cannot_be_silently_omitted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "db.sqlite"
            path.with_name("db.sqlite-wal").touch()
            with self.assertRaisesRegex(ValueError, "absent WAL"):
                REVIEW.sqlite_read(path)


if __name__ == "__main__":
    unittest.main()
