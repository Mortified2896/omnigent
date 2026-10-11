"""Loopback-only historical review queue; source databases remain read-only.

Human judgments are append-only in a separate private file. No scoring model,
provider request, source-database mutation, or production router is enabled.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sqlite3
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


def codex_index(database: Path) -> list[dict]:
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute("""SELECT id,title,cwd,model,reasoning_effort,
            updated_at,rollout_path,source FROM threads ORDER BY updated_at DESC""")
        return [
            {**dict(row), "source": "codex"} for row in rows if "subagent" not in row["source"]
        ]


def sqlite_read(path: Path):
    # ZCode's checkpointed stores use no WAL. Immutable reads avoid creating
    # journal/SHM files. Refuse this path if a WAL exists rather than omit it.
    if path.with_name(path.name + "-wal").exists():
        raise ValueError("Checkpointed ZCode read requires an absent WAL")
    connection = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def tool_text(value) -> str:
    """Keep tool text readable without dumping binary image/audio evidence."""
    if isinstance(value, list):
        return "\n".join(tool_text(item) for item in value)
    if isinstance(value, dict):
        if value.get("type") in {"image", "audio", "input_image", "output_image"}:
            return "[Media evidence retained in the original trace]"
        if "text" in value:
            return str(value["text"])
        if "content" in value:
            return tool_text(value["content"])
        return json.dumps(value, indent=2)
    return str(value)


def zcode_index(database: Path) -> list[dict]:
    if not database.exists():
        return []
    before = database.stat()
    with sqlite_read(database) as connection:
        rows = connection.execute("""SELECT id,title,directory AS cwd,
            time_updated/1000 AS updated_at
            FROM session ORDER BY time_updated DESC""")
        result = []
        for row in rows:
            models = connection.execute(
                "SELECT model_id,variant FROM model_usage WHERE session_id=? "
                "ORDER BY started_at DESC LIMIT 1",
                (row["id"],),
            ).fetchone()
            result.append(
                {
                    **dict(row),
                    "source": "zcode",
                    "model": models[0] if models else None,
                    "reasoning_effort": models[1] if models else None,
                }
            )
    after = database.stat()
    if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
        raise ValueError("ZCode database changed during inspection; retry the read")
    return result


def zcode_trace(database: Path, session: str) -> list[dict]:
    before = database.stat()
    items = []
    with sqlite_read(database) as connection:
        messages = connection.execute(
            "SELECT id,data,time_created FROM message WHERE session_id=? "
            "ORDER BY sequence,time_created",
            (session,),
        )
        for row in messages:
            message = json.loads(row["data"])
            role = message.get("role")
            if role not in {"user", "assistant"}:
                continue
            parts = [
                json.loads(p[0])
                for p in connection.execute(
                    "SELECT data FROM part WHERE message_id=? ORDER BY sequence,time_created",
                    (row["id"],),
                )
            ]
            usage = connection.execute(
                "SELECT model_id,variant,turn_id FROM model_usage WHERE assistant_message_id=? "
                "ORDER BY started_at DESC LIMIT 1",
                (row["id"],),
            ).fetchone()
            base = {
                "model": usage[0] if usage else message.get("modelId"),
                "effort": usage[1] if usage else None,
                "turn_id": usage[2] if usage else None,
                "timestamp": datetime.fromtimestamp(
                    row["time_created"] / 1000, timezone.utc
                ).isoformat(),
                "message_id": row["id"],
            }
            for part in parts:
                if part.get("type") == "text" and part.get("text"):
                    items.append({**base, "kind": role, "text": part["text"]})
                elif part.get("type") == "tool":
                    state = part.get("state") or {}
                    items.append(
                        {
                            **base,
                            "kind": part.get("tool") or "tool",
                            "text": json.dumps(
                                {k: state.get(k) for k in ("status", "input", "output")}, indent=2
                            ),
                        }
                    )
    after = database.stat()
    if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
        raise ValueError("ZCode database changed during inspection; retry the read")
    return items


def omnigent_index(database: Path) -> list[dict]:
    if not database.exists():
        return []
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        return [
            {
                "id": bytes(r["id"]).hex() if isinstance(r["id"], bytes) else r["id"],
                "title": r["title"],
                "source": "omnigent",
                "is_test": connection.execute(
                    "SELECT count(*) FROM conversation_labels "
                    "WHERE conversation_id=? AND key='omnigent.test.run_id'",
                    (r["id"],),
                ).fetchone()[0]
                > 0,
                "cwd": "omnigent-mac",
                "updated_at": r["updated_at"],
                "model": None,
                "reasoning_effort": None,
            }
            for r in connection.execute("SELECT * FROM conversations ORDER BY updated_at DESC")
        ]


def omnigent_trace(database: Path, session: str) -> list[dict]:
    items, attributions = [], {}
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        rows = list(
            connection.execute(
                "SELECT * FROM conversation_items WHERE conversation_id=? ORDER BY position",
                (bytes.fromhex(session),),
            )
        )
        for row in rows:
            data = json.loads(row["data"])
            if data.get("resource_type") == "response-execution":
                payload = data.get("resource") or {}
                if payload.get("conversation_id") == session:
                    attributions[row["response_id"]] = payload
        for row in rows:
            data = json.loads(row["data"])
            attr = attributions.get(row["response_id"], {})
            base = {
                "model": attr.get("actual_model"),
                "effort": attr.get("reasoning_effort"),
                "turn_id": row["response_id"],
                "timestamp": datetime.fromtimestamp(row["created_at"], timezone.utc).isoformat(),
            }
            if data.get("role") in {"user", "assistant"}:
                content = data.get("content") or []
                text = (
                    "\n".join(x.get("text", "") for x in content if isinstance(x, dict))
                    if isinstance(content, list)
                    else str(content)
                )
                if text:
                    items.append({**base, "kind": data["role"], "text": text})
            elif row["type"] in (2, 3):
                items.append(
                    {
                        **base,
                        "kind": data.get("name") or "tool result",
                        "text": str(data.get("arguments") or data.get("output") or ""),
                    }
                )
    return items


def codex_trace(path: Path) -> list[dict]:
    """Expose transcript items, not system/developer instructions or hidden reasoning."""
    items = []
    model = effort = turn = None
    with path.open() as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            data = record.get("payload") or {}
            if record.get("type") == "turn_context":
                model, effort, turn = data.get("model"), data.get("effort"), data.get("turn_id")
            elif record.get("type") == "response_item":
                kind = data.get("type")
                role = data.get("role")
                if kind == "message" and role in {"user", "assistant"}:
                    phase = data.get("channel") or data.get("phase")
                    if phase == "analysis":
                        continue
                    content = data.get("content") or []
                    text = "\n".join(x.get("text", "") for x in content if isinstance(x, dict))
                    if role == "user" and text.lstrip().startswith(
                        (
                            "# AGENTS.md instructions",
                            "<environment_context>",
                            "<permissions instructions>",
                            "<external_",
                            "<app-context>",
                            "<codex_apps_client_time_context>",
                            "<codex_internal_context",
                        )
                    ):
                        continue
                    if text:
                        items.append(
                            {
                                "kind": role,
                                "channel": "final" if phase == "final_answer" else phase,
                                "text": text,
                                "model": model,
                                "effort": effort,
                                "turn_id": turn,
                                "timestamp": record.get("timestamp"),
                            }
                        )
                elif kind in {
                    "function_call",
                    "function_call_output",
                    "custom_tool_call",
                    "custom_tool_call_output",
                }:
                    text = data.get("arguments") or data.get("output") or data.get("input") or ""
                    items.append(
                        {
                            "kind": data.get("name") or kind,
                            "item_type": kind,
                            "call_id": data.get("call_id"),
                            "text": tool_text(text),
                            "model": model,
                            "effort": effort,
                            "turn_id": turn,
                            "timestamp": record.get("timestamp"),
                        }
                    )
    return items


HTML = Path(__file__).with_suffix(".html").read_text()


def serve(database: Path, output: Path, port: int) -> None:
    zcode_database = Path.home() / ".zcode/cli/db/db.sqlite"
    omnigent_database = Path.home() / ".local/share/omnigent-mac/data/chat.db"
    rows = codex_index(database) + zcode_index(zcode_database) + omnigent_index(omnigent_database)
    for row in rows:
        row["origin_id"] = row["id"]
        row["id"] = row["source"] + ":" + row["id"]
    rows.sort(key=lambda r: r["updated_at"], reverse=True)
    indexed = {r["id"]: r for r in rows}

    def trace_for(row):
        if row["source"] == "zcode":
            return zcode_trace(zcode_database, row["origin_id"])
        if row["source"] == "omnigent":
            return omnigent_trace(omnigent_database, row["origin_id"])
        return codex_trace(Path(row["rollout_path"]))

    token = secrets.token_urlsafe(32)
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(output.parent, 0o700)

    class Handler(BaseHTTPRequestHandler):
        def valid_host(self):
            return self.headers.get("Host") in {f"127.0.0.1:{port}", f"localhost:{port}"}

        def reply(self, status, payload, content_type="application/json"):
            encoded = (
                payload.encode() if isinstance(payload, str) else json.dumps(payload).encode()
            )
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                "connect-src 'self'; frame-ancestors 'none'",
            )
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self):
            if not self.valid_host():
                return self.reply(403, {"error": "Invalid host"})
            parsed = urlparse(self.path)
            if parsed.path == "/favicon.ico":
                return self.reply(204, "")
            if parsed.path == "/":
                return self.reply(200, HTML.replace("__CSRF__", token), "text/html; charset=utf-8")
            if parsed.path == "/api/chats":
                return self.reply(
                    200,
                    [
                        {
                            **{
                                k: r[k]
                                for k in (
                                    "id",
                                    "title",
                                    "source",
                                    "model",
                                    "reasoning_effort",
                                    "updated_at",
                                )
                            },
                            "project": Path(r["cwd"]).name,
                            "is_test": r.get("is_test", False),
                        }
                        for r in rows
                    ],
                )
            if parsed.path == "/api/trace":
                identity = parse_qs(parsed.query).get("id", [None])[0]
                row = indexed.get(identity)
                if row:
                    try:
                        trace = trace_for(row)
                        for item in trace:
                            item["checkpoint_digest"] = hashlib.sha256(
                                json.dumps(item, sort_keys=True).encode()
                            ).hexdigest()
                        return self.reply(200, trace)
                    except (OSError, ValueError):
                        return self.reply(503, {"error": "Trace unavailable"})
            if parsed.path == "/api/reviews":
                identity = parse_qs(parsed.query).get("id", [None])[0]
                if identity not in indexed:
                    return self.reply(404, {"error": "Chat not found"})
                latest = {}
                if output.exists():
                    with output.open() as handle:
                        for line in handle:
                            try:
                                record = json.loads(line)
                            except ValueError:
                                continue
                            if record.get("source_id") == identity:
                                latest[record.get("checkpoint_digest")] = record
                return self.reply(200, list(latest.values()))
            if parsed.path == "/api/assessments":
                identity = parse_qs(parsed.query).get("id", [None])[0]
                if identity not in indexed:
                    return self.reply(404, {"error": "Chat not found"})
                path = output.parent / "assistant-assessments.jsonl"
                latest = {}
                if path.exists():
                    with path.open() as handle:
                        for line in handle:
                            try:
                                record = json.loads(line)
                            except ValueError:
                                continue
                            if record.get("source_id") == identity:
                                latest[record.get("checkpoint_digest")] = record
                return self.reply(200, list(latest.values()))
            return self.reply(404, {"error": "Not found"})

        def do_POST(self):
            if not self.valid_host() or self.headers.get("X-Review-Token") != token:
                return self.reply(403, {"error": "Invalid review token"})
            if self.headers.get("Origin") not in {
                f"http://127.0.0.1:{port}",
                f"http://localhost:{port}",
            }:
                return self.reply(403, {"error": "Invalid origin"})
            if (
                self.path != "/api/review"
                or self.headers.get("Content-Type") != "application/json"
            ):
                return self.reply(400, {"error": "Invalid request"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 16384:
                    raise ValueError
                payload = json.loads(self.rfile.read(length))
                identity = payload["source_id"]
                if identity not in indexed or payload["source"] != indexed[identity]["source"]:
                    raise ValueError
                checkpoint = payload["checkpoint"]
                trace = trace_for(indexed[identity])
                if (
                    type(checkpoint) is not int
                    or checkpoint < 0
                    or checkpoint >= len(trace)
                    or trace[checkpoint]["kind"] != "assistant"
                ):
                    raise ValueError
                expected = hashlib.sha256(
                    json.dumps(trace[checkpoint], sort_keys=True).encode()
                ).hexdigest()
                if payload.get("checkpoint_digest") != expected:
                    return self.reply(409, {"error": "Checkpoint changed; reload the chat"})
                if payload["outcome"] not in {"success", "partial", "failed", "not_sure"}:
                    raise ValueError
                if payload["task_type"] not in {
                    "unknown",
                    "Coding / repository change",
                    "Infrastructure / terminal",
                    "Research / analysis",
                    "Browser / computer use",
                    "Writing / creative",
                    "Mixed workflow",
                }:
                    raise ValueError
                if not isinstance(payload["comment"], str) or len(payload["comment"]) > 4000:
                    raise ValueError
                tags = payload["tags"]
                if (
                    not isinstance(tags, list)
                    or len(tags) > 8
                    or any(not isinstance(t, str) or len(t) > 64 for t in tags)
                ):
                    raise ValueError
                for field in ("score_eligible", "synthetic", "intervention"):
                    if type(payload[field]) is not bool:
                        raise ValueError
                record = {
                    k: payload[k]
                    for k in (
                        "source",
                        "source_id",
                        "checkpoint",
                        "outcome",
                        "comment",
                        "tags",
                        "score_eligible",
                        "synthetic",
                        "intervention",
                        "task_type",
                    )
                }
                record.update(
                    schema_version=1,
                    review_source="human",
                    created_at=datetime.now(timezone.utc).isoformat(),
                    turn_id=trace[checkpoint]["turn_id"],
                    checkpoint_digest=expected,
                    model=trace[checkpoint]["model"],
                    reasoning_effort=trace[checkpoint]["effort"],
                    synthetic=payload["synthetic"] or indexed[identity].get("is_test", False),
                    score_eligible=payload["score_eligible"]
                    and not payload["synthetic"]
                    and not indexed[identity].get("is_test", False),
                )
                fd = os.open(output, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
                with os.fdopen(fd, "a") as handle:
                    handle.write(json.dumps(record) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                self.reply(200, {"saved": True})
            except (ValueError, KeyError, TypeError, IndexError):
                self.reply(400, {"error": "Invalid review"})

        def log_message(self, *_args):
            pass

    print(f"Task review: http://127.0.0.1:{port} · {len(rows)} chats", flush=True)
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path.home() / ".codex/state_5.sqlite")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path.home() / ".local/share/omnigent-routing-research/human-reviews.jsonl",
    )
    parser.add_argument("--port", type=int, default=3847)
    args = parser.parse_args()
    serve(args.database.resolve(), args.output.resolve(), args.port)
