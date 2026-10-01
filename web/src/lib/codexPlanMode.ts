const CODEX_NATIVE_WRAPPER = "codex-native-ui";
const CODEX_NATIVE_COLLABORATION_MODE_LABEL_KEY =
  "omnigent.harnesses.codex_native.main.collaboration_mode";

export type CodexPlanModeLabelSource =
  { labels?: Record<string, string | null> | null } | null | undefined;

export function isCodexNativeSession(source: CodexPlanModeLabelSource): boolean {
  return source?.labels?.["omnigent.wrapper"] === CODEX_NATIVE_WRAPPER;
}

export function codexPlanModeFromLabels(
  labels: Record<string, string | null> | null | undefined,
): boolean {
  const mode =
    labels && Object.hasOwn(labels, CODEX_NATIVE_COLLABORATION_MODE_LABEL_KEY)
      ? labels[CODEX_NATIVE_COLLABORATION_MODE_LABEL_KEY]
      : labels?.["omnigent.codex_native.collaboration_mode"];
  return mode === "plan";
}

export function codexPlanModeFromSession(source: CodexPlanModeLabelSource): boolean {
  return isCodexNativeSession(source) && codexPlanModeFromLabels(source?.labels);
}
