import { useId, useState } from "react";
import { ChevronDownIcon, MessageSquareIcon, ScanSearchIcon, XIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";

export type DiscussionVariant = "footer" | "quick-prompts" | "inline";
export type FeedbackDiscussionIntent = "discuss" | "suggest" | "inspect";

export function FeedbackDiscussionComposerContext({
  intent,
  onDismiss,
  seconds,
  onStop,
}: {
  intent: FeedbackDiscussionIntent;
  onDismiss: () => void;
  seconds?: number | null;
  onStop?: () => void;
}) {
  const label = {
    discuss: "Feedback discussion",
    suggest: "Review feedback",
    inspect: "Self Reflection",
  }[intent];
  return (
    <div className="flex items-center justify-between px-3 pt-2 text-xs text-muted-foreground">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <span>{label} · original answer</span>
        {seconds != null && (
          <>
            <span role="status">Sending in {seconds}s · Edit to stop</span>
            <Button type="button" size="sm" variant="ghost" onClick={onStop}>
              Stop auto-send
            </Button>
          </>
        )}
      </div>
      <Button
        type="button"
        size="icon-xs"
        variant="ghost"
        aria-label="Continue without answer context"
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
  busy,
  saving,
  onPrepare,
  onSubmitInline,
}: {
  variant: DiscussionVariant;
  busy: boolean;
  saving: boolean;
  onPrepare: (intent: FeedbackDiscussionIntent) => void;
  onSubmitInline: (text: string) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const [text, setText] = useState("");
  const panelId = useId();
  const inputId = useId();
  const disabled = busy || saving;
  function prepare(intent: FeedbackDiscussionIntent) {
    onPrepare(intent);
    setExpanded(false);
  }
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="px-0 text-muted-foreground"
          disabled={disabled}
          onClick={() => prepare("discuss")}
        >
          <MessageSquareIcon className="size-3.5" />
          Feedback discussion
        </Button>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="px-0 text-muted-foreground"
          disabled={busy}
          onClick={() => prepare("inspect")}
        >
          <ScanSearchIcon className="size-3.5" />
          Self Reflection
        </Button>
        {variant !== "footer" && (
          <Button
            type="button"
            size="icon-xs"
            variant="ghost"
            aria-label="More feedback options"
            aria-expanded={expanded}
            aria-controls={panelId}
            disabled={disabled}
            onClick={() => setExpanded((current) => !current)}
          >
            <ChevronDownIcon className="size-3.5" />
          </Button>
        )}
      </div>
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
            Review feedback
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
            What would you like to discuss?
          </label>
          <Textarea
            id={inputId}
            rows={2}
            maxLength={4000}
            value={text}
            disabled={disabled}
            onChange={(event) => setText(event.target.value)}
            placeholder="Does my feedback explain what still needs verification?"
          />
          <div className="flex flex-wrap gap-2">
            <Button size="sm" type="submit" disabled={disabled || !text.trim()}>
              Send to chat
            </Button>
            <Button
              size="sm"
              type="button"
              variant="outline"
              disabled={disabled}
              onClick={() => prepare("suggest")}
            >
              Review feedback
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
