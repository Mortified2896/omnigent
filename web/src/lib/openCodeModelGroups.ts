import type { HarnessModelOption, HarnessModelOptionGroup } from "@/hooks/useHarnessModelOptions";

export const OPENCODE_GROUP_SOURCES = {
  codex: "opencode-codex-subscription-catalog",
  minimax: "opencode-minimax-token-plan-catalog",
  omniroute: "omniroute",
  other: "other-opencode-models",
} as const;

export type OpenCodeGroupKey = keyof typeof OPENCODE_GROUP_SOURCES;

export interface OpenCodePickerGroup extends HarnessModelOptionGroup {
  key: OpenCodeGroupKey;
  label: string;
  source: string;
}

const GROUPS: ReadonlyArray<{
  key: OpenCodeGroupKey;
  label: string;
  source: string;
}> = [
  {
    key: "codex",
    label: "Codex Subscription",
    source: OPENCODE_GROUP_SOURCES.codex,
  },
  {
    key: "minimax",
    label: "MiniMax Token Plan",
    source: OPENCODE_GROUP_SOURCES.minimax,
  },
  {
    key: "omniroute",
    label: "OmniRoute Combos",
    source: OPENCODE_GROUP_SOURCES.omniroute,
  },
  {
    key: "other",
    label: "Other OpenCode Models",
    source: OPENCODE_GROUP_SOURCES.other,
  },
];

function groupKey(source: string | undefined): OpenCodeGroupKey {
  if (source === OPENCODE_GROUP_SOURCES.codex) return "codex";
  if (source === OPENCODE_GROUP_SOURCES.minimax) return "minimax";
  if (source === OPENCODE_GROUP_SOURCES.omniroute) return "omniroute";
  return "other";
}

function isAuthenticatedPlanModel(key: OpenCodeGroupKey, model: HarnessModelOption): boolean {
  if (key === "codex") {
    return model.access_source === "codex-subscription" && model.availability === "available";
  }
  if (key === "minimax") {
    return model.access_source === "minimax-token-plan" && model.availability === "available";
  }
  return true;
}

/** Arrange server-provided OpenCode catalogs without inferring access from model labels. */
export function organizeOpenCodeModelGroups(
  groups: HarnessModelOptionGroup[],
): OpenCodePickerGroup[] {
  const organized = new Map<OpenCodeGroupKey, OpenCodePickerGroup>(
    GROUPS.map((group) => [group.key, { ...group, models: [], error: null }]),
  );
  const seenIds = new Set<string>();

  for (const incoming of groups) {
    const key = groupKey(incoming.source);
    const target = organized.get(key)!;
    if (incoming.error && !target.error) target.error = incoming.error;
    for (const model of incoming.models) {
      if (!isAuthenticatedPlanModel(key, model) || seenIds.has(model.id)) continue;
      seenIds.add(model.id);
      target.models.push(model);
    }
  }

  return GROUPS.map((group) => organized.get(group.key)!);
}

export function openCodeModelMatches(model: HarnessModelOption, query: string): boolean {
  const normalized = query.trim().toLocaleLowerCase();
  if (!normalized) return true;
  return [model.label, model.id, model.provider, model.provider_id]
    .filter((value): value is string => typeof value === "string")
    .some((value) => value.toLocaleLowerCase().includes(normalized));
}

export function openCodeOptionValue(
  group: Pick<OpenCodePickerGroup, "key">,
  model: HarnessModelOption,
): string {
  return group.key === "omniroute" ? (model.route_id ?? model.id) : model.id;
}

export function miniMaxProviderIndicator(providerId: string | undefined): string | null {
  if (providerId === "minimax-coding-plan") return "Global";
  if (providerId === "minimax-cn-coding-plan") return "China";
  return providerId ?? null;
}
