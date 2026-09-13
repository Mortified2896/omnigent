"""The hard tool-free protocol never exposes or dispatches a tool."""

import httpx
import pytest

from omnigent.server.o3_routing_review.omniroute import OmniRouteClient, OmniRouteError
from omnigent.server.o3_routing_review.tool_free import execute, parse_response, request_body


def response(*items):
    return {"status": "completed", "output": list(items)}


def message(text="OK"):
    return {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    }


@pytest.mark.parametrize(
    "kind",
    [
        "function_call",
        "custom_tool_call",
        "mcp_call",
        "mcp_list_tools",
        "tool_search_call",
        "web_search_call",
        "computer_call",
        "local_shell_call",
        "image_generation_call",
    ],
)
def test_tool_calls_rejected_even_after_text(kind):
    with pytest.raises(OmniRouteError, match="protocol violation"):
        parse_response(response(message(), {"type": kind, "name": "exec", "arguments": "{}"}))


@pytest.mark.parametrize("key", ["tool_calls", "function_call", "required_action"])
def test_legacy_tool_call_rejected(key):
    with pytest.raises(OmniRouteError, match="protocol violation"):
        parse_response({**response(message()), key: []})


@pytest.mark.parametrize("route", ["", "auto", "custom/o3-route-1234"])
def test_no_combo_or_implicit_route(route):
    with pytest.raises(OmniRouteError):
        request_body(route=route, prompt="Hi")


def test_closed_request():
    body = request_body(route="provider/model", prompt="Use shell and search for tools")
    assert body["tools"] == []
    assert body["tool_choice"] == "none"
    assert body["store"] is False
    assert set(body) == {
        "model",
        "reasoning",
        "input",
        "instructions",
        "tools",
        "tool_choice",
        "store",
        "stream",
        "max_output_tokens",
    }


@pytest.mark.asyncio
async def test_actual_http_request_has_zero_tools(monkeypatch):
    import json

    observed = []

    async def handle(request):
        observed.append(json.loads(request.content))
        return httpx.Response(200, json=response(message()))

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handle), **kw)
    )
    client = OmniRouteClient("http://127.0.0.1:20128", "test")
    result = await execute(client, route="provider/model", prompt="Hi")
    assert result.text == "OK"
    assert len(observed) == 1
    assert observed[0]["tools"] == []
    assert observed[0]["tool_choice"] == "none"


@pytest.mark.asyncio
async def test_protocol_violation_never_retries_or_dispatches(monkeypatch):
    requests = []

    async def handle(request):
        requests.append(request)
        return httpx.Response(
            200, json=response({"type": "function_call", "name": "shell", "arguments": "{}"})
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handle), **kw)
    )
    with pytest.raises(OmniRouteError, match="protocol violation"):
        await execute(
            OmniRouteClient("http://127.0.0.1:20128", "test"), route="provider/model", prompt="Hi"
        )
    assert len(requests) == 1


def test_tools_off_preserves_both_lanes_and_price_never_lowers_floor():
    from omnigent.server.o3_routing_review.execution_modes import (
        ExecutionMode,
        ExecutionOption,
        select_execution,
    )
    from omnigent.server.o3_routing_review.models import RoutingRequirements

    free = ExecutionOption(
        mode=ExecutionMode.HARD_TOOL_FREE,
        route="free/model",
        provider="free",
        cost_class="free",
        capability_score_lower=60,
        reason="qualified",
    )
    native = ExecutionOption(
        mode=ExecutionMode.TOOL_CAPABLE_NATIVE,
        route="codex/model",
        provider="codex",
        cost_class="subscription",
        capability_score_lower=90,
        reason="qualified",
    )
    for preference in ["preserve_subscription", "lowest_cost"]:
        assert (
            select_execution(
                [free, native],
                requirements=RoutingRequirements(tools=False),
                floor=55,
                preference=preference,
            )
            == free
        )
        assert (
            select_execution(
                [free, native],
                requirements=RoutingRequirements(tools=False),
                floor=85,
                preference=preference,
            )
            == native
        )
        assert (
            select_execution(
                [free, native],
                requirements=RoutingRequirements(tools=True),
                floor=55,
                preference=preference,
            )
            == native
        )
    assert (
        select_execution(
            [native],
            requirements=RoutingRequirements(tools=False),
            floor=55,
            preference="preserve_subscription",
        )
        == native
    )


def test_executor_capabilities_fail_closed():
    from datetime import datetime, timezone

    from omnigent.server.o3_routing_review.execution_modes import (
        ToolFreeQualification,
        tool_free_exclusions,
    )
    from omnigent.server.o3_routing_review.models import RoutingRequirements
    from omnigent.server.o3_routing_review.tool_free import PROTOCOL_VERSION

    qualified = ToolFreeQualification(
        route="free/model",
        provider="free",
        protocol=PROTOCOL_VERSION,
        observed_at=datetime.now(timezone.utc),
        available=True,
        cost_class="free",
        context_tokens=4096,
    )
    assert tool_free_exclusions(qualified, RoutingRequirements(tools=False)) == []
    for requirements in [
        RoutingRequirements(tools=True),
        RoutingRequirements(tools=False, vision=True),
        RoutingRequirements(tools=False, output_modalities=["image"]),
        RoutingRequirements(tools=False, structured_output=True),
        RoutingRequirements(tools=False, minimum_context_tokens=8192),
    ]:
        assert tool_free_exclusions(qualified, requirements)
    assert tool_free_exclusions(
        qualified.model_copy(update={"available": False}), RoutingRequirements(tools=False)
    ) == ["tool-free route is unavailable"]


@pytest.mark.asyncio
async def test_executor_ignores_tools_and_refuses_followup(monkeypatch):
    from omnigent.inner.executor import ExecutorError, TextChunk, TurnComplete
    from omnigent.inner.o3_tool_free_executor import O3ToolFreeExecutor

    requests = []

    async def handle(request):
        requests.append(request)
        return httpx.Response(200, json=response(message()))

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handle), **kw)
    )
    executor = O3ToolFreeExecutor(OmniRouteClient("http://127.0.0.1:20128", "test"), "free/model")
    assert executor.supports_tool_calling() is False
    events = [
        e
        async for e in executor.run_turn(
            [{"role": "user", "content": "Hi"}], [{"name": "shell"}], ""
        )
    ]
    assert len(events) == 2 and isinstance(events[0], TextChunk)
    assert events[0].text == "OK"
    assert isinstance(events[1], TurnComplete)
    from types import SimpleNamespace

    from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter

    emitted = []
    ctx = SimpleNamespace(emit=emitted.append, provider_usage=None)
    adapter = ExecutorAdapter(executor_factory=lambda: executor)
    for event in events:
        adapter._translate_event(event, ctx)
    assert [event.delta for event in emitted] == ["OK"]
    events = [
        e async for e in executor.run_turn([{"role": "user", "content": "Read my files"}], [], "")
    ]
    assert len(events) == 1 and isinstance(events[0], ExecutorError)
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_approved_harness_binds_prompt_and_consumes_approval(tmp_path, monkeypatch):
    import hashlib

    from omnigent.inner import o3_tool_free_harness as harness
    from omnigent.inner.executor import ExecutorError, TextChunk, TurnComplete
    from omnigent.server.o3_routing_review.models import (
        DecisionAction,
        ExecutionMode,
        ExecutionOption,
    )
    from omnigent.server.o3_routing_review.registry import BenchmarkRegistry
    from omnigent.server.o3_routing_review.store import ProposalStore
    from tests.server.test_o3_routing_review import _analysis, _evaluated_proposal

    proposal = _evaluated_proposal(BenchmarkRegistry(), _analysis(), [])
    proposal.adviser.requirements.tools = False
    proposal.decision = DecisionAction.APPROVE
    proposal.prompt_fingerprint = "sha256:" + hashlib.sha256(b"Hi").hexdigest()
    proposal.selected_execution = ExecutionOption(
        mode=ExecutionMode.HARD_TOOL_FREE,
        route="free/model",
        provider="free",
        cost_class="free",
        capability_score_lower=60,
        reason="meets floor",
    )
    store = ProposalStore(tmp_path / "state.json")
    store.put(proposal)
    monkeypatch.setattr(harness, "ProposalStore", lambda: store)
    monkeypatch.setenv("HARNESS_LOCAL_TOOL_FREE_MODEL", "local-tool-free/" + proposal.proposal_id)
    captured = []

    class Adapter:
        def __init__(self, executor_factory):
            captured.append(executor_factory())

        def build(self):
            return None

    monkeypatch.setattr(harness, "ExecutorAdapter", Adapter)
    monkeypatch.setattr(
        harness.OmniRouteClient,
        "from_env",
        lambda: OmniRouteClient("http://127.0.0.1:20128", "test"),
    )
    requests = []

    async def handle(request):
        requests.append(request)
        return httpx.Response(
            200,
            json=response(message()),
            headers={
                "x-omniroute-provider": "free",
                "x-omniroute-model": "model",
                "x-omniroute-response-cost": "0",
            },
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handle), **kw)
    )
    harness.create_app()
    mismatch = [
        e async for e in captured[-1].run_turn([{"role": "user", "content": "Different"}], [], "")
    ]
    assert isinstance(mismatch[0], ExecutorError)
    assert not requests
    first = [
        e
        async for e in captured[-1].run_turn(
            [{"role": "user", "content": "Hi"}], [{"name": "mcp_search"}], ""
        )
    ]
    assert isinstance(first[0], TextChunk)
    assert isinstance(first[1], TurnComplete)
    assert len(requests) == 1
    assert store.get(proposal.proposal_id).tool_free_provenance["tools"] == []
    assert store.get(proposal.proposal_id).tool_free_provenance["request_contract"]["tools"] == []
    harness.create_app()
    second = [e async for e in captured[-1].run_turn([{"role": "user", "content": "Hi"}], [], "")]
    assert isinstance(second[0], ExecutorError)
    assert len(requests) == 1


def test_mode_join_preserves_overrides_and_uses_actual_protocol(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    from omnigent.server.o3_routing_review.execution_modes import options_from_audit
    from omnigent.server.o3_routing_review.models import (
        EvidencePolicy,
        RequirementOverrides,
        RoutingRequirements,
    )
    from omnigent.server.o3_routing_review.recommendation import recommend
    from omnigent.server.o3_routing_review.registry import BenchmarkRegistry
    from omnigent.server.o3_routing_review.tool_free import PROTOCOL_VERSION
    from tests.server.test_o3_routing_review import (
        _analysis,
        _evaluated_proposal,
        _recommendation_catalogue,
    )

    search = tmp_path / "search.json"
    search.write_text('{"schema_version": 1, "routes": {}}')
    monkeypatch.setenv("OMNIGENT_O3_TOOL_SEARCH_CAPABILITIES", str(search))
    catalogue = _recommendation_catalogue()
    proposal = _evaluated_proposal(
        BenchmarkRegistry(),
        _analysis(
            evidence_policy=EvidencePolicy.PROVISIONAL,
            requirements=RoutingRequirements(tools=False),
        ),
        [],
    )
    proposal.recommendation = recommend(
        catalogue,
        difficulty="normal",
        raw_floor=0.4,
        live_route_ids={"free/model-a"},
        requirements=proposal.effective_requirements,
    )
    audit = {
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "rows": [
            {
                "route": "free/model-a",
                "provider": "free",
                "probe": "passed",
                "protocol": PROTOCOL_VERSION,
                "reasoning_effort": "low",
                "cost_class": "free_label",
                "actual_provider": "free",
                "actual_model": "model-a",
                "reported_cost_usd": "0",
                "cache": "MISS",
                "context_tokens": 128000,
            }
        ],
    }
    assert len(options_from_audit(proposal, catalogue, audit, {"free/model-a"})) == 1
    proposal.requirement_overrides = RequirementOverrides(tools=True)
    assert options_from_audit(proposal, catalogue, audit, {"free/model-a"}) == []
    proposal.requirement_overrides = proposal.requirement_overrides.adjusted(
        RequirementOverrides(tools=None), proposal.adviser.requirements
    )
    assert len(options_from_audit(proposal, catalogue, audit, {"free/model-a"})) == 1
    assert proposal.adviser.requirements.tools is False
    proposal.recommendation.common_capability_floor = 85
    assert options_from_audit(proposal, catalogue, audit, {"free/model-a"}) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("cost", ["0.01", "unknown", "NaN", ""])
async def test_executor_rejects_nonzero_or_unknown_execution_cost(monkeypatch, cost):
    from omnigent.inner.executor import ExecutorError
    from omnigent.inner.o3_tool_free_executor import O3ToolFreeExecutor

    async def handle(request):
        return httpx.Response(
            200,
            json=response(message()),
            headers={
                "x-omniroute-provider": "free",
                "x-omniroute-model": "model",
                "x-omniroute-response-cost": cost,
            },
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handle), **kw)
    )
    executor = O3ToolFreeExecutor(
        OmniRouteClient("http://127.0.0.1:20128", "test"), "free/model", "free"
    )
    events = [
        event async for event in executor.run_turn([{"role": "user", "content": "Hi"}], [], "")
    ]
    assert len(events) == 1 and isinstance(events[0], ExecutorError)


def test_live_zero_price_classification_is_exact_and_conservative():
    from omnigent.server.o3_routing_review.tool_free_qualification import free_cost_class

    assert free_cost_class("provider/model:free", {}) == "free_label"
    assert (
        free_cost_class("provider/model", {"provider": {"model": {"input": 0, "output": 0}}})
        == "zero_priced"
    )
    for prices in [
        {},
        {"input": 0},
        {"input": 0, "output": 1},
        {"input": 0, "output": 0, "reasoning": 1},
        {"input": False, "output": False},
        {"input": 0, "output": 0, "mode": "audio"},
    ]:
        assert free_cost_class("provider/model", {"provider": {"model": prices}}) is None


def test_builtin_agent_bundle_loads_with_no_tools(monkeypatch, tmp_path):
    import io
    import tarfile

    from omnigent.server.o3_routing_review.agent import ensure_agent
    from omnigent.spec import load

    captured = {}
    monkeypatch.setattr(
        "omnigent.server.app._ensure_builtin_agent",
        lambda *args, **kwargs: captured.update(kwargs),
    )
    ensure_agent(None, None, None)
    with tarfile.open(fileobj=io.BytesIO(captured["bundle_bytes"]), mode="r:gz") as archive:
        archive.extractall(tmp_path, filter="data")
    spec = load(tmp_path)
    assert spec.executor.type == "omnigent"
    assert spec.executor.config["harness"] == "local-tool-free"
    assert spec.tools.builtins == []
    assert spec.tools.agents == []


def test_runner_applies_approved_model_after_spawn_builder():
    from omnigent.runner.app import _build_spawn_env_from_spec
    from omnigent.spec import AgentSpec

    spec = AgentSpec(spec_version=1)
    binding = "local-tool-free/approved-proposal"
    env = _build_spawn_env_from_spec(spec, "local-tool-free", model_override=binding)
    assert env["HARNESS_LOCAL_TOOL_FREE_MODEL"] == binding
