"""OpenAI chat surface with a gateway-first, CLI-owned subscription fallback.

This standalone companion reuses the native Codex bridge. A complete primary
response is validated before publication, so a failed gateway cannot publish
partial tool calls and then replay them through another route. Route notices
are response content, not transient notifications. No OAuth credential is read
by this adapter; the separately signed-in Codex app-server owns authentication.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import copy
import json
import time
import uuid
from pathlib import Path

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from .codex_bridge import Bridge

DIRECT_NOTICE = (
    "> ## ⚠️ DIRECT CODEX FALLBACK ACTIVE\n"
    "> OmniRoute could not complete this chat request. This response uses your "
    "Codex subscription directly. **Please investigate OmniRoute.**\n"
    "> This chat stays on the direct route; start a new chat after recovery "
    "to try OmniRoute again.\n\n---\n\n"
)
MAX_REQUEST_BYTES = 8 * 1024 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class GatewayFailure(RuntimeError):
    """A safe reason category, never an upstream payload or credential."""


class InvalidRequest(ValueError):
    pass


def direct_history(messages: list) -> bool:
    return any(
        m.get("role") == "assistant"
        and isinstance(m.get("content"), str)
        and m["content"].startswith(DIRECT_NOTICE)
        for m in messages
    )


def anthropic_request(body: dict, profile: dict) -> dict:
    """Preserve roles, tool IDs and results when resuming the native bridge."""
    messages, system = [], []
    for message in body.get("messages", []):
        role = message.get("role")
        content = message.get("content") or ""
        if isinstance(content, str):
            if role == "assistant" and content.startswith(DIRECT_NOTICE):
                content = content[len(DIRECT_NOTICE) :]
            blocks = [{"type": "text", "text": content}] if content else []
        elif isinstance(content, list):
            blocks = []
            for part in content:
                if part.get("type") in {"text", "input_text"}:
                    blocks.append({"type": "text", "text": part.get("text", "")})
                elif part.get("type") == "image_url":
                    image = part["image_url"]
                    url = image if isinstance(image, str) else image["url"]
                    blocks.append({"type": "image", "source": {"type": "url", "url": url}})
                else:
                    raise InvalidRequest(
                        "Direct fallback supports text, images and function tools"
                    )
        else:
            raise InvalidRequest("Unsupported message content")
        if role in {"system", "developer"}:
            system.extend(blocks)
            continue
        if role == "tool":
            blocks = [
                {"type": "tool_result", "tool_use_id": message["tool_call_id"], "content": blocks}
            ]
            role = "user"
        elif role == "assistant":
            for call in message.get("tool_calls") or []:
                if call.get("type") != "function":
                    raise InvalidRequest("Only function tool calls are supported")
                function = call["function"]
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call["id"],
                        "name": function["name"],
                        "input": json.loads(function.get("arguments") or "{}"),
                    }
                )
        if role not in {"user", "assistant"}:
            raise InvalidRequest("Unsupported message role")
        messages.append({"role": role, "content": blocks})
    tools = []
    for tool in body.get("tools") or []:
        if tool.get("type") != "function":
            raise InvalidRequest("Only function tools are supported")
        function = tool["function"]
        tools.append(
            {
                "name": function["name"],
                "description": function.get("description", ""),
                "input_schema": function.get("parameters", {"type": "object", "properties": {}}),
            }
        )
    return {
        "model": profile["model"],
        "messages": messages,
        "system": system,
        "tools": tools,
        "output_config": {"effort": profile["effort"]},
    }


def openai_result(result: dict, model: str) -> dict:
    text, calls = [], []
    for block in result.get("content", []):
        if block["type"] == "text":
            text.append(block["text"])
        elif block["type"] == "tool_use":
            calls.append(
                {
                    "id": block["id"],
                    "type": "function",
                    "function": {"name": block["name"], "arguments": json.dumps(block["input"])},
                }
            )
    message = {"role": "assistant", "content": "".join(text) or None}
    if calls:
        message["tool_calls"] = calls
    usage = result.get("usage", {})
    input_tokens = sum(
        usage.get(k, 0)
        for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    )
    output_tokens = usage.get("output_tokens", 0)
    return {
        "id": "chatcmpl-" + uuid.uuid4().hex,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {"index": 0, "message": message, "finish_reason": "tool_calls" if calls else "stop"}
        ],
        "usage": {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
    }


def chunks(result: dict, prefix: str = ""):
    def event(delta, finish=None):
        return (
            "data: "
            + json.dumps(
                {
                    "id": result["id"],
                    "object": "chat.completion.chunk",
                    "created": result.get("created", int(time.time())),
                    "model": result["model"],
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                }
            )
            + "\n\n"
        )

    yield event({"role": "assistant"})
    if prefix:
        yield event({"content": prefix})
    choice = result["choices"][0]
    message = choice["message"]
    if message.get("content"):
        yield event({"content": message["content"]})
    for index, call in enumerate(message.get("tool_calls") or []):
        yield event({"tool_calls": [{"index": index, **call}]})
    yield event({}, choice.get("finish_reason") or "stop")
    if result.get("usage"):
        yield (
            "data: "
            + json.dumps(
                {
                    "id": result["id"],
                    "object": "chat.completion.chunk",
                    "created": result.get("created", int(time.time())),
                    "model": result["model"],
                    "choices": [],
                    "usage": result["usage"],
                }
            )
            + "\n\n"
        )
    yield "data: [DONE]\n\n"


class OpenAIBridge:
    def __init__(self, config: dict, native: Bridge | None = None, transport=None):
        self.config = config
        self.native = native or Bridge(config)
        self.profiles = {p["id"]: p for p in config["model_profiles"]}
        self.direct_slots = asyncio.Semaphore(config.get("direct_concurrency", 1))
        self.request_slots = asyncio.Semaphore(config.get("max_requests", 8))
        self.transport = transport
        self.route_counts = {"omniroute": 0, "direct": 0, "failed": 0}

    async def models(self, request):
        if not await self.native.authorized(request):
            return JSONResponse({"error": {"code": "unauthorized"}}, status_code=401)
        return JSONResponse(
            {
                "object": "list",
                "data": [
                    {
                        "id": p["id"],
                        "object": "model",
                        "created": 0,
                        "owned_by": "codex-subscription",
                        "name": p.get("name", p["id"]),
                    }
                    for p in self.profiles.values()
                ],
            }
        )

    async def primary(self, body: dict, profile: dict) -> dict:
        payload = copy.deepcopy(body)
        payload["model"] = "codex/" + profile["model"]
        payload["reasoning_effort"] = body.get("reasoning_effort") or profile["effort"]
        payload["stream"] = False
        payload.pop("stream_options", None)
        key = Path(self.config["primary_key_file"]).read_text().strip()
        timeout = httpx.Timeout(self.config.get("primary_idle_timeout", 600), connect=5)
        async with (
            httpx.AsyncClient(
                transport=self.transport, timeout=timeout, trust_env=False, follow_redirects=False
            ) as client,
            client.stream(
                "POST",
                self.config["primary_base_url"] + "/chat/completions",
                headers={"Authorization": "Bearer " + key},
                json=payload,
            ) as response,
        ):
            if response.status_code in {400, 413, 422}:
                raise InvalidRequest(
                    "OmniRoute rejected this request; check its size and settings"
                )
            if response.status_code != 200:
                raise GatewayFailure("upstream_http_error")
            parts, size = [], 0
            async for part in response.aiter_bytes():
                size += len(part)
                if size > MAX_RESPONSE_BYTES:
                    raise GatewayFailure("response_too_large")
                parts.append(part)
        result = json.loads(b"".join(parts))
        if not isinstance(result, dict) or result.get("error") or not result.get("choices"):
            raise GatewayFailure("invalid_completion")
        choice = result["choices"][0]
        if choice.get("finish_reason") not in {"stop", "length", "tool_calls", "content_filter"}:
            raise GatewayFailure("incomplete_completion")
        if not isinstance(choice.get("message"), dict):
            raise GatewayFailure("invalid_completion")
        result["model"] = body["model"]
        return result

    async def direct(self, body: dict, profile: dict) -> dict:
        native_body = anthropic_request(
            body, {**profile, "effort": body.get("reasoning_effort") or profile["effort"]}
        )
        try:
            await asyncio.wait_for(self.direct_slots.acquire(), 60)
        except TimeoutError as error:
            raise GatewayFailure("direct_capacity_busy") from error
        turn = None
        try:
            async with self.native.lock:
                turn = await self.native.start(native_body)
            result = await self.native.response(native_body, turn)
            return openai_result(result, body["model"])
        except BaseException:
            if turn:
                await self.native.discard(turn)
            raise
        finally:
            self.direct_slots.release()

    async def resolve(self, body: dict, profile: dict, notice=None):
        sticky = direct_history(body["messages"])
        if not sticky:
            try:
                result = await self.primary(body, profile)
                self.route_counts["omniroute"] += 1
                return result, False
            except (GatewayFailure, httpx.HTTPError, json.JSONDecodeError, OSError):
                pass
        if notice:
            notice.set()
        result = await self.direct(body, profile)
        self.route_counts["direct"] += 1
        return result, True

    async def completions(self, request: Request):
        if not await self.native.authorized(request):
            return JSONResponse({"error": {"code": "unauthorized"}}, status_code=401)
        body_bytes = bytearray()
        async for block in request.stream():
            body_bytes.extend(block)
            if len(body_bytes) > MAX_REQUEST_BYTES:
                return JSONResponse(
                    {"error": {"message": "Chat body exceeds the 8 MiB limit"}}, status_code=413
                )
        try:
            body = json.loads(body_bytes)
            if not isinstance(body.get("messages"), list) or not body["messages"]:
                raise InvalidRequest("A non-empty messages list is required")
            profile = self.profiles.get(body.get("model"))
            if not profile:
                raise InvalidRequest("Model is outside the configured subscription catalog")
            effort = body.get("reasoning_effort") or profile["effort"]
            if effort not in profile["supported_efforts"]:
                raise InvalidRequest("Reasoning effort is unsupported by this model")
        except (ValueError, TypeError, AttributeError) as error:
            return JSONResponse(
                {
                    "error": {
                        "message": str(error)
                        if isinstance(error, InvalidRequest)
                        else "Invalid chat request"
                    }
                },
                status_code=400,
            )

        notice = asyncio.Event()

        async def resolve_bounded():
            acquired = False
            try:
                await asyncio.wait_for(self.request_slots.acquire(), 30)
                acquired = True
                return await self.resolve(body, profile, notice)
            finally:
                if acquired:
                    self.request_slots.release()

        if not body.get("stream"):
            try:
                result, direct = await resolve_bounded()
                if direct:
                    message = result["choices"][0]["message"]
                    message["content"] = DIRECT_NOTICE + (message.get("content") or "")
                return JSONResponse(
                    result, headers={"X-Codex-Route": "direct" if direct else "omniroute"}
                )
            except InvalidRequest as error:
                return JSONResponse({"error": {"message": str(error)}}, status_code=400)
            except Exception:  # noqa: BLE001 — fixed error boundary; keep upstream payloads private
                self.route_counts["failed"] += 1
                return JSONResponse(
                    {
                        "error": {
                            "code": "codex_routes_unavailable",
                            "message": DIRECT_NOTICE
                            + "Both routes failed. Your original prompt remains in this chat. "
                            "Check the services and retry.",
                        }
                    },
                    status_code=502,
                )

        async def stream():
            task = asyncio.create_task(resolve_bounded())
            warned = False
            response_id = "chatcmpl-" + uuid.uuid4().hex
            try:
                while not task.done():
                    await asyncio.wait({task}, timeout=10)
                    if notice.is_set() and not warned and not task.done():
                        yield (
                            "data: "
                            + json.dumps(
                                {
                                    "id": response_id,
                                    "object": "chat.completion.chunk",
                                    "model": body["model"],
                                    "choices": [
                                        {
                                            "index": 0,
                                            "delta": {
                                                "role": "assistant",
                                                "content": DIRECT_NOTICE,
                                            },
                                            "finish_reason": None,
                                        }
                                    ],
                                }
                            )
                            + "\n\n"
                        )
                        warned = True
                    if not task.done():
                        yield ": waiting for a complete response\n\n"
                result, direct = await task
                result["id"] = response_id
                for chunk in chunks(result, DIRECT_NOTICE if direct and not warned else ""):
                    yield chunk
            except InvalidRequest as error:
                yield "data: " + json.dumps({"error": {"message": str(error)}}) + "\n\n"
            except Exception:  # noqa: BLE001 — fixed error boundary; keep upstream payloads private
                self.route_counts["failed"] += 1
                message = (
                    DIRECT_NOTICE
                    + "Both routes failed. Your original prompt remains in this chat. "
                    "Check the services and retry."
                )
                result = openai_result(
                    {"content": [{"type": "text", "text": message}]}, body["model"]
                )
                for chunk in chunks(result):
                    yield chunk
            finally:
                if not task.done():
                    task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )


def create_app(config: dict, native=None, transport=None) -> Starlette:
    bridge = OpenAIBridge(config, native=native, transport=transport)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        async def reap():
            while True:
                await asyncio.sleep(30)
                for turn in list(bridge.native.pending.values()):
                    if time.monotonic() - turn.touched > 300:
                        await bridge.native.discard(turn)

        reaper = asyncio.create_task(reap())
        try:
            yield
        finally:
            reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reaper
            for turn in list(bridge.native.pending.values()):
                await bridge.native.discard(turn)

    async def health(_request):
        return JSONResponse({"status": "ok"})

    app = Starlette(
        lifespan=lifespan,
        routes=[
            Route("/health", health),
            Route("/identity", bridge.native.identity),
            Route("/v1/models", bridge.models),
            Route("/v1/chat/completions", bridge.completions, methods=["POST"]),
        ],
    )
    app.state.bridge = bridge
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args()
    uvicorn.run(
        create_app(json.loads(args.config.read_text())),
        host=args.host,
        port=args.port,
        access_log=False,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
