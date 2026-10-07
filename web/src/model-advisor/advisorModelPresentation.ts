/** Advisor-side adapter over the shared model presentation system.
 *
 * The primary execution picker and the Advisor must present the same live
 * catalog the same way. The shared vocabulary (lane labels, route summaries,
 * group headings, effort labels/ranks) lives in `modelPickerGroups` and
 * `providerPreferences`; this module maps the Advisor's LOGICAL catalog
 * (one choice per provider/checkpoint/effort, never one row per transport)
 * onto that presentation without collapsing route information: a logical
 * choice keeps every lane it can execute on and shows them as its route line.
 *
 * Persistence stays logical: a row's selection key is the provider+model pair
 * (the same key the composer's model chip resolves against), transport
 * preference remains a per-provider settings concept, and no lane here mints
 * a second selectable row for the same checkpoint.
 */
import { routeSummaryForLanes } from "@/lib/modelPickerGroups";
import {
  PROVIDER_LABELS,
  effortLabel,
  reasoningEffortRank,
  type LogicalOption,
  type ProviderGroup,
  type ProviderPreferences,
} from "@/model-advisor/providerPreferences";

/** The stable selection key for one advisor model row (provider + checkpoint). */
export function advisorModelKey(option: Pick<LogicalOption, "provider" | "model_id">): string {
  return JSON.stringify([option.provider, option.model_id]);
}

/** One rendered advisor model row: a logical model with its route metadata. */
export interface AdvisorModelRow {
  /** Stable selection key (`advisorModelKey`) — what persistence stores. */
  key: string;
  provider: ProviderGroup;
  modelId: string;
  displayName: string;
  /** True when at least one effort choice is qualified by the live catalog. */
  available: boolean;
  /** Why every choice is unavailable; only set when `available` is false. */
  disabledReason?: string;
  /** The transports this model can currently execute on, host-labeled. */
  routeSummary: string;
  /** Search keywords: display name, physical spellings, route labels. */
  keywords: string[];
  /** The model's effort choices, ascending effort rank. */
  choices: LogicalOption[];
}

/** Project the logical catalog into advisor model rows via the shared presentation. */
export function advisorModelRows(options: readonly LogicalOption[]): AdvisorModelRow[] {
  const rows = new Map<string, AdvisorModelRow>();
  const lanesByKey = new Map<string, string[]>();
  for (const option of options) {
    const key = advisorModelKey(option);
    let row = rows.get(key);
    if (!row) {
      row = {
        key,
        provider: option.provider,
        modelId: option.model_id,
        displayName: option.display_name,
        available: false,
        routeSummary: "",
        keywords: [],
        choices: [],
      };
      rows.set(key, row);
      lanesByKey.set(key, []);
    }
    row.choices.push(option);
    if (option.available) {
      row.available = true;
      row.disabledReason = undefined;
      const lanes = lanesByKey.get(key) ?? [];
      for (const lane of option.access_lanes) {
        if (!lanes.includes(lane)) lanes.push(lane);
      }
      lanesByKey.set(key, lanes);
    } else if (!row.available && !row.disabledReason) {
      row.disabledReason = option.unavailable_reason ?? "Unavailable from the current host catalog";
    }
  }
  return [...rows.values()].map((row) => {
    const routeSummary = routeSummaryForLanes(lanesByKey.get(row.key) ?? []);
    return {
      ...row,
      choices: [...row.choices].sort(
        (left, right) =>
          reasoningEffortRank(left.reasoning_effort) - reasoningEffortRank(right.reasoning_effort),
      ),
      routeSummary,
      keywords: Array.from(
        new Set([
          row.displayName,
          row.modelId,
          ...row.choices.flatMap((choice) => choice.model_ids),
          ...routeSummary.split(" · ").filter(Boolean),
        ]),
      ),
    };
  });
}

/** The menu heading for an advisor provider group (provider + access plan). */
export function advisorGroupHeading(provider: ProviderGroup): string {
  return PROVIDER_LABELS[provider];
}

/** The effort options one advisor model currently offers, ascending rank. */
export function advisorEffortOptions(
  choices: readonly LogicalOption[],
): readonly { value: string; label: string; choice: LogicalOption }[] {
  return choices
    .filter((choice) => choice.available)
    .sort(
      (left, right) =>
        reasoningEffortRank(left.reasoning_effort) - reasoningEffortRank(right.reasoning_effort),
    )
    .map((choice) => ({
      value: choice.reasoning_effort,
      label: effortLabel(choice.reasoning_effort),
      choice,
    }));
}

/**
 * The advisor choice a switch should land on, by the shared concrete-effort
 * precedence: the effort already selected when the new model still offers it,
 * then the catalog-declared default (a choice the host flags as a lane
 * default), then a deterministic supported fallback (lowest effort rank, then
 * choice id so repeated renders agree). `not_applicable` rows participate
 * like any other spelling — they are real catalog choices, never an implicit
 * "Default" pseudo-option.
 */
export function reconcileAdvisorChoice(
  choices: readonly LogicalOption[],
  currentEffort: string | null | undefined,
): LogicalOption | null {
  const available = choices.filter((choice) => choice.available);
  if (available.length === 0) return null;
  if (currentEffort) {
    const kept = available.find((choice) => choice.reasoning_effort === currentEffort);
    if (kept) return kept;
  }
  const declared = available
    .filter((choice) => (choice.default_access_lanes ?? []).length > 0)
    .sort(
      (left, right) =>
        reasoningEffortRank(left.reasoning_effort) - reasoningEffortRank(right.reasoning_effort) ||
        left.choice_id.localeCompare(right.choice_id),
    );
  if (declared.length > 0) return declared[0];
  return [...available].sort(
    (left, right) =>
      reasoningEffortRank(left.reasoning_effort) - reasoningEffortRank(right.reasoning_effort) ||
      left.choice_id.localeCompare(right.choice_id),
  )[0];
}

/**
 * Seed an advisor draft that has no valid configuration so enabling produces
 * a coherent state instead of a validation error.
 *
 * Two orthogonal rules, applied only where the draft is genuinely empty:
 * an answer pool with no remembered choices gets each enabled provider's
 * declared-default choice (the catalog's own "established default") plus the
 * composer's resolved human choice so the round stays launchable; a null
 * recommender choice gets the deterministic declared default. Saved-but-
 * drifted configurations are left untouched — their explicit review flow
 * owns them. Returns null only when a never-configured draft cannot be
 * seeded because the live catalog has nothing available; the caller then
 * keeps the real problem visible instead of inventing a choice.
 */
export function seedFreshAdvisorDraft(
  draft: ProviderPreferences,
  options: readonly LogicalOption[],
  humanChoiceId: string | null,
): ProviderPreferences | null {
  if (draft.unresolved_legacy_ids.length) return draft;
  const available = options.filter((option) => option.available);
  const openaiIds = draft.providers.openai.selected_choice_ids;
  const glmIds = draft.providers.glm.selected_choice_ids;
  const neverConfigured = openaiIds.length === 0 && glmIds.length === 0 && !draft.advisor_choice_id;
  if (!neverConfigured && draft.advisor_choice_id && (openaiIds.length || glmIds.length)) {
    return draft;
  }
  if (available.length === 0) return neverConfigured ? null : draft;
  // One choice per enabled provider: the provider's declared-default effort
  // when the catalog declares one, else the deterministic first available.
  const defaultChoiceFor = (provider: ProviderGroup): LogicalOption | null => {
    const providerChoices = available.filter((option) => option.provider === provider);
    const declared = providerChoices.filter(
      (option) => (option.default_access_lanes ?? []).length > 0,
    );
    return reconcileAdvisorChoice(declared, null) ?? reconcileAdvisorChoice(providerChoices, null);
  };
  const pool = new Set<string>(
    [openaiIds, glmIds].flat().filter((id) => available.some((option) => option.choice_id === id)),
  );
  if (openaiIds.length === 0 && glmIds.length === 0) {
    for (const choice of [defaultChoiceFor("openai"), defaultChoiceFor("glm")]) {
      if (choice) pool.add(choice.choice_id);
    }
    if (humanChoiceId && available.some((option) => option.choice_id === humanChoiceId)) {
      pool.add(humanChoiceId);
    }
  }
  const preferredAdvisor = available.find(
    (option) =>
      /^(?:(?:openai|codex)\/)?gpt-6-luna$/.test(option.model_id) &&
      option.reasoning_effort === "max",
  );
  const advisorChoice = draft.advisor_choice_id
    ? null
    : (preferredAdvisor ??
      reconcileAdvisorChoice(
        available.filter((option) => pool.has(option.choice_id)),
        null,
      ) ??
      reconcileAdvisorChoice(available, null));
  if (!draft.advisor_choice_id && !advisorChoice) return neverConfigured ? null : draft;
  // A remembered pool is preserved verbatim — only a truly empty pool is
  // seeded, and nothing the user saved is silently dropped here.
  const seededIds = [...pool];
  const nextOpenai = openaiIds.length
    ? openaiIds
    : seededIds.filter((id) =>
        available.some((option) => option.choice_id === id && option.provider === "openai"),
      );
  const nextGlm = glmIds.length
    ? glmIds
    : seededIds.filter((id) =>
        available.some((option) => option.choice_id === id && option.provider === "glm"),
      );
  if (!advisorChoice && nextOpenai === openaiIds && nextGlm === glmIds) return draft;
  return {
    ...draft,
    providers: {
      openai: { ...draft.providers.openai, selected_choice_ids: nextOpenai },
      glm: { ...draft.providers.glm, selected_choice_ids: nextGlm },
    },
    advisor_choice_id: draft.advisor_choice_id ?? advisorChoice?.choice_id ?? null,
  };
}
