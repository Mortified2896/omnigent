# OmniRoute Free Chat in Omnigent

This integration creates a deliberately narrow chat lane:

- Omnigent harness: `openai-agents` (OpenAI Agents SDK)
- Agent: **OmniRoute Free Chat**
- OmniRoute requested model: `custom/free-chat-smart`
- Tool surface: empty
- Conversation state: persistent through the Agents SDK session layer
- Actual route display: Omnigent receives the concrete provider/model selected
  by OmniRoute for each completed model request and publishes it through the
  existing session-model event path.

The OmniRoute Combo itself is versioned in the sibling
`omniroute-customizations` repository. Its free-only allowlist is the economic
safety boundary. Omnigent must not replace that Combo with a broad `auto/*`
route.

## Provider configuration

Configure a named OpenAI-compatible provider on the RTX host. Keep the secret
outside git:

```yaml
# ~/.omnigent/config.yaml
providers:
  omniroute-free-chat:
    kind: gateway
    openai:
      base_url: http://127.0.0.1:20128/v1
      api_key_ref: env:OMNIROUTE_API_KEY
      wire_api: responses
      models:
        default: custom/free-chat-smart
```

The Omnigent service environment must expose `OMNIROUTE_API_KEY`; do not copy
the token into this repository. The agent explicitly selects this provider with
`executor.auth: {type: provider, name: omniroute-free-chat}`, so it does not
become the global OpenAI default and cannot accidentally reroute Codex or other
agents.

## Tool-free contract

The agent at
`examples/free-chat/agents/omniroute-free-chat/config.yaml` declares no
`tools` or `os_env`, and sets `async: false` and `timers: false`. This
keeps the dedicated chat lane conversation-only without weakening the global
Agents SDK harness for other agents.

## Route visibility

OmniRoute already returns
`x-omniroute-selected-provider` and `x-omniroute-selected-model`. The Agents
SDK executor correlates those headers using the SDK-propagated `x-request-id`
and reports the concrete route as the turn's model. Existing Omnigent session
model events then update the normal model display.

Examples of values the UI may show:

- `groq/openai/gpt-oss-120b`
- `nvidia/nvidia/nemotron-3-super-120b-a12b`
- `requesty/nvidia/nemotron-3-super-120b-a12b`
- `gemini/gemini-3.1-flash-lite`
- `openrouter/nex-agi/nex-n2.5-mini:free`
- `mistral/codestral-latest`

A later turn may legitimately show a different route when the free Combo's
quality/health/quota scoring changes. The requested Combo remains
`custom/free-chat-smart`; only provenance/display changes.

## Registration

Register the operator-authored template using the same production agent
registration mechanism as the existing server deployment (`omnigent server
--agent ...`). Do not upload it as an untrusted tenant bundle because its
named provider is host configuration.

Deployment and service changes belong in the HomeLab/peer-deployer layer, not
in this portable application branch.
