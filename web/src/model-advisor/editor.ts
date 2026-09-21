/** Pure editor state. Persistence is server-owned; no localStorage writes. */
export interface AdvisorPreferences {
  schema_version: 1;
  enabled: boolean;
  allowed_candidate_ids: string[];
  advisor_candidate_id: string | null;
  human_probability_percent: number;
}

export interface AdvisorOption {
  candidate_id: string;
  model_id: string;
  display_name: string;
  lane_id: "codex-direct" | "glm-direct" | "omniroute";
  reasoning_effort: string;
  available: boolean;
  unavailable_reason?: string;
}

export interface SavedPreferences {
  version: number;
  etag: string;
  preferences: AdvisorPreferences;
}

export interface EditorState {
  scope: string;
  saved: SavedPreferences | null;
  draft: AdvisorPreferences | null;
  dirty: boolean;
  error: string | null;
}

export type EditorAction =
  | { type: "scope"; scope: string }
  | { type: "hydrate"; scope: string; saved: SavedPreferences }
  | { type: "edit"; preferences: AdvisorPreferences }
  | { type: "saved"; scope: string; saved: SavedPreferences; submitted: AdvisorPreferences }
  | { type: "error"; scope: string; message: string };

export function initialEditor(scope: string): EditorState {
  return { scope, saved: null, draft: null, dirty: false, error: null };
}

export function samePreferences(a: AdvisorPreferences, b: AdvisorPreferences): boolean {
  return (
    a.enabled === b.enabled &&
    a.advisor_candidate_id === b.advisor_candidate_id &&
    a.human_probability_percent === b.human_probability_percent &&
    JSON.stringify(a.allowed_candidate_ids) === JSON.stringify(b.allowed_candidate_ids)
  );
}

export function editorReducer(state: EditorState, action: EditorAction): EditorState {
  if (action.type === "scope")
    return action.scope === state.scope ? state : initialEditor(action.scope);
  if (action.type === "edit") {
    if (!state.draft) return state; // No edits/writes before initial hydration.
    return {
      ...state,
      draft: action.preferences,
      dirty: !state.saved || !samePreferences(state.saved.preferences, action.preferences),
      error: null,
    };
  }
  if (action.scope !== state.scope) return state; // A response for another host/user.
  if (action.type === "error") return { ...state, error: action.message };
  if (state.saved && action.saved.version < state.saved.version) return state;
  if (action.type === "hydrate") {
    if (state.dirty) return state; // Never discard edits after a delayed fetch.
    return {
      ...state,
      saved: action.saved,
      draft: action.saved.preferences,
      dirty: false,
      error: null,
    };
  }
  const unchanged = state.draft !== null && samePreferences(state.draft, action.submitted);
  return {
    ...state,
    saved: action.saved,
    draft: unchanged ? action.saved.preferences : state.draft,
    dirty: !unchanged,
    error: null,
  };
}

export function toggleCandidate(
  prefs: AdvisorPreferences,
  id: string,
  included: boolean,
): AdvisorPreferences {
  const choices = new Set(prefs.allowed_candidate_ids);
  if (included) choices.add(id);
  else choices.delete(id);
  return { ...prefs, allowed_candidate_ids: [...choices] };
}

export function optionLabel(option: AdvisorOption): string {
  const lane =
    option.lane_id === "codex-direct"
      ? "ChatGPT plan · Direct"
      : option.lane_id === "glm-direct"
        ? "Z.AI Direct"
        : "OmniRoute GLM";
  const effort =
    option.reasoning_effort === "not_applicable"
      ? "No effort setting"
      : `${option.reasoning_effort} reasoning`;
  return `${option.display_name} · ${lane} · ${effort}`;
}

export function validateEditor(
  prefs: AdvisorPreferences,
  options: readonly AdvisorOption[],
): string | null {
  if (
    !Number.isInteger(prefs.human_probability_percent) ||
    prefs.human_probability_percent < 0 ||
    prefs.human_probability_percent > 100
  )
    return "Decision balance must be between 0 and 100.";
  if (!prefs.enabled) return null;
  if (!prefs.allowed_candidate_ids.length) return "Select at least one allowed answer.";
  const byId = new Map(options.map((option) => [option.candidate_id, option]));
  for (const id of prefs.allowed_candidate_ids) {
    if (!byId.get(id)?.available)
      return "A saved answer choice is unavailable. Remove it or restore its connection.";
  }
  if (!prefs.advisor_candidate_id || !byId.get(prefs.advisor_candidate_id)?.available)
    return "Select an available advisor model and effort.";
  return null;
}
