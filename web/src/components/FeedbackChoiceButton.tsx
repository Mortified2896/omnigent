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
