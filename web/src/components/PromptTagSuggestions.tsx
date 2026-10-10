import { useState } from "react";
import { CheckIcon, PencilIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { TaskTagsControls } from "./composer/TaskTagsPicker";
import { SuggestedFeedbackPreview } from "./SuggestedFeedbackPreview";
import { reviewedFeedback, reviewChangeKeys, type ChangeDecisions } from "@/lib/feedbackReview";
import { authenticatedFetch } from "@/lib/identity";

const NO_TAGS: string[] = [];

/** Advisor labels remain proposals until the user explicitly applies a reviewed set. */
export function PromptTagSuggestions({
  original,
  proposed,
  onApply,
}: {
  original: string[];
  proposed: string[];
  onApply: (tags: string[]) => Promise<void>;
}) {
  const [draft, setDraft] = useState([...new Set([...original, ...proposed])].slice(0, 8));
  const [decisions, setDecisions] = useState<ChangeDecisions>({});
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const before = { outcome: "not_sure", comment: "", tags: original };
  const after = { ...before, tags: draft };
  const resolved = reviewedFeedback(before, after, decisions);
  const pending = reviewChangeKeys(before, after).filter((key) => !decisions[key]).length;
  async function apply(tags: string[]) {
    setBusy(true);
    setError(null);
    try {
      await onApply(tags);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Tags could not be saved.");
    } finally {
      setBusy(false);
    }
  }
  return (
    <section
      aria-label="Suggested prompt tags"
      className="mt-2 min-w-0 space-y-2 text-left md:rounded-lg md:border md:p-3"
    >
      <p className="text-xs font-medium">Advisor suggested prompt tags</p>
      {editing ? (
        <TaskTagsControls
          value={draft}
          onChange={(tags) => {
            setDraft(tags);
            setDecisions({});
          }}
        />
      ) : (
        <SuggestedFeedbackPreview
          original={before}
          current={after}
          trackChanges
          tagsOnly
          decisions={decisions}
          onDecide={
            busy
              ? undefined
              : (key, value) => setDecisions((previous) => ({ ...previous, [key]: value }))
          }
        />
      )}
      <div className="flex flex-wrap items-center gap-2">
        <Button type="button" size="sm" disabled={busy} onClick={() => void apply(resolved.tags)}>
          <CheckIcon data-icon="inline-start" />
          {busy
            ? "Saving…"
            : Object.keys(decisions).length
              ? pending
                ? "Accept remaining & apply"
                : "Apply reviewed tags"
              : "Accept tags"}
        </Button>
        <Button
          type="button"
          size="sm"
          variant="ghost"
          disabled={busy}
          onClick={() => setEditing((value) => !value)}
        >
          <PencilIcon data-icon="inline-start" />
          {editing ? "Review changes" : "Edit"}
        </Button>
        <Button
          type="button"
          size="sm"
          variant="ghost"
          disabled={busy}
          aria-label="Reject suggested prompt tags"
          onClick={() => void apply(original)}
        >
          Reject
        </Button>
      </div>
      <p role="status" className="text-xs text-muted-foreground">
        {error ?? "Apply tags to save them on this prompt."}
      </p>
    </section>
  );
}

export function MessageTaskTags({
  sessionId,
  itemId,
  tags = NO_TAGS,
  suggestions = NO_TAGS,
}: {
  sessionId: string | null;
  itemId: string;
  tags?: string[];
  suggestions?: string[];
}) {
  const [saved, setSaved] = useState<string[] | null>(null);
  const [reviewed, setReviewed] = useState(false);
  const current = saved ?? tags;
  return (
    <>
      {current.length > 0 && (
        <div
          className="mt-1 flex max-w-full flex-wrap justify-end gap-1"
          aria-label="Message task tags"
        >
          {current.map((tag) => (
            <span
              key={tag}
              className="rounded border border-primary/50 bg-primary/10 px-2 py-0.5 text-xs"
            >
              {tag}
            </span>
          ))}
        </div>
      )}
      {sessionId && suggestions.length > 0 && !reviewed && (
        <PromptTagSuggestions
          original={current}
          proposed={suggestions}
          onApply={async (value) => {
            const response = await authenticatedFetch(
              `/v1/sessions/${encodeURIComponent(sessionId)}/items/${encodeURIComponent(itemId)}/task-tags`,
              {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ expected_tags: current, tags: value }),
              },
            );
            if (!response.ok)
              throw new Error(
                response.status === 409
                  ? "Tags changed in another view. Reload to review the latest tags."
                  : "Tags were not saved. Please try again.",
              );
            const item = await response.json();
            setSaved(item.task_tags ?? []);
            setReviewed(true);
          }}
        />
      )}
    </>
  );
}
