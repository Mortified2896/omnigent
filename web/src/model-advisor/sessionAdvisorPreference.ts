import { getCurrentAuthorId } from "@/lib/identity";
import type { ProviderPreferences } from "./providerPreferences";

/** Session-scoped choice survives navigation and a frontend reload. */
const key = (host: string, session: string) => `omnigent:session-advisor:${host}:${session}`;
export function readSessionAdvisorEnabled(host: string | null, session: string | null): boolean {
  if (!host || !session) return false;
  try {
    return localStorage.getItem(key(host, session)) === "true";
  } catch {
    return false;
  }
}
export function writeSessionAdvisorEnabled(host: string, session: string, enabled: boolean): void {
  try {
    localStorage.setItem(key(host, session), String(enabled));
  } catch {
    /* Keep the current UI choice if storage is unavailable. */
  }
}

export interface SessionAdvisorChoices {
  preferences: ProviderPreferences;
  humanPick: { model: string; accessLane: string | null; effort: string } | null;
}
const choicesKey = (host: string, session: string) =>
  `omnigent:session-advisor-choices:${getCurrentAuthorId()}:${host}:${session}`;
export function readSessionAdvisorChoices(
  host: string,
  session: string,
): SessionAdvisorChoices | null {
  try {
    const raw = localStorage.getItem(choicesKey(host, session));
    if (!raw) return null;
    const value = JSON.parse(raw) as SessionAdvisorChoices;
    const prefs = value.preferences;
    if (prefs?.schema_version !== 3 || !prefs.providers?.openai || !prefs.providers?.glm)
      return null;
    const isStringList = (items: unknown) =>
      Array.isArray(items) && items.every((item) => typeof item === "string");
    if (
      typeof prefs.enabled !== "boolean" ||
      !isStringList(prefs.unresolved_legacy_ids) ||
      !isStringList(prefs.route_review_required) ||
      typeof prefs.human_probability_percent !== "number"
    )
      return null;
    for (const group of [prefs.providers.openai, prefs.providers.glm]) {
      if (
        typeof group.enabled !== "boolean" ||
        typeof group.collapsed !== "boolean" ||
        !isStringList(group.selected_choice_ids) ||
        !isStringList(group.disabled_model_ids) ||
        (group.approval_model_ids !== undefined && !isStringList(group.approval_model_ids))
      )
        return null;
    }
    if (
      value.humanPick !== null &&
      (typeof value.humanPick?.model !== "string" ||
        typeof value.humanPick.effort !== "string" ||
        (value.humanPick.accessLane !== null && typeof value.humanPick.accessLane !== "string"))
    )
      return null;
    return value;
  } catch {
    return null;
  }
}
export function writeSessionAdvisorChoices(
  host: string,
  session: string,
  value: SessionAdvisorChoices,
): void {
  try {
    localStorage.setItem(choicesKey(host, session), JSON.stringify(value));
  } catch {
    /* The current composer still retains its choices. */
  }
}

const modelKey = (host: string, session: string) =>
  `omnigent:keep-chosen-model:${getCurrentAuthorId()}:${host}:${session}`;
export function readKeepChosenModel(host: string | null, session: string | null): boolean {
  if (!host || !session) return true;
  try {
    return localStorage.getItem(modelKey(host, session)) !== "false";
  } catch {
    return true;
  }
}
export function writeKeepChosenModel(host: string, session: string, keep: boolean): void {
  try {
    localStorage.setItem(modelKey(host, session), String(keep));
  } catch {
    /* Keep current UI choice. */
  }
}
