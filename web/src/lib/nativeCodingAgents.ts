import type { AvailableAgent } from "@/hooks/useAvailableAgents";

export const WRAPPER_LABEL_KEY = "omnigent.wrapper";
export const UI_MODE_LABEL_KEY = "omnigent.ui";
export const UI_MODE_TERMINAL_VALUE = "terminal";

export type NativeCodingAgentIconKind =
  | "claude"
  | "codex"
  | "opencode"
  | "opencode-minimax-token-plan"
  | "opencode-codex-subscription"
  | "pi"
  | "cursor"
  | "kiro"
  | "goose"
  | "qwen"
  | "antigravity"
  | "kimi"
  | "hermes";
export type NativeCodingAgentCapability =
  | "permissionMode"
  | "approvalMode"
  | "cursorMode"
  // The harness exposes a list of models via the generic
  // ``/v1/harness-model-options?harness=<canonical-harness>`` endpoint.
  // The new-session picker surfaces the list in the per-entry submenu so
  // the user can pick the launch model BEFORE the first prompt. The pick
  // rides along to the create as ``model_override`` (same field the
  // claude-native picker uses). The picked model is also the default
  // for the in-session model picker (ChatPage already plumbs this
  // through for harnesses whose server-side default is a free-catalog
  // model, e.g. opencode-native).
  | "modelOptions"
  // OpenCode-specific reasoning effort support. When present, the
  // pre-session submenu shows a reasoning-effort picker whose options
  // are filtered by the picked model's ``variants`` array (models with
  // no variants hide the picker). The selected effort is stored as
  // ``reasoning_effort`` on the create body and forwarded to OpenCode
  // as the ``--variant`` per-prompt flag.
  | "reasoningEffort";

// Access-path grouping for the harness selector. The picker renders
// harnesses under these group headers so the user can see, at a glance,
// whether a lane is free / no paid API (no API key, no fallback) or
// subscription-backed (Token Plan / Codex subscription). Grouping is
// driven by the harness's *access path* — NEVER by the model family
// name. A model family like MiniMax M3 may exist in BOTH the OpenCode
// Free catalog AND the MiniMax Token Plan catalog; these are NEVER
// conflated, because they live under different harnesses with separate
// canonical ids. See ``docs/omnigent-tailscale-eval.md``.
export type NativeCodingAgentAccessPathGroup =
  | "free" // Free / no paid API — OpenCode Free today; no API key, no
  //         fallback to a paid provider.
  | "subscription" // Subscription — Token Plan / subscription-backed.
  | "other"; // Anything else (Claude Code, Codex, Pi, Cursor, Kiro, Goose, etc.).

export interface NativeCodingAgentSpec {
  key: NativeCodingAgentIconKind;
  agentName: string;
  harness: string;
  wrapperLabel: string;
  displayName: string;
  iconKind: NativeCodingAgentIconKind;
  sortRank: number;
  capabilities?: readonly NativeCodingAgentCapability[];
  // Which access-path group this agent belongs to in the picker.
  // Drives the "Free / no paid API" vs "Subscriptions" header split
  // in the harness selector. Defaults to "other" when unset so
  // existing entries render under the legacy "Harnesses" header
  // (Claude, Codex, Pi, etc. stay together; the access-path split
  // applies ONLY to the OpenCode-backed lanes where the grouping
  // carries real safety meaning).
  accessPathGroup?: NativeCodingAgentAccessPathGroup;
}

export const NATIVE_CODING_AGENTS = [
  {
    key: "claude",
    agentName: "claude-native-ui",
    harness: "claude-native",
    wrapperLabel: "claude-code-native-ui",
    displayName: "Claude Code",
    iconKind: "claude",
    sortRank: 10,
    capabilities: ["permissionMode"],
  },
  {
    key: "codex",
    agentName: "codex-native-ui",
    harness: "codex-native",
    wrapperLabel: "codex-native-ui",
    displayName: "Codex",
    iconKind: "codex",
    sortRank: 20,
    capabilities: ["approvalMode"],
  },
  {
    key: "opencode",
    agentName: "opencode-native-ui",
    harness: "opencode-native",
    wrapperLabel: "opencode-native-ui",
    displayName: "OpenCode Free",
    iconKind: "opencode",
    sortRank: 25,
    accessPathGroup: "free",
    // Permission mode lets the user control OpenCode's auto-approval
    // behavior. Unlike Claude Code (which injects --permission-mode into the
    // TUI), OpenCode's permission mode is translated server-side into the
    // synthesized opencode.json ``permission`` field and, for the "auto" /
    // "bypass" modes, the runner writes ``permission: "allow"`` instead of the
    // default ``permission: "ask"``. The runner also stores the mode on the
    // conversation so the policy engine can factor the user's intent into
    // auto-decisions.
    //
    // Reasoning effort is exposed as ``--variant`` per-prompt when the picked
    // model's catalog entry advertises non-empty ``variants``. Models with no
    // variants (e.g. ``opencode/big-pickle``, all ``omniroute/*`` models) hide
    // the effort picker. The selected effort is stored as ``reasoning_effort``
    // on the conversation and forwarded to OpenCode's HTTP API as the
    // ``variant`` field in each prompt payload.
    //
    // ``modelOptions`` exposes OpenCode Free's pre-session model submenu via
    // ``GET /v1/harness-model-options?harness=opencode-native``. The catalog
    // is owned by ``~/.cache/homelab/opencode-free-models.json`` (sync script
    // in the HomeLab repo). The MiniMax Token Plan lane stays separate —
    // it lives under harness ``opencode-native-minimax-token-plan`` and gets
    // its own picker row, NOT this one. No API-metered MiniMax id can ever
    // reach this lane (the server rejects non-OpenCode-Free ids in the
    // catalog reader).
    capabilities: ["modelOptions", "permissionMode", "reasoningEffort"],
  },
  {
    // OpenCode-backed MiniMax Token Plan lane. Distinct harness id
    // (``opencode-native-minimax-token-plan``) and distinct wrapper label
    // (``opencode-native-minimax-token-plan-ui``) from ``opencode-native``
    // so:
    //   * a stored model pick from this lane never leaks into the OpenCode
    //     Free lane and vice versa;
    //   * a same-family model name (e.g. ``MiniMax-M3``) appearing in
    //     BOTH the OpenCode Free catalog AND the MiniMax Token Plan
    //     catalog renders as TWO distinct picker rows under TWO distinct
    //     group headers (``Free / no paid API`` vs ``Subscriptions``);
    //   * the create body ships the correct ``omnigent.wrapper`` label
    //     so the runner routes to the right OpenCode provider prefix.
    //
    // Subscription-only. The executor rejects any ``model_override`` whose
    // provider prefix is not under ``minimax-coding-plan`` or
    // ``minimax-cn-coding-plan`` — the two Token Plan prefixes. The
    // API-metered ``minimax/`` and ``minimax-cn/`` ids can NEVER reach
    // this lane; the catalog resolver also strips them at three layers
    // (sync / verify / resolver). No ``MINIMAX_API_KEY`` is consulted
    // as a fallback at runtime; the catalog only carries a boolean
    // ``credentials_present``.
    key: "opencode-minimax-token-plan",
    agentName: "opencode-native-minimax-token-plan-ui",
    harness: "opencode-native-minimax-token-plan",
    wrapperLabel: "opencode-native-minimax-token-plan-ui",
    displayName: "MiniMax Token Plan",
    iconKind: "opencode-minimax-token-plan",
    sortRank: 26,
    accessPathGroup: "subscription",
    capabilities: ["modelOptions"],
  },
  {
    // OpenCode-backed Codex Subscription lane. Distinct harness id
    // (``opencode-native-codex-subscription``) and distinct wrapper label
    // (``opencode-native-codex-subscription-ui``) from BOTH
    // ``codex-native`` (the OpenAI API-billed Codex path) and
    // ``opencode-native`` (the free lane) so a stored model pick never
    // leaks across lanes and a same-family model name appearing in
    // multiple lanes renders as separate rows under separate group
    // headers.
    //
    // Subscription-only, today fail-closed: the local catalog resolver
    // returns an empty list with a setup / status message because no
    // public OpenCode Codex-subscription provider prefix is verified yet.
    // The picker renders the empty state instead of inventing models.
    // When the allowlist grows, the executor and the picker gate on the
    // same membership list so they stay in lockstep.
    //
    // No ``OPENAI_API_KEY`` is consulted anywhere in this lane — no
    // OpenAI SDK, no OpenAI billing path, no silent fallback. The
    // subscription-backed Codex path is the ONLY way this lane can
    // ever run a model.
    key: "opencode-codex-subscription",
    agentName: "opencode-native-codex-subscription-ui",
    harness: "opencode-native-codex-subscription",
    wrapperLabel: "opencode-native-codex-subscription-ui",
    displayName: "Codex Subscription",
    iconKind: "opencode-codex-subscription",
    sortRank: 27,
    accessPathGroup: "subscription",
    capabilities: ["modelOptions"],
  },
  {
    key: "cursor",
    agentName: "cursor-native-ui",
    harness: "cursor-native",
    wrapperLabel: "cursor-native-ui",
    displayName: "Cursor",
    iconKind: "cursor",
    sortRank: 30,
    capabilities: ["cursorMode"],
  },
  {
    key: "pi",
    agentName: "pi-native-ui",
    harness: "pi-native",
    wrapperLabel: "pi-native-ui",
    displayName: "Pi",
    iconKind: "pi",
    sortRank: 40,
  },
  {
    key: "kiro",
    agentName: "kiro-native-ui",
    harness: "kiro-native",
    wrapperLabel: "kiro-native-ui",
    displayName: "Kiro",
    iconKind: "kiro",
    sortRank: 50,
  },
  {
    // Antigravity's native CLI (Gemini-family). Mirrors the server's
    // canonical `antigravity-native` harness and the `antigravity-native-ui`
    // wrapper the runner keys off to boot the terminal. Added ALONGSIDE the
    // upstream in-process `antigravity` SDK harness (see BRAIN_HARNESS_LABELS
    // in agentLabels.ts) — they are distinct rows.
    key: "antigravity",
    agentName: "antigravity-native-ui",
    harness: "antigravity-native",
    wrapperLabel: "antigravity-native-ui",
    displayName: "Antigravity",
    iconKind: "antigravity",
    sortRank: 45,
  },
  {
    key: "goose",
    agentName: "goose-native-ui",
    harness: "goose-native",
    wrapperLabel: "goose-native-ui",
    displayName: "Goose",
    iconKind: "goose",
    sortRank: 60,
  },
  {
    // qwen has no brand glyph yet, so it falls back to the generic bot icon
    // (see AgentCard.iconForAgent / SubagentsPanel) — the `iconKind: "qwen"`
    // intentionally matches no icon branch. Auth/approval surface in the
    // embedded terminal, so no capability flags are declared here.
    key: "qwen",
    agentName: "qwen-native-ui",
    harness: "qwen-native",
    wrapperLabel: "qwen-native-ui",
    displayName: "Qwen Code",
    iconKind: "qwen",
    sortRank: 60,
  },
  {
    key: "kimi",
    agentName: "kimi-native-ui",
    harness: "kimi-native",
    wrapperLabel: "kimi-native-ui",
    displayName: "Kimi",
    iconKind: "kimi",
    sortRank: 70,
  },
  {
    // hermes has no brand glyph yet, so it falls back to the generic bot icon
    // (see AgentCard.iconForAgent / SubagentsPanel) — the `iconKind: "hermes"`
    // intentionally matches no icon branch. Auth/approval surface in the
    // embedded terminal, so no capability flags are declared here.
    key: "hermes",
    agentName: "hermes-native-ui",
    harness: "hermes-native",
    wrapperLabel: "hermes-native-ui",
    displayName: "Hermes",
    iconKind: "hermes",
    sortRank: 80,
  },
] as const satisfies readonly NativeCodingAgentSpec[];

const BY_AGENT_NAME: Map<string, NativeCodingAgentSpec> = new Map(
  NATIVE_CODING_AGENTS.map((agent) => [agent.agentName, agent]),
);
const BY_HARNESS: Map<string, NativeCodingAgentSpec> = new Map(
  NATIVE_CODING_AGENTS.map((agent) => [agent.harness, agent]),
);
const BY_WRAPPER: Map<string, NativeCodingAgentSpec> = new Map(
  NATIVE_CODING_AGENTS.map((agent) => [agent.wrapperLabel, agent]),
);

// Server-sent harness spellings that fold to a canonical native `harness`.
// Mirrors omnigent.harness_aliases.HARNESS_ALIASES on the server, which
// maps shorthand and reversed spellings to canonical ids. Entries here
// let nativeCodingAgentForHarness resolve the canonical spec for any
// alias the server may send in configured_harnesses, and lets
// dedupeNativeAgents collapse duplicates from the agent list.
const HARNESS_ALIASES: Record<string, string> = {
  "opencode": "opencode-native",
  "native-opencode": "opencode-native",
  "opencode-minimax-token-plan": "opencode-native-minimax-token-plan",
  "opencode-codex-subscription": "opencode-native-codex-subscription",
  "native-pi": "pi-native",
  "native-cursor": "cursor-native",
  "native-kiro": "kiro-native",
  "native-antigravity": "antigravity-native",
  "native-goose": "goose-native",
  "native-qwen": "qwen-native",
  "native-kimi": "kimi-native",
  "native-hermes": "hermes-native",
};

export function nativeCodingAgentForAgentName(
  name: string | null | undefined,
): NativeCodingAgentSpec | undefined {
  return name == null ? undefined : BY_AGENT_NAME.get(name);
}

export function nativeCodingAgentForHarness(
  harness: string | null | undefined,
): NativeCodingAgentSpec | undefined {
  if (harness == null) return undefined;
  return BY_HARNESS.get(HARNESS_ALIASES[harness] ?? harness);
}

export function nativeCodingAgentForWrapper(
  wrapper: string | null | undefined,
): NativeCodingAgentSpec | undefined {
  return wrapper == null ? undefined : BY_WRAPPER.get(wrapper);
}

export function nativeCodingAgentForAvailableAgent(
  agent: Pick<AvailableAgent, "name" | "harness"> | null | undefined,
): NativeCodingAgentSpec | undefined {
  if (agent == null) return undefined;
  return nativeCodingAgentForHarness(agent.harness) ?? nativeCodingAgentForAgentName(agent.name);
}

export function isNativeCodingAgent(
  agent: Pick<AvailableAgent, "name" | "harness"> | null | undefined,
): boolean {
  return nativeCodingAgentForAvailableAgent(agent) !== undefined;
}

export function isNativeWrapper(wrapper: string | null | undefined): boolean {
  return nativeCodingAgentForWrapper(wrapper) !== undefined;
}

export function nativeWrapperLabelsForAgent(
  agent: Pick<AvailableAgent, "name" | "harness"> | null | undefined,
): Record<string, string> | undefined {
  const nativeAgent = nativeCodingAgentForAvailableAgent(agent);
  if (nativeAgent === undefined) return undefined;
  return {
    [UI_MODE_LABEL_KEY]: UI_MODE_TERMINAL_VALUE,
    [WRAPPER_LABEL_KEY]: nativeAgent.wrapperLabel,
  };
}

export function nativeDisplayNameForAgent(agent: Pick<AvailableAgent, "name" | "harness">): string {
  return (
    nativeCodingAgentForAvailableAgent(agent)?.displayName ??
    nativeCodingAgentForAgentName(agent.name)?.displayName ??
    agent.name
  );
}

export function nativeAgentSortRank(agent: Pick<AvailableAgent, "name" | "harness">): number {
  return nativeCodingAgentForAvailableAgent(agent)?.sortRank ?? Number.POSITIVE_INFINITY;
}

export function nativeAgentHasCapability(
  agent: Pick<AvailableAgent, "name" | "harness"> | null | undefined,
  capability: NativeCodingAgentCapability,
): boolean {
  return nativeCodingAgentForAvailableAgent(agent)?.capabilities?.includes(capability) ?? false;
}

/**
 * Return the access-path group this native agent belongs to.
 *
 * Drives the "Free / no paid API" vs "Subscriptions" header split in
 * the harness selector. The grouping is by **access path**, NEVER by
 * model family name — a model family like MiniMax M3 may exist in
 * both the OpenCode Free catalog and the MiniMax Token Plan catalog,
 * and these render under different groups because their harnesses
 * route through different access paths.
 *
 * Returns ``"other"`` for native agents without a declared group
 * (Claude Code, Codex, Pi, Cursor, Kiro, Goose, …) so they keep
 * rendering under the legacy "Harnesses" header. ``null`` for
 * non-native / unknown harnesses.
 */
export function nativeAgentAccessPathGroup(
  agent: Pick<AvailableAgent, "name" | "harness"> | null | undefined,
): NativeCodingAgentAccessPathGroup | null {
  if (agent == null) return null;
  const native = nativeCodingAgentForAvailableAgent(agent);
  if (native === undefined) return null;
  return native.accessPathGroup ?? "other";
}
