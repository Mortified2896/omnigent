import type { AvailableAgent } from "@/hooks/useAvailableAgents";

export const WRAPPER_LABEL_KEY = "omnigent.wrapper";
export const UI_MODE_LABEL_KEY = "omnigent.ui";
export const UI_MODE_TERMINAL_VALUE = "terminal";

export type NativeCodingAgentIconKind =
  | "claude"
  | "codex"
  | "opencode"
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
  | "modelOptions";

export interface NativeCodingAgentSpec {
  key: NativeCodingAgentIconKind;
  agentName: string;
  harness: string;
  wrapperLabel: string;
  displayName: string;
  iconKind: NativeCodingAgentIconKind;
  sortRank: number;
  capabilities?: readonly NativeCodingAgentCapability[];
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
    displayName: "OpenCode",
    iconKind: "opencode",
    sortRank: 25,
    // No `permissionMode` capability: OpenCode has no claude-style
    // permission-mode surface to mirror. Its native modes are the `build`
    // (allow-by-default) and `plan` primary agents, switched at runtime via Tab
    // inside the TUI — and `opencode attach` (how the runner launches it) has
    // no `--agent` flag to preset one anyway. The runner already forces
    // `permission: "ask"` so tools route through the Omnigent policy engine, so
    // a launch-time picker would mirror nothing. (Previously declared Codex's
    // `approvalMode`, whose `--sandbox`/`--ask-for-approval` presets aren't
    // understood by `opencode attach` and crashed the TUI on any non-default
    // pick.)
    //
    // `modelOptions` exposes OpenCode Free's pre-session model submenu via
    // ``GET /v1/harness-model-options?harness=opencode-native``. The catalog
    // is owned by ``~/.cache/homelab/opencode-free-models.json`` (sync script
    // in the HomeLab repo). The MiniMax Token Plan lane stays separate —
    // it lives under harness ``opencode-native-minimax-token-plan`` and gets
    // its own picker row, NOT this one. No API-metered MiniMax id can ever
    // reach this lane (the server rejects non-OpenCode-Free ids in the
    // catalog reader).
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
