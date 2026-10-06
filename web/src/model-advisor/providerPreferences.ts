/** V3 presentation model. The server owns qualification, persistence and dispatch. */
export type ProviderGroup = "openai" | "glm";
export type TransportPreference = "omniroute_preferred" | "omniroute_only" | "direct_only";
export const PROVIDER_GROUPS: readonly ProviderGroup[] = ["openai", "glm"];
export const PROVIDER_LABELS: Record<ProviderGroup, string> = {
  openai: "OpenAI / ChatGPT plan",
  glm: "GLM / Z.AI",
};

const REASONING_EFFORT_ORDER = ["low", "medium", "high", "xhigh", "max", "ultra"] as const;
const REASONING_EFFORT_RANK: ReadonlyMap<string, number> = new Map(
  REASONING_EFFORT_ORDER.map((effort, index) => [effort, index]),
);

/** Deterministic sort key for reasoning efforts; unknown spellings sort last. */
export function reasoningEffortRank(effort: string): number {
  return REASONING_EFFORT_RANK.get(effort) ?? REASONING_EFFORT_ORDER.length;
}

export interface ProviderSelection {
  enabled: boolean;
  collapsed: boolean;
  selected_choice_ids: string[];
  disabled_model_ids: string[];
  approval_model_ids?: string[];
  approval_choice_ids?: string[];
  transport_preference: TransportPreference;
}

export interface ProviderPreferences {
  schema_version: 3;
  enabled: boolean;
  providers: Record<ProviderGroup, ProviderSelection>;
  advisor_choice_id: string | null;
  human_probability_percent: number;
  unresolved_legacy_ids: string[];
  route_review_required: ProviderGroup[];
}

export interface ProviderPreferencesV2 {
  schema_version: 2;
  enabled: boolean;
  providers: Record<ProviderGroup, Omit<ProviderSelection, "disabled_model_ids">>;
  advisor_choice_id: string | null;
  human_probability_percent: number;
  unresolved_legacy_ids: string[];
  route_review_required: ProviderGroup[];
}

export type ProviderPreferencesDto = ProviderPreferences | ProviderPreferencesV2;

/** A v2 payload has exactly the old meaning: no model is disabled by default. */
export function normalizeProviderPreferences(prefs: ProviderPreferencesDto): ProviderPreferences {
  if (prefs.schema_version === 3) return prefs;
  return {
    ...prefs,
    schema_version: 3,
    providers: {
      openai: { ...prefs.providers.openai, disabled_model_ids: [] },
      glm: { ...prefs.providers.glm, disabled_model_ids: [] },
    },
  };
}

/** One canonical checkpoint + effective effort, NEVER one row per transport. */
export interface LogicalOption {
  choice_id: string;
  provider: ProviderGroup;
  model_id: string;
  display_name: string;
  reasoning_effort: string;
  /** Physical spellings are display/catalog metadata for mapping the existing
   * composer pick; they never enter the advisor prompt or persisted choice id. */
  model_ids: string[];
  access_lanes: string[];
  /** Lanes whose host catalog marks this reasoning option as the default. */
  default_access_lanes?: string[];
  available: boolean;
  unavailable_reason?: string;
}

export interface ModelGroup {
  model_id: string;
  display_name: string;
  options: LogicalOption[];
}

export function emptyProviderPreferences(): ProviderPreferences {
  const group = (): ProviderSelection => ({
    enabled: true,
    collapsed: false,
    selected_choice_ids: [],
    disabled_model_ids: [],
    transport_preference: "omniroute_preferred",
  });
  return {
    schema_version: 3,
    enabled: false,
    providers: { openai: group(), glm: group() },
    advisor_choice_id: null,
    human_probability_percent: 50,
    unresolved_legacy_ids: [],
    route_review_required: [],
  };
}

export function updateProvider(
  prefs: ProviderPreferences,
  provider: ProviderGroup,
  patch: Partial<ProviderSelection>,
): ProviderPreferences {
  return {
    ...prefs,
    providers: { ...prefs.providers, [provider]: { ...prefs.providers[provider], ...patch } },
  };
}

export function toggleProvider(
  prefs: ProviderPreferences,
  provider: ProviderGroup,
  enabled: boolean,
): ProviderPreferences {
  // Only the answer-pool mask changes. Memory and the advisor stay untouched.
  return updateProvider(prefs, provider, { enabled });
}

export function toggleModel(
  prefs: ProviderPreferences,
  provider: ProviderGroup,
  modelId: string,
  enabled: boolean,
): ProviderPreferences {
  const disabled = new Set(prefs.providers[provider].disabled_model_ids);
  if (enabled) disabled.delete(modelId);
  else disabled.add(modelId);
  return updateProvider(prefs, provider, { disabled_model_ids: [...disabled] });
}

export function toggleEffort(
  prefs: ProviderPreferences,
  provider: ProviderGroup,
  choiceId: string,
  checked: boolean,
): ProviderPreferences {
  const remembered = new Set(prefs.providers[provider].selected_choice_ids);
  if (checked) remembered.add(choiceId);
  else remembered.delete(choiceId);
  return updateProvider(prefs, provider, { selected_choice_ids: [...remembered] });
}

export function selectTransport(
  prefs: ProviderPreferences,
  provider: ProviderGroup,
  transport_preference: TransportPreference,
): ProviderPreferences {
  return {
    ...updateProvider(prefs, provider, { transport_preference }),
    route_review_required: prefs.route_review_required.filter((value) => value !== provider),
  };
}

export function activeChoiceIds(
  prefs: ProviderPreferences,
  catalog: readonly LogicalOption[],
): string[] {
  if (!prefs.enabled) return [];
  const byId = new Map(catalog.map((option) => [option.choice_id, option]));
  return PROVIDER_GROUPS.flatMap((provider) => {
    const group = prefs.providers[provider];
    if (!group.enabled) return [];
    return group.selected_choice_ids.filter((id) => {
      const option = byId.get(id);
      return (
        option !== undefined &&
        option.provider === provider &&
        option.available &&
        !group.disabled_model_ids.includes(option.model_id)
      );
    });
  });
}

export function groupModels(
  options: readonly LogicalOption[],
  provider: ProviderGroup,
): ModelGroup[] {
  const models = new Map<string, ModelGroup>();
  const ids = new Set<string>();
  const combinations = new Set<string>();
  for (const option of options) {
    if (option.provider !== provider) continue;
    const combination = JSON.stringify([option.model_id, option.reasoning_effort]);
    if (ids.has(option.choice_id) || combinations.has(combination)) {
      throw new Error("Duplicate logical model/effort in provider catalog");
    }
    ids.add(option.choice_id);
    combinations.add(combination);
    let model = models.get(option.model_id);
    if (!model) {
      model = { model_id: option.model_id, display_name: option.display_name, options: [] };
      models.set(option.model_id, model);
    }
    model.options.push(option);
  }
  return [...models.values()].map((model) => ({
    ...model,
    options: [...model.options].sort(
      (left, right) =>
        (REASONING_EFFORT_RANK.get(left.reasoning_effort) ?? REASONING_EFFORT_ORDER.length) -
        (REASONING_EFFORT_RANK.get(right.reasoning_effort) ?? REASONING_EFFORT_ORDER.length),
    ),
  }));
}

export function effortLabel(effort: string): string {
  const labels: Record<string, string> = {
    not_applicable: "No effort setting",
    low: "Low",
    medium: "Medium",
    high: "High",
    xhigh: "xHigh",
    max: "Max",
    ultra: "Ultra",
  };
  return labels[effort] ?? effort;
}

export function effectiveOptions(
  prefs: ProviderPreferences,
  catalog: readonly LogicalOption[],
): LogicalOption[] {
  if (!prefs.enabled) return [];
  if (prefs.route_review_required.length || prefs.unresolved_legacy_ids.length) {
    throw new Error("Review migrated choices and connection preferences first");
  }
  const byId = new Map(catalog.map((option) => [option.choice_id, option]));
  if (byId.size !== catalog.length) throw new Error("Duplicate logical choice ID");
  const result: LogicalOption[] = [];
  for (const provider of PROVIDER_GROUPS) {
    const group = prefs.providers[provider];
    if (!group.enabled) continue;
    for (const id of group.selected_choice_ids) {
      const option = byId.get(id);
      if (!option || option.provider !== provider) {
        throw new Error("A remembered choice is missing or belongs to another provider");
      }
      if (group.disabled_model_ids.includes(option.model_id)) continue;
      if (!option.available) {
        throw new Error("A selected reasoning choice is unavailable from the live catalog");
      }
      // Do not change the choice set based on the preferred transport's health.
      result.push(option);
    }
  }
  if (!result.length) throw new Error("Select at least one active model and reasoning level");
  return result;
}
