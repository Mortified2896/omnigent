import type { TaskOutcome } from "@/hooks/useTaskExperiment";

export function parseFeedbackProposal(
  text: string,
): { comment: string; tags: string[]; outcome?: TaskOutcome } | null {
  const match = text.match(/```feedback-json\s*([\s\S]*?)```/);
  if (!match) return null;
  try {
    const value = JSON.parse(match[1]);
    if (
      !value ||
      (value.outcome !== undefined &&
        !["success", "partial", "failed", "not_sure"].includes(value.outcome)) ||
      typeof value.comment !== "string" ||
      value.comment.length > 4000 ||
      !Array.isArray(value.tags) ||
      value.tags.length > 8 ||
      value.tags.some((tag: unknown) => typeof tag !== "string" || !tag.trim() || tag.length > 64)
    )
      return null;
    return {
      ...(value.outcome === undefined ? {} : { outcome: value.outcome as TaskOutcome }),
      comment: value.comment,
      tags: [...new Set<string>(value.tags.map((tag: string) => tag.trim()))],
    };
  } catch {
    return null;
  }
}
