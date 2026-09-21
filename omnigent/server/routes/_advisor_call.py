"""One round-trip helper for ``host.advisor_call``.

The model advisor's single bounded selection call runs on the machine that
holds the user's authorized lane credentials — the same reason pre-launch
model options resolve host-side. Only this request shape differs, so the
request-id / future / frame / timeout / cleanup pattern mirrors
``_host_model_options`` here once.
"""

from __future__ import annotations

import asyncio
import secrets
from typing import Any

from omnigent.host.frames import HostAdvisorCallFrame, encode_host_frame
from omnigent.server.host_registry import HostConnection, HostRegistry


async def request_host_advisor_call(
    *,
    host_registry: HostRegistry,
    host_conn: HostConnection,
    request: dict[str, Any],
    model: str,
    access_lane: str | None,
    reasoning_effort: str | None,
    timeout_s: float,
) -> dict[str, Any]:
    """
    Send a ``host.advisor_call`` frame and await the host's result.

    :param host_registry: Registry used to enqueue the outbound frame.
    :param host_conn: Live host connection to run the call on.
    :param request: The allowlisted advisor request object (instructions,
        task, candidates, output schema) built by the advisor core.
    :param model: The advisor model id, e.g. ``"gpt-5.3-codex"``.
    :param access_lane: Authorized lane for the advisor transport.
    :param reasoning_effort: Advisor effort, or ``None``.
    :param timeout_s: Seconds to wait for the result frame.
    :returns: The result payload, e.g.
        ``{"status": "ok", "raw_output": "..."}``.
    :raises ConnectionError: The host connection dropped before the frame
        could be enqueued.
    :raises asyncio.TimeoutError: The host did not answer within
        *timeout_s*.
    """
    request_id = secrets.token_hex(8)
    future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
    host_conn.pending_advisor_calls[request_id] = future
    frame = encode_host_frame(
        HostAdvisorCallFrame(
            request_id=request_id,
            request=request,
            model=model,
            access_lane=access_lane,
            reasoning_effort=reasoning_effort,
        )
    )
    try:
        host_registry.send_text(host_conn, frame)
        return await asyncio.wait_for(future, timeout=timeout_s)
    finally:
        host_conn.pending_advisor_calls.pop(request_id, None)
