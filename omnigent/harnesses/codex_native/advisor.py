"""One bounded advisor turn inheriting a native chat's existing history.

An ephemeral fork uses the verified current model and inherited history on the
existing native connection. The child's effort is updated and the advisor request
is appended, without overriding instructions, tools or output schemas.
Tool execution is denied by the existing trusted native hook, rather than
changing tool definitions and invalidating the cached prefix. Cache hits remain
provider evidence, never a promise made by this adapter.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import time
from pathlib import Path
from typing import Any

from omnigent.harnesses.codex_native.bridge import bridge_dir_for_bridge_id, read_bridge_state
from omnigent.host.advisor_call import (
    ADVISOR_INFERENCE_TIMEOUT_SECONDS,
    AdvisorCallError,
    AdvisorCallResult,
    build_advisor_prompt,
)

ADVISOR_THREAD_SOURCE = "omnigent-model-advisor"
_CAPABILITY_FILE = "advisor_context_guard.json"


def write_advisor_context_capability(bridge_dir: Path, command: str) -> None:
    """Bind this new guard implementation to the exact registered hook command."""
    bridge_dir.mkdir(parents=True, exist_ok=True)
    target = bridge_dir / _CAPABILITY_FILE
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps({"version": 1, "command": command}))
    temporary.replace(target)


def _guard_command(bridge_dir: Path) -> str | None:
    try:
        value = json.loads((bridge_dir / _CAPABILITY_FILE).read_text())
    except (OSError, ValueError):
        return None
    return value.get("command") if isinstance(value, dict) and value.get("version") == 1 else None


def _marker(bridge_dir: Path, thread_id: str) -> Path:
    return bridge_dir / "advisor_context_threads" / hashlib.sha256(thread_id.encode()).hexdigest()


def is_advisor_context_hook(bridge_dir: Path, payload: dict[str, Any]) -> bool:
    """Hook session_id is Codex's thread id, not an Omnigent conversation id."""
    thread_id = payload.get("session_id")
    return isinstance(thread_id, str) and _marker(bridge_dir, thread_id).is_file()


def _counter(usage: dict[str, Any], key: str) -> int | None:
    value = usage.get(key)
    return value if type(value) is int and value >= 0 else None


def _object(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise AdvisorCallError("Codex returned an invalid advisor response")
    return value


async def generate_context_advisor_selection(
    *,
    request: dict[str, Any],
    model: str,
    reasoning_effort: str | None,
    session_id: str | None,
    bridge_id: str | None,
    timeout_s: float = ADVISOR_INFERENCE_TIMEOUT_SECONDS,
) -> AdvisorCallResult:
    """Use one authorized native session; never fall back to a fresh context."""
    from omnigent.harnesses.codex_native.app_server import (
        _our_policy_hooks_from_list,
        client_for_transport,
    )

    if not session_id or not bridge_id or not reasoning_effort:
        raise AdvisorCallError("Current-model advice requires a chat and an explicit effort")
    bridge_dir = bridge_dir_for_bridge_id(bridge_id)
    state = read_bridge_state(bridge_dir)
    if state is None or state.session_id != session_id or not state.cwd:
        raise AdvisorCallError(
            "Current-model context is unavailable; resume this native Codex chat"
        )
    guard_command = _guard_command(bridge_dir)
    if not guard_command:
        raise AdvisorCallError(
            "Resume this chat with the updated native harness before using current-model advice"
        )
    from omnigent.model_advisor_attachments import advisor_image_inputs

    prompt = build_advisor_prompt(request)
    client = client_for_transport(state.socket_path, client_name="omnigent-model-advisor")
    child: str | None = None
    turn_id: str | None = None
    marker: Path | None = None
    completed = False
    started = time.monotonic()
    output = ""
    usage: dict[str, Any] = {}

    async def run() -> None:
        nonlocal child, turn_id, marker, completed, output, usage
        await client.connect()
        hooks = _our_policy_hooks_from_list(
            await client.request("hooks/list", {"cwds": [state.cwd]}), state.cwd
        )
        if not any(
            hook.get("eventName") == "preToolUse"
            and hook.get("enabled") is True
            and hook.get("trustStatus") in {"trusted", "managed"}
            and hook.get("command") == guard_command
            for hook in hooks
        ):
            raise AdvisorCallError("Current-model advice requires the trusted tool-blocking hook")
        response = await client.request(
            "thread/fork",
            {
                "threadId": state.thread_id,
                # Fork otherwise resolves the private home's default, which
                # can differ after a live model switch in the parent.
                "model": model,
                "ephemeral": True,
                "excludeTurns": True,
                "threadSource": ADVISOR_THREAD_SOURCE,
            },
        )
        result = _object(response.get("result"))
        thread = _object(result.get("thread"))
        child = thread.get("id")
        if not isinstance(child, str) or not child or child == state.thread_id:
            child = None
            raise AdvisorCallError("Codex did not create an independent advisor context")
        if result.get("model") != model:
            raise AdvisorCallError("The current model changed; request a new recommendation")
        marker = _marker(bridge_dir, child)
        marker.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        marker.write_text("advisor\n")
        # Ephemeral Codex threads do not support goals or automatic goal
        # continuation, so this child owns exactly the explicit advisor turn.
        await client.request(
            "thread/settings/update", {"threadId": child, "effort": reasoning_effort}
        )
        response = await client.request(
            "turn/start",
            {
                "threadId": child,
                "input": [{"type": "text", "text": prompt}, *advisor_image_inputs(request)],
            },
        )
        turn_id = _object(_object(response.get("result")).get("turn")).get("id")
        if not isinstance(turn_id, str) or not turn_id:
            raise AdvisorCallError("Codex did not start the advisor turn")
        async for event in client.iter_events():
            params = _object(event.get("params") or {})
            if params.get("threadId") != child:
                continue
            method = event.get("method")
            # A request for approval/elicitation is never granted by the advisor.
            if "id" in event:
                raise AdvisorCallError("The advisor attempted a tool or approval request")
            if params.get("turnId") not in (None, turn_id):
                continue
            if method == "item/agentMessage/delta":
                output += str(params.get("delta") or "")
            elif method == "item/completed":
                item = _object(params.get("item") or {})
                if item.get("type") == "agentMessage":
                    output = str(item.get("text") or output)
            elif method == "thread/tokenUsage/updated":
                usage = _object(_object(params.get("tokenUsage")).get("last"))
            elif method == "turn/completed":
                turn = _object(params.get("turn") or {})
                if turn.get("id") != turn_id or turn.get("status") != "completed":
                    raise AdvisorCallError("The advisor turn did not complete successfully")
                completed = True
                break
            if len(output.encode()) > 8192:
                raise AdvisorCallError("The advisor exceeded its bounded output budget")
        if not completed or not output.strip():
            raise AdvisorCallError("The advisor produced no completed selection")

    try:
        await asyncio.wait_for(run(), timeout=timeout_s)
        return AdvisorCallResult(
            raw_output=output.strip(),
            latency_ms=int((time.monotonic() - started) * 1000),
            input_tokens=_counter(usage, "inputTokens"),
            output_tokens=_counter(usage, "outputTokens"),
            cached_input_tokens=_counter(usage, "cachedInputTokens"),
            response_id=turn_id,
        )
    except AdvisorCallError:
        raise
    except TimeoutError as exc:
        raise AdvisorCallError(
            f"Current-model advisor exceeded its {timeout_s:g}s budget"
        ) from exc
    except Exception as exc:
        raise AdvisorCallError(
            "Current-model advisor transport failed; no recommendation was applied"
        ) from exc
    finally:
        if child and turn_id and not completed:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    client.request("turn/interrupt", {"threadId": child, "turnId": turn_id}),
                    timeout=5,
                )
        if child:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    client.request("thread/unsubscribe", {"threadId": child}), timeout=5
                )
        await client.close()
        # Keep an abandoned child's guard until its native process ends. Removing
        # it after an uncertain interrupt would allow a still-running tool call.
        if marker is not None and completed:
            marker.unlink(missing_ok=True)
