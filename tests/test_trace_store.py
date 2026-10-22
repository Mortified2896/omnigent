import json
import sqlite3
import time

from omnigent.trace_store.__main__ import collect
from omnigent.trace_store.adapters import otlp_record
from omnigent.trace_store.store import Store, scrub, source_db


def log(name="codex.api_request", **attributes):
    attributes = {
        "event.name": name,
        "conversation.id": "session",
        "model": "test-model",
        **attributes,
    }
    return {
        "resourceLogs": [
            {
                "scopeLogs": [
                    {
                        "logRecords": [
                            {
                                "timeUnixNano": "1792660000000000000",
                                "attributes": [
                                    {"key": k, "value": {"stringValue": v}}
                                    for k, v in attributes.items()
                                ],
                                "body": {"stringValue": "secret tool output"},
                            }
                        ]
                    }
                ]
            }
        ]
    }


def test_otlp_drops_streams_and_private_payloads(tmp_path):
    store = Store(tmp_path / "store.db")
    otlp_record(store, "mac", log("codex.sse_event"))
    assert store.db.execute("SELECT count(*) FROM events").fetchone()[0] == 0
    otlp_record(store, "mac", log(password="sensitive"))
    otlp_record(store, "mac", log(password="sensitive"))
    rows = [dict(r) for r in store.db.execute("SELECT * FROM events")]
    assert len(rows) == 1
    assert rows[0]["timestamp"] == 1792660000
    assert "sensitive" not in json.dumps(rows)
    assert "secret tool output" not in json.dumps(rows)


def test_cursor_partial_line_rotation_and_atomic_failure(tmp_path):
    path = tmp_path / "logs.jsonl"
    store = Store(tmp_path / "store.db")
    spec = {"sources": [{"id": "mac", "kind": "otlp", "path": str(path)}]}
    encoded = json.dumps(log())
    path.write_text(encoded[:40])
    assert collect(store, spec) == []
    assert store.checkpoint("mac")["offset"] == 0
    path.write_text(encoded + "\n")
    assert collect(store, spec) == []
    assert store.db.execute("SELECT count(*) FROM events").fetchone()[0] == 1
    previous = store.checkpoint("mac")
    with path.open("a") as f:
        f.write(json.dumps(log(request_id="next")) + "\n{broken}\n")
    assert collect(store, spec)
    assert store.checkpoint("mac") == previous
    assert store.db.execute("SELECT count(*) FROM events").fetchone()[0] == 1
    path.rename(tmp_path / "old")
    path.write_text(json.dumps(log(request_id="replacement")) + "\n")
    assert collect(store, spec) == []
    assert store.db.execute("SELECT count(*) FROM events").fetchone()[0] == 2


def test_source_is_read_only_and_missing_db_not_created(tmp_path):
    missing = tmp_path / "missing.db"
    try:
        with source_db(missing):
            raise AssertionError("opened missing source")
    except sqlite3.OperationalError:
        pass
    assert not missing.exists()
    with sqlite3.connect(missing) as db:
        db.execute("CREATE TABLE test (id INTEGER)")
    with source_db(missing) as db:
        try:
            db.execute("INSERT INTO test VALUES (1)")
            raise AssertionError("source mutation allowed")
        except sqlite3.OperationalError:
            pass


def test_retention_redaction_and_restore_snapshot(tmp_path):
    store = Store(tmp_path / "store.db")
    now = time.time()
    store.event("source", "old", "message", timestamp=now - 31 * 86400, detail="old")
    store.event(
        "source",
        "failure",
        "message",
        timestamp=now - 40 * 86400,
        status="error",
        detail="Bearer dangerous",
    )
    store.event("source", "new", "message", timestamp=now, detail="api_key=secret password=other")
    rows = list(store.db.execute("SELECT text FROM details"))
    assert len(rows) == 2
    assert all("secret" not in r[0] and "dangerous" not in r[0] for r in rows)
    target = tmp_path / "snapshot.db"
    store.snapshot(target)
    with sqlite3.connect(target) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 3
    assert target.stat().st_mode & 0o077 == 0
    assert (
        scrub("-----BEGIN PRIVATE KEY-----\nsecret\n-----END PRIVATE KEY-----")
        == "[redacted private key]"
    )


def test_quarantined_gap_and_replay_are_visible_without_raw_payload(tmp_path):
    path = tmp_path / "logs.jsonl"
    store = Store(tmp_path / "store.db")
    spec = {
        "sources": [{"id": "mac", "kind": "otlp", "path": str(path), "quarantine_malformed": True}]
    }
    path.write_text(
        json.dumps(log(**{"http.response.status_code": "429"})) + "\n{broken secret}\n"
    )
    assert collect(store, spec) == []
    assert store.checkpoint("mac")["malformed_lines"] == 1
    assert store.db.execute("SELECT status FROM sources").fetchone()[0] == "partial"
    assert (
        store.db.execute("SELECT status FROM events WHERE kind='codex.api_request'").fetchone()[0]
        == "429"
    )
    assert "secret" not in str([dict(r) for r in store.db.execute("SELECT * FROM events")])
    store.db.execute("UPDATE sources SET checkpoint='{}'")
    store.db.commit()
    assert collect(store, spec) == []
    assert store.checkpoint("mac")["malformed_lines"] == 1
    assert store.db.execute("SELECT count(*) FROM events").fetchone()[0] == 2


def test_zcode_request_projection_excludes_private_io(tmp_path):
    from omnigent.trace_store.adapters import zcode_io_record

    store = Store(tmp_path / "store.db")
    row = {
        "type": "model_io",
        "requestId": "r",
        "sessionId": "s",
        "turnId": "t",
        "startedAt": "2026-10-22T12:00:00Z",
        "model": {"modelId": "requested"},
        "request": {"headers": {"Authorization": "secret"}, "messages": ["private"]},
        "response": {
            "modelId": "actual",
            "reasoningText": "private reasoning",
            "usage": {"inputTokens": 100, "outputTokens": 20, "cacheReadTokens": 50},
        },
    }
    zcode_io_record(store, "zcode", row)
    zcode_io_record(store, "zcode", row)
    event = dict(store.db.execute("SELECT * FROM events").fetchone())
    assert event["model"] == "actual" and event["input_tokens"] == 100
    assert "private" not in str(event) and "secret" not in str(event)
    assert store.db.execute("SELECT count(*) FROM events").fetchone()[0] == 1


def test_hex_ids_and_latest_human_outcome_projection(tmp_path):
    import uuid

    from omnigent.trace_store.adapters import omnigent

    path = tmp_path / "source.db"
    sid = uuid.uuid4()
    with sqlite3.connect(path) as db:
        db.executescript("""
        CREATE TABLE conversations(id BLOB,created_at TEXT,updated_at TEXT,
            session_overrides TEXT,parent_conversation_id BLOB);
        CREATE TABLE omnigent_conversation_metadata(id BLOB,external_session_id TEXT,
            session_usage TEXT);
        CREATE TABLE conversation_labels(conversation_id BLOB,key TEXT,value TEXT);
        CREATE TABLE conversation_items(id BLOB,conversation_id BLOB,response_id TEXT,
            created_at TEXT,created_by TEXT,type INTEGER,status INTEGER,
            position INTEGER,data TEXT);
        """)
        db.execute(
            "INSERT INTO conversations VALUES (?,?,?,?,?)",
            [sid.bytes, "2026-10-22", "2026-10-22", "{}", None],
        )
        for i, outcome in enumerate(["failed", "success"]):
            data = {
                "resource_type": "task-success-experiment",
                "resource": {
                    "conversation_id": sid.hex,
                    "kind": "outcome",
                    "outcome": outcome,
                    "review_source": "human",
                },
            }
            db.execute(
                "INSERT INTO conversation_items VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    uuid.uuid4().bytes,
                    sid.bytes,
                    "turn",
                    "2026-10-22",
                    "owner",
                    8,
                    1,
                    i,
                    json.dumps(data),
                ],
            )
    store = Store(tmp_path / "analysis.db")
    omnigent(store, "omni", path)
    outcomes = list(store.db.execute("SELECT * FROM human_outcomes"))
    assert len(outcomes) == 1 and outcomes[0]["outcome"] == "success"
    assert outcomes[0]["session_id"] == sid.hex
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO conversation_labels VALUES (?,?,?)",
            [sid.bytes, "omnigent.test.run_id", "test"],
        )
    omnigent(store, "omni", path)
    assert store.db.execute("SELECT count(*) FROM human_outcomes").fetchone()[0] == 0
