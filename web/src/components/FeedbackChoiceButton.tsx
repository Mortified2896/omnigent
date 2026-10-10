import type { TaskOutcome } from "@/hooks/useTaskExperiment";
import type { ComponentProps } from "react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

/** Shared rating/tag buttons for the answer editor and suggested corrections. */
export function FeedbackChoiceButton({
  kind,
  selected,
  className,
  ...props
}: ComponentProps<typeof Button> & { kind: "outcome" | "tag"; selected: boolean }) {
  return (
    <Button
      type="button"
      size="sm"
      variant={selected ? "secondary" : kind === "tag" ? "outline" : "ghost"}
      aria-pressed={selected}
      className={cn(
        kind === "outcome"
          ? "min-h-10 text-xs md:min-h-7"
          : "min-h-10 px-2 text-xs md:h-7 md:min-h-7 md:text-[11px]",
        className,
      )}
      {...props}
    />
  );
}

const COMPACT_TAG_LABELS: Record<string, string> = {
  "AGENTS instructions": "Instructions",
  "Task specification": "Task spec",
  "Environment/dependency": "Environment",
};
export function feedbackTagLabel(tag: string) {
  return COMPACT_TAG_LABELS[tag] ?? tag;
}

export const FEEDBACK_OUTCOMES: { value: TaskOutcome; label: string; definition: string }[] = [
  {
    value: "success",
    label: "Success",
    definition:
      "The requested task was accomplished on this attempt without a material correction or retry.",
  },
  {
    value: "partial",
    label: "Partial",
    definition:
      "Meaningful correct progress was made, but a material follow-up, correction, or additional implementation is required.",
  },
  {
    value: "failed",
    label: "Failed",
    definition:
      "The attempt did not accomplish the task or make sufficient correct progress to count as partial.",
  },
  {
    value: "not_sure",
    label: "Not sure",
    definition:
      "The outcome cannot yet be judged reliably. You can revise this after verification.",
  },
];
