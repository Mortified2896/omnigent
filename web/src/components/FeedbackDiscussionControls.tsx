import { useId, useState } from "react";
import { ChevronDownIcon, MessageSquareIcon, XIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";

export type DiscussionVariant = "footer" | "quick-prompts" | "inline";

export function FeedbackDiscussionComposerContext({ onDismiss }: { onDismiss: () => void }) {
  return (
    <div className="flex items-center justify-between px-3 pt-2 text-xs text-muted-foreground">
      <span>Feedback discussion · original answer</span>
      <Button
        type="button"
        size="icon-xs"
        variant="ghost"
        aria-label="Continue without feedback context"
        onClick={onDismiss}
      >
        <XIcon className="size-3" />
      </Button>
    </div>
  );
}

/** Proposed ongoing-chat controls, shared by the inspection stories and future integration. */
export function FeedbackDiscussionControls({
  variant,
  disabled,
  saving,
  onPrepare,
  onSubmitInline,
}: {
  variant: DiscussionVariant;
  disabled: boolean;
  saving: boolean;
  onPrepare: (intent: "discuss" | "suggest") => void;
  onSubmitInline: (text: string) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const [text, setText] = useState("");
  const panelId = useId();
  const inputId = useId();
  function prepare(intent: "discuss" | "suggest") {
    onPrepare(intent);
    setExpanded(false);
  }
  return (
    <div className="space-y-2">
      <Button
        type="button"
        variant="ghost"
        size="sm"
        className="px-0 text-muted-foreground"
        disabled={disabled}
        aria-expanded={variant === "footer" ? undefined : expanded}
        aria-controls={variant === "footer" ? undefined : panelId}
        onClick={() =>
          variant === "footer" ? prepare("suggest") : setExpanded((current) => !current)
        }
      >
        <MessageSquareIcon className="size-3.5" />
        Feedback discussion
        {variant === "quick-prompts" && <ChevronDownIcon className="size-3.5" />}
      </Button>
      {expanded && variant === "quick-prompts" && (
        <div id={panelId} className="flex flex-wrap gap-2">
          <Button
            type="button"
            size="sm"
            variant="outline"
            disabled={disabled}
            onClick={() => prepare("discuss")}
          >
            Discuss my feedback
          </Button>
          <Button
            type="button"
            size="sm"
            variant="outline"
            disabled={disabled}
            onClick={() => prepare("suggest")}
          >
            Suggest changes
          </Button>
        </div>
      )}
      {expanded && variant === "inline" && (
        <form
          id={panelId}
          className="space-y-2 rounded-lg border p-3"
          onSubmit={(event) => {
            event.preventDefault();
            if (disabled || !text.trim()) return;
            onSubmitInline(text.trim());
            setText("");
            setExpanded(false);
          }}
        >
          <label htmlFor={inputId} className="text-xs text-muted-foreground">
            What would you like to change?
          </label>
          <Textarea
            id={inputId}
            rows={2}
            maxLength={4000}
            value={text}
            disabled={disabled}
            onChange={(event) => setText(event.target.value)}
            placeholder="My comment should mention that local checks passed…"
          />
          <div className="flex flex-wrap gap-2">
            <Button size="sm" type="submit" disabled={disabled || !text.trim()}>
              Discuss change
            </Button>
            <Button
              size="sm"
              type="button"
              variant="outline"
              disabled={disabled}
              onClick={() => prepare("suggest")}
            >
              Suggest changes
            </Button>
          </div>
        </form>
      )}
      {saving && (
        <p className="text-xs text-muted-foreground">Waiting for your feedback to save…</p>
      )}
    </div>
  );
}
