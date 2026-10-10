import { useState, type ReactNode } from "react";
import { FeedbackChoiceButton } from "./FeedbackChoiceButton";
import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { CheckIcon, XIcon } from "lucide-react";
import type { ChangeDecision } from "@/lib/feedbackReview";

export function FeedbackChangeControl({
  label,
  decision,
  onDecide,
  children,
  choice,
}: {
  label: string;
  decision?: ChangeDecision;
  onDecide: (decision: ChangeDecision) => void;
  children: ReactNode;
  choice?: { kind: "outcome" | "tag"; selected: boolean };
}) {
  const [open, setOpen] = useState(false);
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        {choice ? (
          <FeedbackChoiceButton
            kind={choice.kind}
            selected={choice.selected}
            aria-label={`Review ${label}${decision ? ` · ${decision}` : ""}`}
            className="h-auto max-w-full whitespace-normal break-words text-left"
          >
            {children}
          </FeedbackChoiceButton>
        ) : (
          <button
            type="button"
            aria-label={`Review ${label}${decision ? ` · ${decision}` : ""}`}
            className="inline cursor-pointer whitespace-pre-wrap rounded-sm text-left outline-none hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring [&>del+ins]:ms-1 [&>ins+del]:ms-1"
          >
            {children}
          </button>
        )}
      </PopoverTrigger>
      <PopoverContent
        className="w-auto max-w-[calc(100vw-24px)] gap-0 p-1"
        align="start"
        aria-label={`Review ${label}${decision ? ` · ${decision}` : ""}`}
      >
        <div className="flex gap-0">
          <Button
            size="xs"
            variant="ghost"
            className="[@media(pointer:coarse)]:min-h-10"
            onClick={() => {
              onDecide("accepted");
              setOpen(false);
            }}
            aria-label={`Accept ${label}`}
          >
            <CheckIcon data-icon="inline-start" />
            Accept
          </Button>
          <Button
            size="xs"
            variant="ghost"
            className="[@media(pointer:coarse)]:min-h-10"
            onClick={() => {
              onDecide("rejected");
              setOpen(false);
            }}
            aria-label={`Reject ${label}`}
          >
            <XIcon data-icon="inline-start" />
            Reject
          </Button>
        </div>
      </PopoverContent>
    </Popover>
  );
}
