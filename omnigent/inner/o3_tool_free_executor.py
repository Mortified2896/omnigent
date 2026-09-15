"""Single approved O3 turn through a closed, tool-free Responses protocol."""

from __future__ import annotations

from collections.abc import AsyncIterator

from omnigent.inner.executor import (
    Executor,
    ExecutorConfig,
    ExecutorError,
    ExecutorEvent,
    Message,
    TextChunk,
    ToolSpec,
    TurnComplete,
)
from omnigent.server.o3_routing_review.omniroute import OmniRouteClient, OmniRouteError
from omnigent.server.o3_routing_review.tool_free import ToolFreeResult, execute


class O3ToolFreeExecutor(Executor):
    """Never yields tool events or invokes the adapter's tool callbacks."""

    def __init__(self, client: OmniRouteClient, route: str, provider: str | None = None) -> None:
        self._client = client
        self._route = route
        self._provider = provider
        self._attempted = False

    def record_result(self, result: ToolFreeResult) -> None:
        """Optional audit sink; the executor never dispatches tools."""

    def supports_tool_calling(self) -> bool:
        return False

    async def run_turn(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        system_prompt: str,
        config: ExecutorConfig | None = None,
    ) -> AsyncIterator[ExecutorEvent]:
        del tools
        users = [message for message in messages if message.get("role") == "user"]
        if (
            self._attempted
            or len(users) != 1
            or any(
                message.get("role") not in {"user", "system", "developer"} for message in messages
            )
        ):
            yield ExecutorError(
                message="Tool-free follow-ups require a new O3 routing review. "
                "Start a new task and enable Tools required if external access is needed."
            )
            return
        if config and config.model and config.model != self._route:
            yield ExecutorError(
                message="Changing the approved tool-free route requires a new review."
            )
            return
        prompt = users[0].get("content")
        if not isinstance(prompt, str):
            yield ExecutorError(
                message="This tool-free executor does not support this input modality."
            )
            return
        self._attempted = True
        try:
            result = await execute(
                self._client,
                route=self._route,
                prompt=prompt,
                context=system_prompt,
                reasoning_effort=str(config.extra.get("reasoning_effort", "low"))
                if config
                else "low",
            )
        except OmniRouteError as exc:
            yield ExecutorError(message=str(exc))
            return
        try:
            zero_cost = float(result.cost_usd or "nan") == 0
        except ValueError:
            zero_cost = False
        if self._provider is not None and (
            result.provider != self._provider
            or result.model not in {self._route, self._route.split("/", 1)[-1]}
            or not zero_cost
        ):
            yield ExecutorError(
                message="Tool-free execution provenance did not match the approval."
            )
            return
        self.record_result(result)
        yield TextChunk(text=result.text)
        yield TurnComplete(response=None, usage=result.usage)
