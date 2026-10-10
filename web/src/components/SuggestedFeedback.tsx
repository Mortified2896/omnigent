import { useEffect, useState } from "react";
import type { TaskOutcome } from "@/hooks/useTaskExperiment";
import { Button } from "@/components/ui/button";

interface FeedbackDetails {
  outcome?: TaskOutcome;
  comment: string;
  tags: string[];
}
/** Proposed edits only update the shared feedback form after explicit acceptance. */
export function SuggestedFeedback({
  id,
  original,
  proposed,
  onApply,
}: {
  id?: string;
  original: Omit<FeedbackDetails, "outcome"> & { outcome: string };
  proposed: FeedbackDetails;
  onApply: (details: FeedbackDetails) => void | Promise<void>;
}) {
  const key = id ? `omnigent.feedback-proposal:${id}` : null;
  const [stored] = useState(() => {
    try {
      return key
        ? (JSON.parse(localStorage.getItem(key) ?? "null") as {
            state?: string;
            previous?: FeedbackDetails;
            comment?: string;
            tags?: string[];
            outcome?: TaskOutcome;
          } | null)
        : null;
    } catch {
      return null;
    }
  });
  const [previous, setPrevious] = useState<FeedbackDetails>(
    stored?.previous ?? { ...original, outcome: original.outcome as TaskOutcome },
  );
  const [state, setState] = useState<"pending" | "accepted" | "rejected">(
    stored?.state === "accepted" || stored?.state === "rejected" ? stored.state : "pending",
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [comment, setComment] = useState(stored?.comment ?? proposed.comment);
  const [proposedTags, setProposedTags] = useState(stored?.tags ?? proposed.tags);
  const [outcome, setOutcome] = useState<TaskOutcome>(
    stored?.outcome ?? proposed.outcome ?? (original.outcome as TaskOutcome),
  );
  const [newTag, setNewTag] = useState("");
  useEffect(() => {
    try {
      if (key)
        localStorage.setItem(
          key,
          JSON.stringify({ state, previous, comment, tags: proposedTags, outcome }),
        );
    } catch {
      /* Keep edits in memory when storage is unavailable. */
    }
  }, [key, state, previous, comment, proposedTags, outcome]);
  const changeState = (next: "pending" | "accepted" | "rejected", before = previous) => {
    setState(next);
    try {
      if (key)
        localStorage.setItem(
          key,
          JSON.stringify({ state: next, previous: before, comment, tags: proposedTags, outcome }),
        );
    } catch {
      /* Keep the current review state in memory. */
    }
  };
  const apply = async (details: FeedbackDetails, next: "pending" | "accepted") => {
    setBusy(true);
    setError(null);
    const before = {
      outcome: original.outcome as TaskOutcome,
      comment: original.comment,
      tags: [...original.tags],
    };
    try {
      await onApply(details);
      if (next === "accepted") setPrevious(before);
      changeState(next, next === "accepted" ? before : previous);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Changes were not saved");
    } finally {
      setBusy(false);
    }
  };
  return (
    <details
      open
      aria-label="Suggested feedback"
      className="mt-3 space-y-3 rounded-lg border bg-muted/20 p-3"
    >
      <summary className="cursor-pointer text-sm font-medium">Review suggested changes</summary>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-sm font-medium">Review feedback changes</p>
        <span className="text-xs text-muted-foreground">
          Outcome: {original.outcome.replaceAll("_", " ")}
        </span>
      </div>
      <p className="text-xs text-muted-foreground">
        Accepting saves the proposed rating, tags, and comment. You can edit all three first.
      </p>
      <label className="block space-y-1 text-xs">
        <span>Proposed rating · Edit before accepting</span>
        <select
          aria-label="Suggested feedback rating"
          className="block rounded-md border bg-background p-2 text-sm"
          value={outcome}
          disabled={state !== "pending" || busy}
          onChange={(event) => setOutcome(event.target.value as TaskOutcome)}
        >
          <option value="success">Success</option>
          <option value="partial">Partial</option>
          <option value="failed">Failed</option>
          <option value="not_sure">Not sure</option>
        </select>
      </label>
      <div className="flex flex-wrap gap-1" aria-label="Proposed feedback tags">
        {original.tags
          .filter((tag) => !proposedTags.includes(tag))
          .map((tag) => (
            <del
              key={tag}
              className="rounded border border-red-500/30 bg-red-500/10 px-2 py-1 text-xs"
            >
              {tag}
            </del>
          ))}
        {proposedTags.map((tag) => (
          <span
            key={tag}
            className={`rounded border px-2 py-1 text-xs ${original.tags.includes(tag) ? "" : "border-brand-accent/40 bg-brand-accent/10 text-brand-accent"}`}
            title={original.tags.includes(tag) ? "Unchanged tag" : "Added tag"}
          >
            {tag}
            {state === "pending" && (
              <button
                type="button"
                disabled={busy}
                className="ml-2"
                aria-label={`Remove suggested tag ${tag}`}
                onClick={() => setProposedTags((tags) => tags.filter((value) => value !== tag))}
              >
                ×
              </button>
            )}
          </span>
        ))}
      </div>
      {state === "pending" && (
        <div className="flex gap-2">
          <input
            aria-label="Suggested feedback tag"
            className="min-w-0 flex-1 rounded-md border bg-background p-2 text-sm"
            maxLength={64}
            placeholder="Add a tag…"
            value={newTag}
            disabled={busy || proposedTags.length >= 8}
            onChange={(event) => setNewTag(event.target.value)}
          />
          <Button
            type="button"
            size="sm"
            variant="outline"
            disabled={busy || proposedTags.length >= 8 || !newTag.trim()}
            onClick={() => {
              const tag = newTag.trim();
              if (!proposedTags.some((value) => value.toLowerCase() === tag.toLowerCase()))
                setProposedTags((tags) => [...tags, tag]);
              setNewTag("");
            }}
          >
            Add proposed tag
          </Button>
        </div>
      )}
      {state === "pending" && original.comment && (
        <del
          className="block rounded-md bg-red-500/10 p-2 text-sm"
          aria-label="Original feedback comment"
        >
          {original.comment}
        </del>
      )}
      <label className="block space-y-1 text-xs">
        <span>
          {state === "pending" ? "Proposed comment · Edit before accepting" : "Suggested comment"}
        </span>
        <textarea
          aria-label="Suggested feedback comment"
          className="w-full rounded-md border border-brand-accent/40 bg-brand-accent/10 p-2 text-sm"
          rows={4}
          maxLength={4000}
          value={comment}
          disabled={state !== "pending" || busy}
          onChange={(event) => setComment(event.target.value)}
        />
      </label>
      <div className="flex flex-wrap gap-2">
        {state === "pending" ? (
          <>
            <Button
              size="sm"
              disabled={busy}
              onClick={() =>
                void apply({ outcome, comment: comment.trim(), tags: proposedTags }, "accepted")
              }
            >
              Accept changes
            </Button>
            <Button
              size="sm"
              variant="outline"
              disabled={busy}
              onClick={() => changeState("rejected")}
            >
              Reject
            </Button>
          </>
        ) : (
          <Button
            size="sm"
            variant="outline"
            disabled={busy}
            onClick={() => {
              if (state === "accepted") void apply(previous, "pending");
              else changeState("pending");
            }}
          >
            {state === "accepted" ? "Undo" : "Review again"}
          </Button>
        )}
      </div>
      {error && (
        <p role="alert" className="text-xs text-destructive">
          {error}
        </p>
      )}
      <p className="text-xs text-muted-foreground" role="status">
        {busy
          ? "Saving changes…"
          : state === "accepted"
            ? "Changes saved to your feedback form."
            : state === "rejected"
              ? "Suggestion rejected. Your saved feedback is unchanged."
              : "Suggested changes · Not applied"}
      </p>
    </details>
  );
}
