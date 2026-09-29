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
