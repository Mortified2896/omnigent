# OmniRoute integration plan

## Goal

Connect OmniRoute as the model gateway for the Control-Room installation
without writing a single line of code in `omnigent/`. Reproduce the
provenance the fork's `opencode_provenance_proxy.py` recorded (requested
route, executed provider/model, reasoning effort, fallback status, billing
class, route approval), and let OmniRoute remain the source of truth for
routing.

## 1. Upstream transport already available

`omnigent/llms/adapters/openai.py` (`OpenAICompatibleAdapter`) accepts a
per-call `base_url` and `api_key` and talks the OpenAI Chat Completions
wire format. `omnigent/llms/adapters/anthropic.py` accepts `base_url` for
Anthropic-compatible gateways. The control-room admin points these at
OmniRoute via the user-level `~/.omnigent/config.yaml`:

```yaml
providers:
  omniroute:
    base_url: https://omniroute.omnigent-ai.example/v1
    api_key: ${OMNIROUTE_API_KEY}
llm:
  model: omniroute/MiniMax-M3
  profile: omniroute
harness:
  pi:
    model: HARNESS_PI_MODEL
```

That is the **entire** runtime config the Control-Room layer needs. No
upstream change.

## 2. Upstream external router already available

`omnigent/server/smart_routing.py` ships an `ExternalRoutingClient` (the
`routing-mvp` branch is merged into v0.7.0) that calls a
`/routes:select` endpoint with the `omnigent.api.routing.v1` proto.
OmniRoute's v0.7 series exposes that endpoint natively, so the control
room points the routing config at OmniRoute's base URL:

```yaml
routing:
  provider: external
  base_url: https://omniroute.omnigent-ai.example/routing/v1
  router_name: task_v0
  auth:
    type: bearer
    api_key: ${OMNIGENT_ROUTING_API_KEY}
```

The judge + external-routing handshake is fully handled by upstream.

## 3. Provenance: what the user sees in the UI / Langfuse

OmniRoute returns `x-omniroute-*` response headers on every chat
completion and on every `/routes:select` decision. The fork's
`omnigent/opencode_provenance_proxy.py:structured_runtime_provenance`
already reads and validates them. We re-implement that allow-listed
parser in `deploy/control-room/omniroute/provenance.py`:

| Field | Source header | Provenance attribute (Langfuse span) |
| --- | --- | --- |
| `requested_model` | `x-omniroute-requested-model` | `omniroute.requested.model` |
| `actual_provider` | `x-omniroute-selected-provider` (or `x-omniroute-provider`) | `omniroute.executed.provider` |
| `actual_provider_model` | `x-omniroute-selected-model` (or `x-omniroute-model`) | `omniroute.executed.model` |
| `verified` | derived: provider+model both set, distinct from the approved combo | `omniroute.provenance.verified` |
| `fallback_used` | `x-omniroute-fallback-used` (parsed as bool) | `omniroute.fallback.used` |
| `selection_strategy` | `x-omniroute-selection-strategy` | `omniroute.strategy` |
| `billing_class` | `x-omniroute-billing-class` | `omniroute.billing.class` |
| `request_id` | `x-omniroute-request-id` | `omniroute.request.id` |
| `decision_id` | `x-omniroute-decision-id` | `omniroute.decision.id` |
| `reasoning_effort` | the value of the `reasoning_effort` key in the request body | `omniroute.reasoning.effort` |
| `route_approval` | derived from `route_approval_enabled` session flag (set by the orchestrator card) | `omniroute.route_approval.state` |

The `verified` flag is set only when the upstream response *actually
names* a provider and a model distinct from the approved combo. A
self-referential answer (e.g. the approved combo is `auto` and OmniRoute
echoes `auto`) leaves `verified=False`, so the Langfuse trace
visibly distinguishes "OmniRoute decided" from "OmniRoute rubber-stamped
the request".

## 4. Fail-closed silence

Upstream `OpenAICompatibleAdapter` raises `OmnigentError(code=INVALID_INPUT)`
when neither the per-call `base_url` nor the adapter default resolves
(`omnigent/llms/adapters/openai.py:300-310`). **No silent fallback is
possible in the control-room configuration** because:

1. The user-level `providers:` block requires an explicit `base_url` for
   `omniroute`; an absent env var surfaces as a clear startup error.
2. The runtime never falls back to a "default" provider — every LLM
   call carries the `omniroute/...` prefix and the adapter's
   `_resolve_base_url` raises if the URL is empty.
3. The external routing client returns `None` and surfaces a
   structured `last_error` when OmniRoute returns no verdict, so the
   runner proceeds on the agent's default model **with the error
   surfaced** (the default-model path is still user-chosen, never
   inferred from a hidden fallback).

## 5. Acceptance

Phase 8 acceptance test #6 ("delegation to OpenCode does not silently
fall back to another harness") is satisfied by upstream's
`OPENCODE_NATIVE_CODING_AGENT` declaration + the `prune_invalid_sub_agents`
loader behavior. Phase 8 acceptance test #9 ("OmniRoute provenance
records both requested and executed routing details") is satisfied by
the `deploy/control-room/omniroute/provenance.py` adapter, which is
exercised by `deploy/control-room/tests/test_omniroute_provenance.py`
(declarative unit tests against canned `httpx.Headers`).

## 6. Out of scope

- Migrating the fork's `omnigent/server/routing_agent.py` (1 550 LOC
  routing-agent state machine) to upstream. It is replaced by upstream's
  `ExternalRoutingClient` + the `LLMRoutingClient` judge; the fork's
  state machine was the *competing* implementation, not an extension of
  upstream.
- Building an `omniroute` provider prefix inside
  `omnigent/llms/routing.py:PROVIDER_CONFIGS`. The user-level config
  block already does this without code.
- Exposing `omniroute` model ids inside `omnigent/model_catalog.py`.
  The catalog is populated by the runner's live `/api/.../models`
  endpoint, which forwards the user's `providers:` block to OmniRoute
  and renders its catalog.
