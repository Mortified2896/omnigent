"""Local Anthropic Messages surface over a CLI-owned Codex subscription.

Claude owns tool execution. A dynamic-tool request remains pending in Codex
until Claude returns its result; no OAuth token is read or replayed here.
Run with ``python -m ...codex_bridge --config <private JSON> --port <port>``.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import copy
import hashlib
import hmac
import json
import re
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import uvicorn
import websockets
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

BRIDGE_PROTOCOL = "omnigent-codex-subscription-bridge-v1"

# Max-effort reasoning can be silent for several minutes. Bound inactivity,
# not the total response duration: each Codex event renews this wait.
_RESPONSE_IDLE_TIMEOUT_SECONDS = 600


class RpcRejected(RuntimeError):
    def __init__(self, method: str, error: dict):
        super().__init__("Codex rejected " + method)
        self.operation = method
        self.code = error.get("code")
        message = str(error.get("message", "")).lower()
        self.categories = [
            word
            for word in (
                "tool",
                "schema",
                "name",
                "duplicate",
                "cwd",
                "properties",
                "required",
                "additional",
                "namespace",
            )
            if word in message
        ]


def usage_delta(total: dict, previous: dict) -> dict[str, int]:
    """Anthropic input excludes cache; Codex input includes it."""

    def growth(key: str) -> int:
        return max(0, int(total.get(key, 0)) - int(previous.get(key, 0)))

    cached = growth("cachedInputTokens")
    written = growth("cacheWriteInputTokens")
    return {
        "input_tokens": max(0, growth("inputTokens") - cached - written),
        "output_tokens": growth("outputTokens"),
        "cache_read_input_tokens": cached,
        "cache_creation_input_tokens": written,
    }


class Rpc:
    def __init__(self, ws):
        self.ws = ws
        self.sequence = 0
        self.waiters: dict[int, asyncio.Future] = {}
        self.events: asyncio.Queue = asyncio.Queue()
        self.reader = asyncio.create_task(self.read())

    async def read(self):
        try:
            async for raw in self.ws:
                row = json.loads(raw)
                if "method" not in row and row.get("id") in self.waiters:
                    self.waiters.pop(row["id"]).set_result(row)
                else:
                    await self.events.put(row)
        finally:
            for future in self.waiters.values():
                if not future.done():
                    future.set_exception(RuntimeError("Codex bridge disconnected"))
            await self.events.put({"method": "bridge/disconnected"})

    async def request(self, method, params):
        self.sequence += 1
        ident = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.waiters[ident] = future
        await self.ws.send(json.dumps({"id": ident, "method": method, "params": params}))
        row = await asyncio.wait_for(future, 30)
        if "error" in row:
            raise RpcRejected(method, row["error"])
        return row.get("result", {})

    async def close(self):
        await self.ws.close()
        self.reader.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self.reader


@dataclass
class Turn:
    rpc: Rpc
    model: str
    history: str
    tools: str
    effort: str
    thread: str
    tool_names: dict[str, str] = field(default_factory=dict)
    total: dict = field(default_factory=dict)
    recorded: dict = field(default_factory=dict)
    pending: dict = field(default_factory=dict)
    touched: float = field(default_factory=time.monotonic)
    turn_id: str = ""


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def semantic(value):
    if isinstance(value, dict):
        return {k: semantic(v) for k, v in value.items() if k != "cache_control"}
    if isinstance(value, list):
        return [semantic(v) for v in value]
    return value


def history_digest(messages: list) -> str:
    # Claude splits streamed text and tool blocks into adjacent assistant
    # messages, and adds cache annotations when replaying them.
    normalized = []
    for message in messages:
        content = message.get("content", [])
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        row = {"role": message["role"], "content": semantic(content)}
        if normalized and normalized[-1]["role"] == row["role"]:
            normalized[-1]["content"].extend(row["content"])
        else:
            normalized.append(row)
    return digest(normalized)


class Bridge:
    def __init__(self, config: dict):
        self.config = config
        self.pending: dict[str, Turn] = {}
        self.lock = asyncio.Lock()
        self.usage_audit: list[dict] = []
        self.resume_diagnostic: dict = {}
        self.last_error: dict = {}

    def key(self) -> str:
        result = subprocess.run(
            self.config["api_key_command"], capture_output=True, text=True, timeout=10, check=True
        )
        key = result.stdout.strip()
        if not key:
            raise RuntimeError("Bridge credential unavailable")
        return key

    async def authorized(self, request: Request) -> bool:
        supplied = request.headers.get("x-api-key") or request.headers.get(
            "authorization", ""
        ).removeprefix("Bearer ")
        return hmac.compare_digest(supplied, await asyncio.to_thread(self.key))

    async def connect(self) -> Rpc:
        token = Path(self.config["app_server_token_file"]).read_text().strip()
        ws = await websockets.connect(
            self.config["app_server_url"],
            additional_headers={"Authorization": "Bearer " + token},
            max_size=8_000_000,
        )
        rpc = Rpc(ws)
        try:
            await rpc.request(
                "initialize",
                {
                    "clientInfo": {"name": "omnigent-claude-codex-bridge", "version": "1"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            return rpc
        except Exception:
            await rpc.close()
            raise

    async def identity(self, request):
        if not await self.authorized(request):
            return JSONResponse({"error": {"type": "authentication_error"}}, status_code=401)
        rpc = await self.connect()
        try:
            account = (await rpc.request("account/read", {"refreshToken": False})).get("account")
            if not account or account.get("type") != "chatgpt":
                return JSONResponse(
                    {"error": "Codex subscription login required"}, status_code=503
                )
            models = (await rpc.request("model/list", {})).get("data", [])
            rows = [m for m in models if m.get("model") in self.config["models"]]
            return JSONResponse(
                {
                    "protocol": BRIDGE_PROTOCOL,
                    "provider": "openai",
                    "account_type": "chatgpt",
                    "entitlement_key": "chatgpt-plan:bridge:" + digest(account),
                    "connection_id": "codex-claude-bridge:"
                    + digest(
                        {
                            "url": self.config["app_server_url"],
                            "credential": digest(
                                Path(self.config["app_server_token_file"]).read_text()
                            ),
                        }
                    ),
                    "models": rows,
                    "usage_audit": self.usage_audit[-32:],
                    "resume_diagnostic": self.resume_diagnostic,
                    "last_error": self.last_error,
                }
            )
        finally:
            await rpc.close()

    async def discard(self, turn: Turn):
        self.pending = {k: v for k, v in self.pending.items() if v is not turn}
        with contextlib.suppress(Exception):
            await turn.rpc.request(
                "turn/interrupt",
                {"threadId": turn.thread, "turnId": turn.pending.get("turnId") or turn.turn_id},
            )
        await turn.rpc.close()

    async def start(self, body: dict) -> Turn:
        messages = body.get("messages", [])
        # Claude may append a separate user message containing reminders
        # after the tool result. Match the outstanding call in the replay.
        matches = [
            (index, block)
            for index, message in enumerate(messages)
            if isinstance(message.get("content"), list)
            for block in message["content"]
            if block.get("type") == "tool_result" and block.get("tool_use_id") in self.pending
        ]
        results = [block for _, block in matches]
        tools = digest(semantic(body.get("tools", [])))
        effort = body.get("output_config", {}).get("effort") or "medium"
        if results:
            call_id = results[0].get("tool_use_id")
            turn = self.pending.get(call_id)
            checks = {
                "pending": turn is not None,
                "single_result": len(results) == 1,
                "model": turn is not None and turn.model == body["model"],
                "effort": turn is not None
                and turn.effort == (body.get("output_config", {}).get("effort") or turn.effort),
                "history": turn is not None
                and turn.history == history_digest(messages[: matches[0][0]]),
            }
            self.resume_diagnostic = checks
            if not all(checks.values()):
                raise ValueError("Tool result does not match the pending Codex turn")
            assert turn is not None
            self.pending.pop(call_id)
            turn.history = history_digest(messages)
            await turn.rpc.ws.send(
                json.dumps(
                    {
                        "id": turn.pending["id"],
                        "result": {
                            "contentItems": [
                                {
                                    "type": "inputText",
                                    "text": json.dumps(messages[matches[0][0] :]),
                                }
                            ],
                            "success": not results[0].get("is_error", False),
                        },
                    }
                )
            )
            turn.pending = {}
            return turn
        if len(self.pending) >= 32:
            raise ValueError("Too many pending Claude tool calls")
        rpc = await self.connect()
        try:
            # Codex reserves MCP-style names. Use a private dynamic-tool
            # vocabulary, restoring Claude's names before it executes them.
            tool_names = {
                f"claude_tool_{index}": tool["name"]
                for index, tool in enumerate(body.get("tools", []))
            }
            result = await rpc.request(
                "thread/start",
                {
                    "cwd": self.config["cwd"],
                    "approvalPolicy": "never",
                    "sandbox": "read-only",
                    "config": {"shell_tool": False},
                    "dynamicTools": [
                        {
                            "name": f"claude_tool_{index}",
                            "description": "Claude Code tool: "
                            + t["name"]
                            + "\n"
                            + t.get("description", ""),
                            "inputSchema": t.get(
                                "input_schema", {"type": "object", "properties": {}}
                            ),
                        }
                        for index, t in enumerate(body.get("tools", []))
                    ],
                },
            )
            thread = result["thread"]["id"]
            # Preserve roles, system instructions and content blocks in the initial
            # request. Subsequent tool results resume this same native turn.
            client_name = self.config.get("client_name", "Claude Code")
            prompt_messages = copy.deepcopy(messages)
            images = []
            if self.config.get("allow_images", False):
                for message in prompt_messages:
                    blocks = message.get("content", [])
                    if not isinstance(blocks, list):
                        continue
                    for index, block in enumerate(blocks):
                        if block.get("type") == "image":
                            source = block["source"]
                            url = source.get("url")
                            if not url and source.get("type") == "base64":
                                url = "data:" + source["media_type"] + ";base64," + source["data"]
                            if not isinstance(url, str) or not url.startswith(
                                ("https://", "data:image/")
                            ):
                                raise ValueError("Unsupported image source")
                            images.append({"type": "image", "url": url})
                            blocks[index] = {
                                "type": "text",
                                "text": f"[Image attachment {len(images)}]",
                            }
            prompt = (
                f"You are running inside {client_name}. Use ONLY its provided function tools "
                "for actions. Do not use Codex built-in tools. Follow these system "
                "instructions and conversation:\n"
                + json.dumps(
                    {
                        "system": body.get("system", []),
                        "messages": prompt_messages,
                        "tool_names": tool_names,
                    },
                    ensure_ascii=False,
                )
            )
            turn = Turn(rpc, body["model"], history_digest(messages), tools, effort, thread)
            turn.tool_names = tool_names
            started = await rpc.request(
                "turn/start",
                {
                    "threadId": thread,
                    "model": turn.model,
                    "effort": effort,
                    "input": [{"type": "text", "text": prompt, "text_elements": []}, *images],
                },
            )
            turn.turn_id = (started.get("turn") or {}).get("id", "")
            return turn
        except Exception:
            await rpc.close()
            raise

    async def response(self, body: dict, turn: Turn):
        content = []
        text = ""
        while True:
            row = await asyncio.wait_for(turn.rpc.events.get(), _RESPONSE_IDLE_TIMEOUT_SECONDS)
            method, params = row.get("method"), row.get("params", {})
            if method == "thread/tokenUsage/updated":
                turn.total = params.get("tokenUsage", {}).get("total") or turn.total
            elif method == "item/agentMessage/delta":
                text += params.get("delta", "")
            elif method == "item/tool/call":
                if text:
                    content.append({"type": "text", "text": text})
                call_id = "toolu_" + uuid.uuid4().hex
                content.append(
                    {
                        "type": "tool_use",
                        "id": call_id,
                        "name": turn.tool_names.get(params["tool"], params["tool"]),
                        "input": params.get("arguments", {}),
                    }
                )
                turn.pending = {**params, "id": row["id"]}
                turn.history = history_digest(
                    [*body.get("messages", []), {"role": "assistant", "content": content}]
                )
                turn.touched = time.monotonic()
                self.pending[call_id] = turn
                reason = "tool_use"
                break
            elif method == "turn/completed":
                if params.get("turn", {}).get("status") != "completed":
                    raise RuntimeError("Codex turn did not complete")
                if text:
                    content.append({"type": "text", "text": text})
                reason = "end_turn"
                break
            elif method in ("error", "bridge/disconnected"):
                raise RuntimeError("Codex subscription turn failed")
            elif "id" in row and method:
                # Deny every built-in approval; Claude owns permissions.
                await turn.rpc.ws.send(
                    json.dumps({"id": row["id"], "result": {"decision": "decline"}})
                )
        usage = usage_delta(turn.total, turn.recorded)
        message_id = "msg_" + uuid.uuid4().hex
        self.usage_audit.append(
            {
                "thread": digest(turn.thread),
                "total": dict(turn.total),
                "delta": usage,
                "stop_reason": reason,
                "message_id": message_id,
            }
        )
        self.usage_audit = self.usage_audit[-32:]
        turn.recorded = dict(turn.total)
        result = {
            "id": message_id,
            "type": "message",
            "role": "assistant",
            "model": turn.model,
            "content": content,
            "stop_reason": reason,
            "stop_sequence": None,
            "usage": usage,
        }
        if reason == "end_turn":
            await turn.rpc.close()
        return result

    async def messages(self, request: Request):
        if not await self.authorized(request):
            return JSONResponse({"error": {"type": "authentication_error"}}, status_code=401)
        turn = None
        phase = "parse"
        body = {}
        try:
            body = await request.json()
            phase = "model"
            if body.get("model") not in self.config["models"]:
                raise ValueError("Model is outside the configured Codex bridge catalog")
            phase = "content"
            for message in body.get("messages", []):
                blocks = message.get("content", [])
                if isinstance(blocks, list) and any(
                    block.get("type")
                    not in {"text", "tool_use", "tool_result", "thinking", "redacted_thinking"}
                    for block in blocks
                ):
                    return JSONResponse(
                        {
                            "error": {
                                "type": "invalid_request_error",
                                "message": "Local Codex bridge supports text and function tools",
                            }
                        },
                        status_code=400,
                    )
            if any(tool.get("type", "custom") != "custom" for tool in body.get("tools", [])):
                return JSONResponse(
                    {
                        "error": {
                            "type": "invalid_request_error",
                            "message": "Server-side Anthropic tools are unsupported here",
                        }
                    },
                    status_code=400,
                )
            phase = "start"
            async with self.lock:
                for old in list(self.pending.values()):
                    if time.monotonic() - old.touched > 300:
                        await self.discard(old)
                turn = await self.start(body)
            phase = "response"
            result = await self.response(body, turn)
            if body.get("stream"):
                return StreamingResponse(message_events(result), media_type="text/event-stream")
            return JSONResponse(result)
        except asyncio.CancelledError:
            if turn:
                await self.discard(turn)
            raise
        except Exception as error:  # noqa: BLE001 — fixed response without upstream payloads
            self.last_error = {"phase": phase, "type": type(error).__name__}
            if isinstance(error, RpcRejected):
                self.last_error.update(
                    operation=error.operation, code=error.code, categories=error.categories
                )
                self.last_error["tool_count"] = len(body.get("tools", []))
            effort_config = body.get("output_config")
            effort = effort_config.get("effort") if isinstance(effort_config, dict) else None
            if isinstance(effort, str) and re.fullmatch(r"[a-z]{1,16}", effort):
                self.last_error["effort"] = effort
            model = body.get("model", "")
            if (
                phase == "model"
                and isinstance(model, str)
                and re.fullmatch(r"[\w.\-\[\]:/]{1,128}", model)
            ):
                self.last_error["model"] = model
            if turn:
                await self.discard(turn)
            # No upstream error/credential/prompt content enters local logs.
            return JSONResponse(
                {
                    "type": "error",
                    "error": {
                        "type": "api_error",
                        "message": "Local Codex bridge could not complete this request",
                    },
                },
                status_code=502,
            )


async def message_events(result):
    def event(name, payload):
        return f"event: {name}\ndata: {json.dumps(payload)}\n\n"

    yield event(
        "message_start",
        {"type": "message_start", "message": {**result, "content": [], "stop_reason": None}},
    )
    for index, block in enumerate(result["content"]):
        empty = {**block, "text": ""} if block["type"] == "text" else {**block, "input": {}}
        yield event(
            "content_block_start",
            {"type": "content_block_start", "index": index, "content_block": empty},
        )
        delta = (
            {"type": "text_delta", "text": block["text"]}
            if block["type"] == "text"
            else {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
        )
        yield event(
            "content_block_delta", {"type": "content_block_delta", "index": index, "delta": delta}
        )
        yield event("content_block_stop", {"type": "content_block_stop", "index": index})
    yield event(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": result["stop_reason"], "stop_sequence": None},
            "usage": result["usage"],
        },
    )
    yield event("message_stop", {"type": "message_stop"})


def create_app(config: dict) -> Starlette:
    bridge = Bridge(config)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        async def reap():
            while True:
                await asyncio.sleep(30)
                for turn in list(bridge.pending.values()):
                    if time.monotonic() - turn.touched > 300:
                        await bridge.discard(turn)

        reaper = asyncio.create_task(reap())
        try:
            yield
        finally:
            reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reaper
            for turn in list(bridge.pending.values()):
                await bridge.discard(turn)

    return Starlette(
        lifespan=lifespan,
        routes=[
            Route("/identity", bridge.identity),
            Route("/v1/messages", bridge.messages, methods=["POST"]),
        ],
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    uvicorn.run(
        create_app(json.loads(args.config.read_text())),
        host="127.0.0.1",
        port=args.port,
        access_log=False,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
