import type { NativeModelOption } from "./types";

/** Catalog/provider prefixes a native model id may carry for comparison only. */
const CATALOG_PREFIXES = ["databricks-", "system.ai.", "codex/"] as const;
const CODEX_EFFORT_LEVEL_ORDER = [
  "minimal",
  "low",
  "medium",
  "high",
  "xhigh",
  "max",
  "ultra",
] as const;
const CODEX_EFFORT_LEVEL_RANK = new Map<string, number>(
  CODEX_EFFORT_LEVEL_ORDER.map((effort, index) => [effort, index]),
);

/**
 * Fold a model id to the spelling Codex row ids compare in.
 *
 * Comparison only, never a value to send anywhere: Codex spells versions with
 * dots (``gpt-5.6-luna``) where the catalog spells them with dashes
 * (``databricks-gpt-5-6-luna``), and Codex's bundled rows carry its own
 * spelling in both ``id`` and ``model``. Mirrors the server's
 * ``comparable_model_id``.
 */
function comparableModelId(model: string | null | undefined): string {
  // Null-safe: a picker row may carry a null ``model`` (cursor rows have
  // only ``id`` + ``displayName``), and folding it must not throw. An empty
  // fold never equals a real (non-empty) target, so it simply never matches.
  const raw = model?.trim();
  if (!raw) return "";
  let bare = raw.toLowerCase();
  if (bare.endsWith("[1m]")) bare = bare.slice(0, -"[1m]".length);
  for (const prefix of CATALOG_PREFIXES) {
    if (bare.startsWith(prefix)) {
      bare = bare.slice(prefix.length);
      break;
    }
  }
  return bare.replaceAll(".", "-");
}

/**
 * Find a native picker option by its UI alias or provider-facing model id.
 *
 * Falls back to comparing folded spellings so a session bound to a catalog id
 * still resolves to the Codex row naming the same model.
 *
 * @param options - Native model options from the session snapshot.
 * @param model - Candidate model id, e.g. ``"gpt-5.5"``.
 * @returns The matching option, or ``null`` when unknown.
 */
export function findNativeModelOption(
  options: readonly NativeModelOption[],
  model: string | null | undefined,
): NativeModelOption | null {
  const raw = model?.trim();
  if (!raw) return null;
  const exact = options.find((option) => option.id === raw || option.model === raw);
  if (exact !== undefined) return exact;
  const target = comparableModelId(raw);
  return (
    options.find(
      (option) =>
        (option.id != null && comparableModelId(option.id) === target) ||
        (option.model != null && comparableModelId(option.model) === target),
    ) ?? null
  );
}

/**
 * Whether a sticky model id is one Codex advertised for this session.
 *
 * @param options - Codex model options from the session snapshot.
 * @param model - Candidate model id.
 * @returns True only when the candidate matches a Codex-returned option.
 */
export function isCodexNativeModel(
  options: readonly NativeModelOption[],
  model: string | null | undefined,
): boolean {
  return findNativeModelOption(options, model) !== null;
}

/**
 * Explicit effort levels for the currently selected Codex model.
 *
 * Codex's ``isDefault`` is a static property of its bundled catalog, not the
 * model a session launched with, so it cannot stand in for an unresolved
 * model — it would offer levels the running model rejects. An unknown model
 * yields no levels and the caller hides the picker until the model resolves.
 * The catalog's ``default`` sentinel means "use the model's configured
 * default" rather than an explicit reasoning level, so it is omitted from the
 * picker while every other catalog-provided level is kept.
 *
 * @param options - Codex model options from the session snapshot.
 * @param currentModel - Active override or bound model id.
 * @returns Model-specific effort values from Codex ``model/list``.
 */
export function codexEffortLevelsForModel(
  options: readonly NativeModelOption[],
  currentModel: string | null | undefined,
): readonly string[] {
  const selected = findNativeModelOption(options, currentModel);
  if (selected === null) return [];
  const efforts = selected.supportedReasoningEfforts ?? [];
  return Array.from(
    new Set(
      efforts
        .map((option) => option.reasoningEffort)
        .filter(
          (effort): effort is string =>
            typeof effort === "string" &&
            effort.length > 0 &&
            effort.trim().toLowerCase() !== "default",
        ),
    ),
  ).sort((left, right) => {
    const leftRank = CODEX_EFFORT_LEVEL_RANK.get(left.trim().toLowerCase());
    const rightRank = CODEX_EFFORT_LEVEL_RANK.get(right.trim().toLowerCase());
    if (leftRank === undefined) return rightRank === undefined ? 0 : 1;
    if (rightRank === undefined) return -1;
    return leftRank - rightRank;
  });
}

/**
 * Baseline effort rungs offered for a *resolved* codex-native row that carries
 * no per-model tiers.
 *
 * A row with empty ``supportedReasoningEfforts`` is a catalog metadata gap —
 * e.g. OmniRoute GLM routes the gateway serves without ``effort_tiers`` — not
 * an unknown model, so hiding the picker misreads it. The rungs mirror the
 * server's probed serving facts for GLM on the codex/Responses wire (tops out
 * at ``high``; see ``omnigent/util/reasoning_effort.py``), and the codex
 * process remains the runtime validator for the pairing. Rows that advertise
 * tiers keep exactly those; an unresolved model still yields no rungs.
 */
export const CODEX_UNADVERTISED_MODEL_EFFORTS: readonly string[] = ["low", "medium", "high"];

/**
 * Effort ladder for the selected Codex model, with a baseline for metadata gaps.
 *
 * Unlike :func:`codexEffortLevelsForModel`, a row that resolves but advertises
 * no reasoning tiers falls back to :data:`CODEX_UNADVERTISED_MODEL_EFFORTS`
 * instead of an empty ladder (which would hide the picker). Use this for the
 * codex-native picker surfaces; keep the strict variant where an unadvertised
 * ladder must stay empty (e.g. Devin, where effort is a model-variant suffix).
 */
export function codexEffortLadderForModel(
  options: readonly NativeModelOption[],
  currentModel: string | null | undefined,
): readonly string[] {
  const advertised = codexEffortLevelsForModel(options, currentModel);
  if (advertised.length > 0) return advertised;
  return findNativeModelOption(options, currentModel) === null
    ? []
    : CODEX_UNADVERTISED_MODEL_EFFORTS;
}
