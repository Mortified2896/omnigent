"""O3-only harness: bind an approved proposal to one closed Responses turn."""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone

from fastapi import FastAPI

from omnigent.inner.executor import ExecutorConfig, ExecutorError
from omnigent.inner.o3_tool_free_executor import O3ToolFreeExecutor
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter
from omnigent.server.o3_routing_review.models import DecisionAction, ExecutionMode
from omnigent.server.o3_routing_review.omniroute import OmniRouteClient
from omnigent.server.o3_routing_review.store import ProposalStore
from omnigent.server.o3_routing_review.tool_free import PROTOCOL_VERSION, ToolFreeResult
from omnigent.spec import AgentSpec

MODEL_PREFIX = "local-tool-free/"


def spawn_env(
    spec: AgentSpec, *, cwd: str | None = None, workdir: str | None = None
) -> dict[str, str]:
    del cwd, workdir
    model = spec.executor.model
    # The runner applies the session model override after this builder.
    if model is None:
        return {}
    if not isinstance(model, str) or not model.startswith(MODEL_PREFIX):
        raise ValueError("tool-free sessions require an approved O3 proposal")
    return {"HARNESS_LOCAL_TOOL_FREE_MODEL": model}


def _approved_executor() -> O3ToolFreeExecutor:
    model = os.environ.get("HARNESS_LOCAL_TOOL_FREE_MODEL", "")
    if not model.startswith(MODEL_PREFIX):
        raise ValueError("tool-free sessions require an approved O3 proposal")
    store = ProposalStore()
    proposal = store.get(model.removeprefix(MODEL_PREFIX))
    if proposal is None or proposal.decision is not DecisionAction.APPROVE:
        raise ValueError("tool-free proposal is not approved")
    selected = proposal.selected_execution
    if selected is None or selected.mode is not ExecutionMode.HARD_TOOL_FREE:
        raise ValueError("proposal is not approved for hard tool-free execution")
    if proposal.effective_requirements.tools or proposal.expires_at < datetime.now(timezone.utc):
        raise ValueError("tool-free approval is expired or requires tools")

    class ApprovedExecutor(O3ToolFreeExecutor):
        def record_result(self, result: ToolFreeResult) -> None:
            current = store.get(proposal.proposal_id)
            if current is None:
                raise ValueError("tool-free proposal disappeared during execution")
            store.put(
                current.model_copy(
                    update={
                        "execution_status": "idle",
                        "actual_provider": result.provider,
                        "actual_model": result.model,
                        "tool_free_provenance": {
                            "protocol": PROTOCOL_VERSION,
                            "endpoint": "/v1/responses",
                            "tools": [],
                            "tool_choice": "none",
                            "store": False,
                            "stream": False,
                            "request_contract": result.request_contract,
                            "response_id": result.response_id,
                            "provider": result.provider,
                            "model": result.model,
                            "reported_cost_usd": result.cost_usd,
                            "usage": result.usage,
                            "observed_at": datetime.now(timezone.utc).isoformat(),
                        },
                    }
                )
            )

        async def run_turn(self, messages, tools, system_prompt, config=None):
            current = store.get(proposal.proposal_id)
            if (
                current is None
                or current.decision is not DecisionAction.APPROVE
                or current.constraint_version != proposal.constraint_version
                or current.selected_execution != selected
                or current.effective_requirements.tools
                or current.expires_at < datetime.now(timezone.utc)
            ):
                yield ExecutorError(
                    message="The approval changed or expired; create a new review."
                )
                return
            users = [m for m in messages if m.get("role") == "user"]
            if len(users) != 1 or not isinstance(users[0].get("content"), str):
                yield ExecutorError(message="Tool-free follow-ups require a new routing review.")
                return
            fingerprint = "sha256:" + hashlib.sha256(users[0]["content"].encode()).hexdigest()
            if fingerprint != proposal.prompt_fingerprint:
                yield ExecutorError(message="The prompt changed; create a new routing review.")
                return
            claims = store.path.parent / "tool-free-claims"
            claims.mkdir(parents=True, exist_ok=True)
            try:
                fd = os.open(
                    claims / proposal.proposal_id, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
                )
            except FileExistsError:
                yield ExecutorError(message="This approval was already used; create a new review.")
                return
            os.close(fd)
            # The proposal alias is a launch binding, never a provider route override.
            config = ExecutorConfig(
                model=selected.route,
                extra={"reasoning_effort": proposal.approved_constraints.reasoning_effort},
            )
            async for event in super().run_turn(messages, tools, system_prompt, config):
                yield event

    return ApprovedExecutor(OmniRouteClient.from_env(), selected.route, selected.provider)


def create_app() -> FastAPI:
    # Session prewarming precedes the runner applying the approved model binding.
    return ExecutorAdapter(executor_factory=_approved_executor).build()
