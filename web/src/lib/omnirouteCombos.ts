/**
 * OmniRoute combo catalog: curated display names + lookup helpers.
 *
 * A "combo" is a routing combination the local OmniRoute runtime exposes
 * on its OpenAI-compatible ``/v1/models`` endpoint, e.g.
 * ``auto/best-coding`` (the curated "best coding" pool), or
 * ``auto/coding:fast``. Combos are NOT concrete models — they map to a
 * concrete provider/model on the routing side at execution time. The web
 * UI surfaces them as selectable rows in the model picker alongside
 * concrete model options, but keeps their native id (verbatim, including
 * colons / slashes / brackets) on the wire so the runner dispatches
 * exactly the combo the user picked.
 *
 * This module is leaf (no React / store imports) so the picker UI, the
 * chat store, and the route-approval card can all read it without a
 * circular import.
 */

/** Curated display names for the three required coding combos. */
export const OMNIROUTE_COMBO_DISPLAY_NAMES: Record<string, string> = {
  "auto/best-coding": "OmniRoute Coding Best",
  "auto/coding:fast": "OmniRoute Coding Fast",
  "auto/coding:reliable": "OmniRoute Coding Reliable",
};

/** Curated combo ids the team committed to surface in the picker. */
export const CURATED_OMNIROUTE_COMBO_IDS: readonly string[] = [
  "auto/best-coding",
  "auto/coding:fast",
  "auto/coding:reliable",
] as const;

/**
 * One live row from the OmniRoute combo catalog.
 *
 * Mirrors the JSON wire shape produced by
 * ``GET /v1/omniroute/combos`` (snake_case) and the snapshot's
 * ``omniroute_combos`` field. ``id`` is preserved verbatim — never
 * normalized, never lowercased — so a colon in a combo id never
 * disappears on the way to the runner.
 */
export interface OmniRouteCombo {
  /** Verbatim native combo id, e.g. ``"auto/coding:fast"``. */
  id: string;
  /** Curated display name, e.g. ``"OmniRoute Coding Fast"``. */
  display_name: string;
  /** Always ``"omniroute"`` for entries from this catalog. */
  provider: "omniroute";
  /** Always ``"combo"`` — distinguishes a curated combo from a concrete model. */
  kind: "combo";
  /** Allowed reasoning-effort values for this combo, e.g. ``["low", "medium"]``. */
  reasoning_efforts: string[];
  /** Highest effort the combo accepts, e.g. ``"high"``. */
  max_reasoning_effort: string;
  /** Recommended effort, e.g. ``"medium"``. */
  default_reasoning_effort: string;
  /** True when the routing agent gates this combo on a confirm step. */
  requires_explicit_approval: boolean;
}

/**
 * Return the curated display name for a combo id, falling back to the id.
 *
 * Public helper used by the model picker, the route-approval card, and
 * the chat store. Never throws, never logs the id, and never rewrites
 * colons / slashes / brackets — an unknown combo id returns the raw
 * verbatim id so the runner can still dispatch it.
 *
 * @param id - A combo id, e.g. ``"auto/coding:fast"``.
 * @returns The display name (``"OmniRoute Coding Fast"``) or the raw id.
 */
export function getOmniRouteComboDisplayName(id: string | null | undefined): string {
  if (!id) return "";
  if (Object.prototype.hasOwnProperty.call(OMNIROUTE_COMBO_DISPLAY_NAMES, id)) {
    return OMNIROUTE_COMBO_DISPLAY_NAMES[id];
  }
  return id;
}

/**
 * Look up a combo in a live catalog array (e.g. the snapshot's
 * ``omniroute_combos`` field). Returns ``null`` when the catalog is
 * missing or doesn't contain the id.
 *
 * @param combos - Live combo catalog (may be empty/null).
 * @param id - The combo id to look up.
 * @returns The matching entry, or ``null`` when not found.
 */
export function findOmniRouteCombo(
  combos: readonly OmniRouteCombo[] | null | undefined,
  id: string | null | undefined,
): OmniRouteCombo | null {
  if (!combos || !id) return null;
  return combos.find((combo) => combo.id === id) ?? null;
}

/**
 * Whether *id* is one of the three curated coding combos.
 *
 * Used by the picker to group the curated rows under a clear
 * "OmniRoute" header even when the live catalog also returns
 * non-curated combos.
 *
 * @param id - The combo id to test.
 * @returns True when the id matches one of the curated combos.
 */
export function isCuratedOmniRouteCombo(id: string | null | undefined): boolean {
  if (!id) return false;
  return (CURATED_OMNIROUTE_COMBO_IDS as readonly string[]).includes(id);
}
