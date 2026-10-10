import { ThumbsDownIcon, ThumbsUpIcon } from "lucide-react";
import { Button } from "@/components/ui/button";

export type FeedbackReplyVote = "up" | "down" | null;
/** Rate the usefulness of a review reply independently from accepting its proposal. */
export function FeedbackReplyRating({
  value,
  onChange,
}: {
  value: FeedbackReplyVote;
  onChange: (value: FeedbackReplyVote) => void;
}) {
  return (
    <div className="flex items-center gap-1" aria-label="Rate feedback reply">
      <Button
        type="button"
        size="icon"
        variant="ghost"
        aria-label="Thumbs up feedback reply"
        aria-pressed={value === "up"}
        onClick={() => onChange(value === "up" ? null : "up")}
      >
        <ThumbsUpIcon />
      </Button>
      <Button
        type="button"
        size="icon"
        variant="ghost"
        aria-label="Thumbs down feedback reply"
        aria-pressed={value === "down"}
        onClick={() => onChange(value === "down" ? null : "down")}
      >
        <ThumbsDownIcon />
      </Button>
      {value && (
        <span className="text-xs text-muted-foreground" role="status">
          {value === "up" ? "Helpful" : "Not helpful"}
        </span>
      )}
    </div>
  );
}
