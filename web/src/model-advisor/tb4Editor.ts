/** Pure draft helpers for the TB4 controls; no network, autosave or provider calls.
 * The existing composer/API controller still needs an explicit v2 integration.
 */
export type TB4PoolMode = "paid_only" | "free_only";
export type TB4AccessClass = "chatgpt_plan" | "glm_plan" | "free" | "unknown";

export interface TB4Preferences {
  readonly schema_version: 2;
  readonly enabled: boolean;
  readonly pool_mode: TB4PoolMode;
  readonly allowed_paid_candidate_ids: readonly string[];
  readonly advisor_candidate_id: string | null;
  readonly human_probability_percent: number;
}

export interface ConcretePreferencesV1 {
  readonly schema_version: 1;
  readonly enabled: boolean;
  readonly allowed_candidate_ids: readonly string[];
  readonly advisor_candidate_id: string | null;
  readonly human_probability_percent: number;
}

export interface TB4Option {
  readonly candidate_id: string;
  readonly access_class: TB4AccessClass;
  readonly available: boolean;
  readonly label: string;
}

export const TB4_POOL_MODES = [
  { value: "paid_only", label: "Paid only" },
  { value: "free_only", label: "Free only" },
] as const;

export function parseTB4Floor(input: string): number {
  // Number("") === 0 and parseFloat("40garbage") === 40 are unsafe defaults.
  const trimmed = input.trim();
  if (!/^(?:\d+(?:\.\d*)?|\.\d+)$/.test(trimmed)) {
    throw new Error("Enter a TB4 floor from 0 to 100.");
  }
  const value = Number(trimmed);
  if (!Number.isFinite(value) || value < 0 || value > 100) {
    throw new Error("Enter a TB4 floor from 0 to 100.");
  }
  return value;
}

/** Opt-in copy only: this neither saves defaults nor changes existing rounds. */
export function copyConcreteDefaults(source: ConcretePreferencesV1): TB4Preferences {
  return {
    schema_version: 2,
    enabled: source.enabled,
    pool_mode: "paid_only",
    allowed_paid_candidate_ids: [...source.allowed_candidate_ids],
    advisor_candidate_id: source.advisor_candidate_id,
    human_probability_percent: source.human_probability_percent,
  };
}

/** Permit an incomplete draft; validate before save/submit, not during typing. */
export function setTB4Pool(draft: TB4Preferences, mode: TB4PoolMode): TB4Preferences {
  return { ...draft, pool_mode: mode };
}

export function setAllowedPaidCandidates(
  draft: TB4Preferences,
  ids: readonly string[],
): TB4Preferences {
  return { ...draft, allowed_paid_candidate_ids: [...ids] };
}

export function validateTB4Draft(
  draft: TB4Preferences,
  options: readonly TB4Option[],
): readonly string[] {
  const errors: string[] = [];
  if (!draft.enabled) return errors;
  if (!Number.isInteger(draft.human_probability_percent)
      || draft.human_probability_percent < 0 || draft.human_probability_percent > 100) {
    errors.push("Human assignment probability must be an integer from 0 to 100.");
  }
  const byId = new Map(options.map((option) => [option.candidate_id, option]));
  if (byId.size !== options.length) errors.push("Catalog contains duplicate candidate IDs.");
  const advisor = byId.get(draft.advisor_candidate_id ?? "");
  if (!advisor || !advisor.available || advisor.access_class === "unknown") {
    errors.push("Select an available, qualified advisor; it will not be replaced automatically.");
  }
  if (draft.pool_mode === "paid_only") {
    if (draft.allowed_paid_candidate_ids.length === 0) {
      errors.push("Select allowed OpenAI/GLM paid-plan model and effort choices.");
    }
    if (new Set(draft.allowed_paid_candidate_ids).size !== draft.allowed_paid_candidate_ids.length) {
      errors.push("The paid allowlist contains duplicate choices.");
    }
    for (const id of draft.allowed_paid_candidate_ids) {
      const option = byId.get(id);
      if (!option || !["chatgpt_plan", "glm_plan"].includes(option.access_class)) {
        errors.push(`Saved paid choice ${id} is missing or is no longer a qualified paid-plan route.`);
      }
    }
    if (!draft.allowed_paid_candidate_ids.some((id) => {
      const option = byId.get(id);
      return option?.available && ["chatgpt_plan", "glm_plan"].includes(option.access_class);
    })) {
      errors.push("No selected paid executor is currently available.");
    }
  } else if (draft.pool_mode === "free_only") {
    if (!options.some((option) => option.access_class === "free" && option.available)) {
      errors.push("No qualified free executor is available. Paid fallback is disabled.");
    }
  } else {
    errors.push("Choose Paid only or Free only.");
  }
  // Passing client validation is not TB4 admission or permission to dispatch.
  return errors;
}

export function advisorUsageNotice(
  draft: TB4Preferences,
  options: readonly TB4Option[],
): string | null {
  const advisor = options.find((option) => option.candidate_id === draft.advisor_candidate_id);
  if (draft.pool_mode === "free_only"
      && advisor && ["chatgpt_plan", "glm_plan"].includes(advisor.access_class)) {
    return "Free only applies to execution. Your separately selected advisor uses paid-plan allowance.";
  }
  return null;
}
