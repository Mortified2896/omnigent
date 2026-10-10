import { useState, type ReactNode } from "react";
import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { CheckIcon, XIcon } from "lucide-react";
import type { ChangeDecision } from "@/lib/feedbackReview";

export function FeedbackChangeControl({
  label,
  decision,
  onDecide,
  children,
}: {
  label: string;
  decision?: ChangeDecision;
  onDecide: (decision: ChangeDecision) => void;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          aria-label={`Review ${label}${decision ? ` · ${decision}` : ""}`}
          className="inline cursor-pointer whitespace-pre-wrap rounded-sm text-left outline-none hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring [&>del+ins]:ms-1 [&>ins+del]:ms-1"
        >
          {children}
        </button>
      </PopoverTrigger>
      <PopoverContent className="w-auto max-w-[calc(100vw-24px)]" align="start">
        <p className="text-xs text-muted-foreground">
          {label}
          {decision ? ` · ${decision}` : ""}
        </p>
        <div className="flex gap-2">
          <Button
            size="sm"
            className="min-h-10"
            onClick={() => {
              onDecide("accepted");
              setOpen(false);
            }}
            aria-label={`Accept ${label}`}
          >
            <CheckIcon />
            Accept
          </Button>
          <Button
            size="sm"
            variant="outline"
            className="min-h-10"
            onClick={() => {
              onDecide("rejected");
              setOpen(false);
            }}
            aria-label={`Reject ${label}`}
          >
            <XIcon />
            Reject
          </Button>
        </div>
      </PopoverContent>
    </Popover>
  );
}
