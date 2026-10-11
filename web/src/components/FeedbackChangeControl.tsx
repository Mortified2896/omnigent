import { useState, type ReactNode } from "react";
import { FeedbackChoiceButton } from "./FeedbackChoiceButton";
import { Button } from "@/components/ui/button";
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
    <span
      className="inline-flex max-w-full flex-wrap items-center gap-1 align-baseline"
      onKeyDown={(event) => {
        if (event.key === "Escape") setOpen(false);
      }}
    >
      {choice ? (
        <FeedbackChoiceButton
          onClick={() => setOpen(!open)}
          aria-expanded={open}
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
          onClick={() => setOpen(!open)}
          aria-expanded={open}
          aria-label={`Review ${label}${decision ? ` · ${decision}` : ""}`}
          className="inline cursor-pointer whitespace-pre-wrap rounded-sm text-left outline-none hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring [&>del+ins]:ms-1 [&>ins+del]:ms-1"
        >
          {children}
        </button>
      )}
      {open && (
        <span
          role="group"
          aria-label={`Actions for ${label}`}
          className="inline-flex shrink-0 items-center border-l border-border pl-1"
        >
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
        </span>
      )}
    </span>
  );
}
