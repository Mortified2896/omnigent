"""Closed Responses protocol for executions with no callable tools."""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from .omniroute import OmniRouteClient, OmniRouteError

PROTOCOL_VERSION = "local-tool-free-responses-v1"


@dataclass(frozen=True)
class ToolFreeResult:
    text: str
    usage: dict[str, object]
    response_id: str | None
    provider: str | None = None
    model: str | None = None
    cost_usd: str | None = None
    cache: str | None = None
    request_contract: dict[str, object] = field(default_factory=dict)


def request_body(
    *,
    route: str,
    prompt: str,
    context: str = "",
    max_output_tokens: int = 1024,
    reasoning_effort: str = "low",
) -> dict[str, object]:
    """Construct a closed request; callers cannot add tools or server state."""
    if not route or "/" not in route or route.startswith("custom/"):
        raise OmniRouteError("tool-free execution requires an exact provider/model route")
    if not prompt.strip():
        raise OmniRouteError("tool-free execution requires a nonempty prompt")
    if reasoning_effort not in {"low", "medium", "high", "xhigh"}:
        raise OmniRouteError("invalid tool-free reasoning effort")
    if max_output_tokens < 1:
        raise OmniRouteError("invalid tool-free output limit")
    return {
        "model": route,
        "reasoning": {"effort": reasoning_effort},
        "input": [{"role": "user", "content": prompt}],
        "instructions": context,
        "tools": [],
        "tool_choice": "none",
        "store": False,
        "stream": False,
        "max_output_tokens": max_output_tokens,
    }


def parse_response(body: dict[str, object]) -> ToolFreeResult:
    """Accept only text messages and reasoning; never dispatch provider output."""
    if body.get("error") or body.get("status") not in {None, "completed"}:
        raise OmniRouteError("tool-free provider did not complete the response")
    if any(key in body for key in ("tool_calls", "function_call", "required_action")):
        raise OmniRouteError("tool-free protocol violation: provider requested a tool")
    output = body.get("output")
    if not isinstance(output, list) or not output:
        raise OmniRouteError("tool-free provider omitted Responses output")
    texts: list[str] = []
    for item in output:
        if not isinstance(item, dict):
            raise OmniRouteError("invalid tool-free output item")
        kind = item.get("type")
        if kind == "reasoning":
            continue
        if kind != "message" or item.get("role") != "assistant":
            raise OmniRouteError("tool-free protocol violation: unsupported output item")
        if any(key in item for key in ("tool_calls", "function_call")):
            raise OmniRouteError("tool-free protocol violation: provider requested a tool")
        content = item.get("content")
        if not isinstance(content, list):
            raise OmniRouteError("invalid tool-free message content")
        for part in content:
            if not isinstance(part, dict) or part.get("type") not in {"output_text", "refusal"}:
                raise OmniRouteError("tool-free protocol violation: unsupported content")
            value = part.get("text" if part["type"] == "output_text" else "refusal")
            if not isinstance(value, str):
                raise OmniRouteError("invalid tool-free text")
            texts.append(value)
    if not texts or not "".join(texts).strip():
        raise OmniRouteError("tool-free provider returned no text")
    usage = body.get("usage")
    response_id = body.get("id")
    return ToolFreeResult(
        text="\n".join(texts),
        usage=usage if isinstance(usage, dict) else {},
        response_id=response_id if isinstance(response_id, str) else None,
    )


async def execute(
    client: OmniRouteClient,
    *,
    route: str,
    prompt: str,
    context: str = "",
    max_output_tokens: int = 1024,
    reasoning_effort: str = "low",
) -> ToolFreeResult:
    """One HTTP response with no continuation, dispatch, or fallback loop."""
    body = request_body(
        route=route,
        prompt=prompt,
        context=context,
        max_output_tokens=max_output_tokens,
        reasoning_effort=reasoning_effort,
    )
    response = await client.create_response(body)
    result = parse_response(response.body)
    headers = response.headers
    return replace(
        result,
        provider=headers.get("x-omniroute-provider"),
        model=headers.get("x-omniroute-model"),
        cost_usd=headers.get("x-omniroute-response-cost"),
        cache=headers.get("x-omniroute-cache"),
        request_contract={
            key: body[key]
            for key in (
                "model",
                "tools",
                "tool_choice",
                "store",
                "stream",
                "reasoning",
                "max_output_tokens",
            )
        },
    )
