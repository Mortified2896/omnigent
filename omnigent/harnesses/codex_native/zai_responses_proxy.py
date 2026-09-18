"""Small Responses-to-Chat adapter for the direct Z.ai GLM lane.

The Z.ai Coding Plan endpoint is OpenAI-compatible at the Chat Completions
surface, while current native Codex releases only accept a Responses provider
configuration. The adapter is deliberately session-local: it binds to an
ephemeral loopback port, keeps the provider key in memory, and is stopped with
the Codex app-server. It is not a general-purpose proxy.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from omnigent.models.glm_model_vocabulary import (
    GLM_CODING_BASE_URL,
    GLM_DIRECT_MODELS,
)

_logger = logging.getLogger(__name__)
_MAX_REQUEST_BYTES = 16 * 1024 * 1024
_UPSTREAM_TIMEOUT_SECONDS = 120


@dataclass(frozen=True)
class ZaiResponsesProxyConfig:
    """In-memory configuration for one session-local adapter."""

    api_key: str = field(repr=False)
    base_url: str = GLM_CODING_BASE_URL


@dataclass
class ZaiResponsesProxy:
    """Running loopback adapter owned by one Codex app-server."""

    server: ThreadingHTTPServer
    thread: threading.Thread
    base_url: str

    def close(self) -> None:
        """Stop the adapter and release its loopback port."""
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return "" if content is None else str(content)
    parts: list[str] = []
    for block in content:
        if not isinstance(block, Mapping):
            continue
        block_type = block.get("type")
        if block_type in {"input_text", "output_text", "text"}:
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
        elif block_type in {"input_image", "input_file"}:
            # The direct lane is text-first. Preserve the fact that a block
            # was present without inventing provider capability metadata.
            parts.append("[attachment omitted by direct GLM adapter]")
    return "\n".join(parts)


def _responses_input_to_messages(request: Mapping[str, object]) -> list[dict[str, object]]:
    messages: list[dict[str, object]] = []
    instructions = request.get("instructions")
    if isinstance(instructions, str) and instructions:
        messages.append({"role": "system", "content": instructions})
    raw_input = request.get("input")
    if isinstance(raw_input, str):
        return [*messages, {"role": "user", "content": raw_input}]
    if not isinstance(raw_input, list):
        return messages

    for item in raw_input:
        if not isinstance(item, Mapping):
            continue
        item_type = item.get("type")
        if item_type == "message" or "role" in item:
            role = item.get("role")
            role_name = "system" if role in {"system", "developer"} else "user"
            if role == "assistant":
                role_name = "assistant"
            messages.append({"role": role_name, "content": _content_text(item.get("content"))})
        elif item_type == "function_call_output":
            call_id = item.get("call_id")
            message: dict[str, object] = {
                "role": "tool",
                "content": _content_text(item.get("output")),
            }
            if isinstance(call_id, str) and call_id:
                message["tool_call_id"] = call_id
            messages.append(message)
        elif item_type == "function_call":
            name = item.get("name")
            arguments = item.get("arguments")
            if isinstance(name, str) and name:
                call = {
                    "id": item.get("call_id") or item.get("id") or f"call_{uuid.uuid4().hex}",
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": (
                            arguments if isinstance(arguments, str) else json.dumps(arguments)
                        ),
                    },
                }
                if messages and messages[-1].get("role") == "assistant":
                    tool_calls = messages[-1].get("tool_calls")
                    if isinstance(tool_calls, list):
                        tool_calls.append(call)
                    else:
                        messages[-1]["tool_calls"] = [call]
                else:
                    messages.append({"role": "assistant", "content": None, "tool_calls": [call]})
    return messages


def _responses_tools_to_chat(tools: object) -> list[dict[str, object]]:
    if not isinstance(tools, list):
        return []
    converted: list[dict[str, object]] = []
    for tool in tools:
        if not isinstance(tool, Mapping) or tool.get("type") != "function":
            continue
        function = tool.get("function")
        if isinstance(function, Mapping):
            converted.append({"type": "function", "function": dict(function)})
            continue
        name = tool.get("name")
        if not isinstance(name, str) or not name:
            continue
        converted.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": tool.get("description", ""),
                    "parameters": tool.get("parameters", {"type": "object", "properties": {}}),
                },
            }
        )
    return converted


def responses_request_to_chat(request: Mapping[str, object]) -> dict[str, object]:
    """Translate the stable subset of a Codex Responses request to Chat."""
    payload: dict[str, object] = {
        "model": request.get("model", "glm-5.3"),
        "messages": _responses_input_to_messages(request),
        "stream": False,
    }
    tools = _responses_tools_to_chat(request.get("tools"))
    if tools:
        payload["tools"] = tools
    tool_choice = request.get("tool_choice")
    if tool_choice is not None:
        payload["tool_choice"] = tool_choice
    max_output_tokens = request.get("max_output_tokens")
    if isinstance(max_output_tokens, int) and max_output_tokens > 0:
        payload["max_tokens"] = max_output_tokens
    reasoning = request.get("reasoning")
    if isinstance(reasoning, Mapping):
        effort = reasoning.get("effort")
        payload["thinking"] = {"type": "disabled" if effort == "none" else "enabled"}
    return payload


def _response_output_from_chat(chat: Mapping[str, object], model: str) -> dict[str, object]:
    response_id = f"resp_zai_{uuid.uuid4().hex}"
    choices = chat.get("choices")
    first = choices[0] if isinstance(choices, list) and choices else {}
    message = first.get("message") if isinstance(first, Mapping) else {}
    if not isinstance(message, Mapping):
        message = {}
    text = message.get("content")
    output: list[dict[str, object]] = []
    if isinstance(text, str) and text:
        output.append(
            {
                "id": f"msg_{response_id}",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        )
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        for index, call in enumerate(tool_calls):
            if not isinstance(call, Mapping):
                continue
            function = call.get("function")
            if not isinstance(function, Mapping) or not isinstance(function.get("name"), str):
                continue
            arguments = function.get("arguments")
            output.append(
                {
                    "id": f"fc_{response_id}_{index}",
                    "type": "function_call",
                    "status": "completed",
                    "call_id": call.get("id") or f"call_{uuid.uuid4().hex}",
                    "name": function["name"],
                    "arguments": (
                        arguments if isinstance(arguments, str) else json.dumps(arguments)
                    ),
                }
            )
    usage = chat.get("usage")
    response: dict[str, object] = {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "model": model,
        "status": "completed",
        "background": False,
        "error": None,
        "output": output,
        "output_text": text if isinstance(text, str) else "",
    }
    if isinstance(usage, Mapping):
        response["usage"] = {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        }
    return response


def _sse(event: str, payload: Mapping[str, object]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n".encode()


def _stream_events(response: Mapping[str, object]) -> bytes:
    events: list[bytes] = []
    events.append(_sse("response.created", {**response, "status": "in_progress", "output": []}))
    events.append(
        _sse("response.in_progress", {"type": "response.in_progress", "response": response})
    )
    output = response.get("output", [])
    output_items = output if isinstance(output, list) else []
    for index, item in enumerate(output_items):
        if not isinstance(item, Mapping):
            continue
        item_id = str(item.get("id", f"item_{index}"))
        events.append(
            _sse(
                "response.output_item.added",
                {"type": "response.output_item.added", "output_index": index, "item": item},
            )
        )
        content = item.get("content")
        if isinstance(content, list) and content and isinstance(content[0], Mapping):
            text = content[0].get("text")
            events.append(
                _sse(
                    "response.content_part.added",
                    {
                        "type": "response.content_part.added",
                        "item_id": item_id,
                        "output_index": index,
                        "content_index": 0,
                        "part": {"type": "output_text", "text": "", "annotations": []},
                    },
                )
            )
            if isinstance(text, str) and text:
                events.append(
                    _sse(
                        "response.output_text.delta",
                        {
                            "type": "response.output_text.delta",
                            "item_id": item_id,
                            "output_index": index,
                            "content_index": 0,
                            "delta": text,
                        },
                    )
                )
            events.append(
                _sse(
                    "response.output_text.done",
                    {
                        "type": "response.output_text.done",
                        "item_id": item_id,
                        "output_index": index,
                        "content_index": 0,
                        "text": text or "",
                    },
                )
            )
            events.append(
                _sse(
                    "response.content_part.done",
                    {
                        "type": "response.content_part.done",
                        "item_id": item_id,
                        "output_index": index,
                        "content_index": 0,
                        "part": content[0],
                    },
                )
            )
        elif item.get("type") == "function_call":
            arguments = item.get("arguments", "")
            events.append(
                _sse(
                    "response.function_call_arguments.delta",
                    {
                        "type": "response.function_call_arguments.delta",
                        "item_id": item_id,
                        "output_index": index,
                        "delta": arguments,
                    },
                )
            )
            events.append(
                _sse(
                    "response.function_call_arguments.done",
                    {
                        "type": "response.function_call_arguments.done",
                        "item_id": item_id,
                        "output_index": index,
                        "arguments": arguments,
                    },
                )
            )
        events.append(
            _sse(
                "response.output_item.done",
                {"type": "response.output_item.done", "output_index": index, "item": item},
            )
        )
    events.append(_sse("response.completed", {"type": "response.completed", "response": response}))
    return b"".join(events)


def _handler_for(config: ZaiResponsesProxyConfig) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *_args: object) -> None:
            # Never let the standard handler log an Authorization header or
            # request body. The session log only needs the failure status.
            del format, _args
            return

        def _send(self, status: int, body: bytes, content_type: str = "application/json") -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path.rstrip("/") not in {"/v1/models", "/models"}:
                self._send(404, b'{"error":{"message":"not found"}}')
                return
            body = json.dumps(
                {
                    "object": "list",
                    "data": [
                        {"id": model_id, "object": "model", "owned_by": "z.ai"}
                        for model_id in sorted(GLM_DIRECT_MODELS)
                    ],
                }
            ).encode()
            self._send(200, body)

        def do_POST(self) -> None:
            if self.path.rstrip("/") not in {"/v1/responses", "/responses"}:
                self._send(404, b'{"error":{"message":"not found"}}')
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                size = 0
            if size <= 0 or size > _MAX_REQUEST_BYTES:
                self._send(413, b'{"error":{"message":"invalid request size"}}')
                return
            try:
                request = json.loads(self.rfile.read(size))
                if not isinstance(request, Mapping):
                    raise ValueError("request must be an object")
                chat_payload = responses_request_to_chat(request)
                upstream_request = Request(
                    config.base_url.rstrip("/") + "/chat/completions",
                    data=json.dumps(chat_payload).encode(),
                    headers={
                        "Authorization": f"Bearer {config.api_key}",
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                    method="POST",
                )
                with urlopen(upstream_request, timeout=_UPSTREAM_TIMEOUT_SECONDS) as upstream:
                    raw = upstream.read()
                    status = upstream.status
                chat_response = json.loads(raw)
                if not isinstance(chat_response, Mapping):
                    raise ValueError("provider response must be an object")
                response = _response_output_from_chat(
                    chat_response,
                    str(request.get("model", "glm-5.3")),
                )
                if request.get("stream") is True:
                    body = _stream_events(response)
                    self._send(200, body, "text/event-stream")
                else:
                    self._send(status, json.dumps(response).encode())
            except HTTPError as exc:
                body = exc.read()
                self._send(exc.code, body or b'{"error":{"message":"direct provider error"}}')
            except (URLError, TimeoutError, OSError) as exc:
                _logger.warning("direct GLM provider request failed: %s", type(exc).__name__)
                self._send(502, b'{"error":{"message":"direct GLM provider request failed"}}')
            except (ValueError, TypeError):
                self._send(502, b'{"error":{"message":"invalid direct GLM provider response"}}')

    return Handler


def start_zai_responses_proxy(config: ZaiResponsesProxyConfig) -> ZaiResponsesProxy:
    """Start one ephemeral loopback adapter."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_for(config))
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name="zai-responses-proxy", daemon=True)
    thread.start()
    return ZaiResponsesProxy(
        server=server,
        thread=thread,
        base_url=f"http://127.0.0.1:{server.server_address[1]}/v1",
    )
