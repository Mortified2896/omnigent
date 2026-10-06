import { useCallback, useState } from "react";
import { getCurrentUserId } from "@/lib/identity";

function read(key: string): string[] {
  try {
    const tags: unknown = JSON.parse(sessionStorage.getItem(key) ?? "[]");
    return Array.isArray(tags)
      ? tags.filter((t): t is string => typeof t === "string" && t.length <= 40).slice(0, 8)
      : [];
  } catch {
    return [];
  }
}
export function useTaskTags(sessionId: string | null) {
  const key = `omnigent.task-tags:${getCurrentUserId()}:${sessionId ?? "new"}`;
  const [drafts, setDrafts] = useState<Record<string, string[]>>({});
  const tags = drafts[key] ?? read(key);
  const setTags = useCallback(
    (value: string[]) => {
      setDrafts((previous) => ({ ...previous, [key]: value }));
      try {
        sessionStorage.setItem(key, JSON.stringify(value));
      } catch {
        /* Keep the draft in memory. */
      }
    },
    [key],
  );
  return [tags, setTags] as const;
}
