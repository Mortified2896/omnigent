"""Regression fixtures for the audit's join and revision boundaries."""

import importlib.util
import json
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "routing_evidence_audit", Path(__file__).parents[2] / "scripts/routing_evidence_audit.py"
)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class AuditTest(unittest.TestCase):
    def test_uuid_storage_revision_exclusion_and_fork_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "chat.db"
            connection = sqlite3.connect(path)
            connection.executescript("""
                CREATE TABLE conversations(id BLOB, parent_conversation_id BLOB);
                CREATE TABLE conversation_labels(conversation_id BLOB,key TEXT,value TEXT);
                CREATE TABLE conversation_items(conversation_id BLOB,response_id TEXT,
                    created_at INTEGER,position INTEGER,data TEXT,created_by TEXT,
                    type INTEGER,status INTEGER);
            """)
            sid, other, test = (uuid.uuid4() for _ in range(3))
            for value in (sid, other, test):
                connection.execute("INSERT INTO conversations VALUES (?,NULL)", (value.bytes,))
            connection.execute(
                "INSERT INTO conversation_labels VALUES (?,?,?)",
                (test.bytes, "omnigent.test.run_id", "fixture"),
            )
            position = 0

            def item(session, response, data):
                nonlocal position
                position += 1
                connection.execute(
                    "INSERT INTO conversation_items VALUES (?,?,?,?,?,?,?,?)",
                    (
                        session.bytes,
                        response,
                        1,
                        position,
                        json.dumps(data),
                        "owner",
                        8 if "resource" in data else 1,
                        1,
                    ),
                )

            def outcome(session, value, response="answer", payload_session=None, kind="outcome"):
                item(
                    session,
                    response,
                    {
                        "event_type": "task.experiment." + kind,
                        "resource_type": "task-success-experiment",
                        "resource": {
                            "conversation_id": (payload_session or session).hex,
                            "kind": kind,
                            "outcome": value,
                            "review_source": "human",
                            "score_eligible": False,
                        },
                    },
                )

            item(sid, "answer", {"role": "assistant", "content": []})
            outcome(sid, "success")
            outcome(sid, "failed")  # Same timestamp; append order must win.
            outcome(other, "success", payload_session=sid)  # Copied fork event.
            item(test, "answer", {"role": "assistant", "content": []})
            outcome(test, "success")
            item(sid, "uncertain", {"role": "assistant", "content": []})
            outcome(sid, "not_sure", response="uncertain")
            item(sid, "excluded", {"role": "assistant", "content": []})
            outcome(sid, "success", response="excluded")
            outcome(sid, None, response="excluded", kind="scoring_eligibility")
            item(
                sid,
                "answer",
                {
                    "event_type": "response.execution.completed",
                    "resource_type": "response-execution",
                    "resource": {
                        "conversation_id": str(sid),
                        "response_id": "answer",
                        "actual_model": "fixture-model",
                        "model_source": "response_usage",
                        "reasoning_effort": "high",
                    },
                },
            )
            connection.commit()
            connection.close()
            result = module.audit(path)
            self.assertEqual(result["copied_events_ignored"], 1)
            self.assertEqual(result["raw_human_outcome_events"], 5)
            self.assertEqual(result["latest_actor_response_outcomes"], 4)
            self.assertEqual(result["latest_included_outcomes"], {"failed": 1, "not_sure": 1})
            self.assertEqual(result["joined_known_outcomes"], 1)
            self.assertEqual(result["joined_models"], {"fixture-model": 1})
            self.assertEqual(result["eligible_responses_without_known_outcome"], 2)


if __name__ == "__main__":
    unittest.main()
