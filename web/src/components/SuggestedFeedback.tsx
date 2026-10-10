import { useEffect, useId, useState } from "react";
import type { TaskOutcome } from "@/hooks/useTaskExperiment";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { SuggestedFeedbackPreview } from "./SuggestedFeedbackPreview";
import { feedbackOutcomeLabel as outcomeLabel } from "@/lib/feedbackChanges";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field";
import {
  InputGroup,
  InputGroupInput,
  InputGroupAddon,
  InputGroupButton,
} from "@/components/ui/input-group";
import { Textarea } from "@/components/ui/textarea";
import { Separator } from "@/components/ui/separator";
import {
  Select,
  SelectTrigger,
  SelectValue,
  SelectContent,
  SelectGroup,
  SelectItem,
} from "@/components/ui/select";
import { CheckIcon, ChevronDownIcon, PencilIcon, PlusIcon, XIcon } from "lucide-react";

import { reviewedFeedback, reviewChangeKeys, type ChangeDecisions } from "@/lib/feedbackReview";

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
            decisions?: ChangeDecisions;
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
  const [decisions, setDecisions] = useState<ChangeDecisions>(stored?.decisions ?? {});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [comment, setComment] = useState(stored?.comment ?? proposed.comment);
  const [proposedTags, setProposedTags] = useState(stored?.tags ?? proposed.tags);
  const [outcome, setOutcome] = useState<TaskOutcome>(
    stored?.outcome ?? proposed.outcome ?? (original.outcome as TaskOutcome),
  );
  const [newTag, setNewTag] = useState("");
  const [showOriginal, setShowOriginal] = useState(false);
  const originalCommentId = useId();
  const ratingId = useId();
  const commentId = useId();
  const [editing, setEditing] = useState(false);
  const [previewMode, setPreviewMode] = useState<"changes" | "final">("changes");
  useEffect(() => {
    try {
      if (key)
        localStorage.setItem(
          key,
          JSON.stringify({ state, previous, comment, tags: proposedTags, outcome, decisions }),
        );
    } catch {
      /* Keep edits in memory when storage is unavailable. */
    }
  }, [key, state, previous, comment, proposedTags, outcome, decisions]);
  const changeState = (next: "pending" | "accepted" | "rejected", before = previous) => {
    setState(next);
    setEditing(false);
    try {
      if (key)
        localStorage.setItem(
          key,
          JSON.stringify({
            state: next,
            previous: before,
            comment,
            tags: proposedTags,
            outcome,
            decisions,
          }),
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
      else setDecisions({});
      changeState(next, next === "accepted" ? before : previous);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Changes were not saved");
    } finally {
      setBusy(false);
    }
  };
  const reviewBase = { ...previous, outcome: previous.outcome ?? original.outcome };
  const draft = { outcome, comment, tags: proposedTags };
  const resolved = reviewedFeedback(reviewBase, draft, decisions);
  const changeKeys = reviewChangeKeys(reviewBase, draft);
  const decidedCount = changeKeys.filter((changeKey) => decisions[changeKey]).length;
  const pendingCount = changeKeys.length - decidedCount;
  function addTag() {
    const tag = newTag.trim();
    if (!tag || proposedTags.length >= 8) return;
    if (!proposedTags.some((value) => value.toLowerCase() === tag.toLowerCase()))
      setProposedTags((tags) => [...tags, tag]);
    setNewTag("");
  }
  return (
    <section
      aria-label="Suggested feedback"
      className="mt-3 flex flex-col gap-3 md:rounded-lg md:border md:p-4"
    >
      <Separator className="md:hidden" />
      <Tabs
        value={previewMode}
        onValueChange={(value) => setPreviewMode(value === "final" ? "final" : "changes")}
        className="gap-3"
      >
        <div className="flex items-center justify-between gap-2">
          <h3 className="text-sm font-medium">Suggested feedback</h3>
          {!editing && (
            <TabsList
              variant="line"
              aria-label="Suggestion display"
              className="shrink-0 group-data-horizontal/tabs:h-10 md:group-data-horizontal/tabs:h-8"
            >
              <TabsTrigger value="changes">Changes</TabsTrigger>
              <TabsTrigger value="final">Final text</TabsTrigger>
            </TabsList>
          )}
        </div>
        {editing && state === "pending" ? (
          <FieldGroup className="gap-3">
            <Field orientation="horizontal">
              <FieldLabel htmlFor={ratingId}>Rating</FieldLabel>
              <Select
                value={outcome}
                disabled={busy}
                onValueChange={(value) => setOutcome(value as TaskOutcome)}
              >
                <SelectTrigger
                  id={ratingId}
                  aria-label="Suggested feedback rating"
                  className="ml-auto min-h-10 md:min-h-0"
                >
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectGroup>
                    {(["success", "partial", "failed", "not_sure"] as const).map((value) => (
                      <SelectItem key={value} value={value}>
                        {outcomeLabel(value)}
                      </SelectItem>
                    ))}
                  </SelectGroup>
                </SelectContent>
              </Select>
            </Field>
            <Field>
              <FieldLabel>Tags</FieldLabel>
              <div className="flex flex-wrap gap-1.5" aria-label="Proposed feedback tags">
                {proposedTags.map((tag) => (
                  <Button
                    key={tag}
                    type="button"
                    size="sm"
                    variant="secondary"
                    className="min-h-10 md:min-h-0"
                    disabled={busy}
                    aria-label={`Remove suggested tag ${tag}`}
                    onClick={() => setProposedTags((tags) => tags.filter((value) => value !== tag))}
                  >
                    {tag}
                    <XIcon data-icon="inline-end" />
                  </Button>
                ))}
              </div>
              <InputGroup className="min-h-10">
                <InputGroupInput
                  aria-label="Suggested feedback tag"
                  value={newTag}
                  maxLength={64}
                  placeholder="Add a tag…"
                  disabled={busy || proposedTags.length >= 8}
                  onChange={(event) => setNewTag(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter") {
                      event.preventDefault();
                      addTag();
                    }
                  }}
                />
                <InputGroupAddon align="inline-end">
                  <InputGroupButton
                    aria-label="Add proposed tag"
                    disabled={busy || proposedTags.length >= 8 || !newTag.trim()}
                    onClick={addTag}
                  >
                    <PlusIcon data-icon="inline-start" />
                    Add
                  </InputGroupButton>
                </InputGroupAddon>
              </InputGroup>
            </Field>
            <Field>
              <FieldLabel htmlFor={commentId}>Comment</FieldLabel>
              <Textarea
                id={commentId}
                aria-label="Suggested feedback comment"
                rows={3}
                maxLength={4000}
                value={comment}
                disabled={busy}
                onChange={(event) => setComment(event.target.value)}
              />
            </Field>
          </FieldGroup>
        ) : (
          <>
            <TabsContent value="changes">
              <SuggestedFeedbackPreview
                original={reviewBase}
                current={state === "pending" ? draft : resolved}
                trackChanges={state === "pending"}
                decisions={decisions}
                onDecide={
                  state === "pending" && !busy
                    ? (changeKey, decision) =>
                        setDecisions((values) => ({ ...values, [changeKey]: decision }))
                    : undefined
                }
              />
            </TabsContent>
            <TabsContent value="final">
              <SuggestedFeedbackPreview
                original={reviewBase}
                current={state === "pending" ? draft : resolved}
                trackChanges={false}
                decisions={decisions}
              />
            </TabsContent>
          </>
        )}
      </Tabs>
      <div className="flex flex-wrap items-center gap-2">
        {state === "pending" ? (
          <>
            <Button
              size="sm"
              className="min-h-10 md:min-h-0"
              disabled={busy}
              onClick={() =>
                void apply({ ...resolved, outcome: resolved.outcome as TaskOutcome }, "accepted")
              }
            >
              <CheckIcon data-icon="inline-start" />
              {decidedCount
                ? pendingCount
                  ? "Accept remaining & save"
                  : "Save reviewed feedback"
                : "Accept changes"}
            </Button>
            <Button
              size="sm"
              variant="ghost"
              className="min-h-10 md:min-h-0"
              disabled={busy}
              onClick={() => {
                if (!editing) setDecisions({});
                setEditing((value) => !value);
              }}
              aria-label={editing ? "Done editing suggestion" : "Edit feedback suggestion"}
            >
              <PencilIcon data-icon="inline-start" />
              {editing ? "Done" : "Edit"}
            </Button>
            <Button
              size="sm"
              variant="ghost"
              className="min-h-10 md:min-h-0"
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
            className="min-h-10 md:min-h-0"
            disabled={busy}
            onClick={() => {
              if (state === "accepted") {
                void apply(previous, "pending");
              } else changeState("pending");
            }}
          >
            {state === "accepted" ? "Undo" : "Review again"}
          </Button>
        )}
      </div>
      {state === "pending" && original.comment && (editing || previewMode === "final") && (
        <div className="flex flex-col gap-2">
          <Button
            type="button"
            size="sm"
            variant="ghost"
            className="self-start"
            aria-expanded={showOriginal}
            aria-controls={originalCommentId}
            onClick={() => setShowOriginal((value) => !value)}
          >
            <ChevronDownIcon data-icon="inline-end" />
            {showOriginal ? "Hide original comment" : "Show original comment"}
          </Button>
          {showOriginal && (
            <p
              id={originalCommentId}
              className="text-sm text-muted-foreground"
              aria-label="Original feedback comment"
            >
              {original.comment}
            </p>
          )}
        </div>
      )}
      {error && (
        <Alert variant="destructive">
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}
      <p className="text-xs text-muted-foreground" role="status">
        {busy
          ? "Saving changes…"
          : state === "accepted"
            ? "Changes saved to your feedback form."
            : state === "rejected"
              ? "Suggestion rejected. Your saved feedback is unchanged."
              : decidedCount
                ? `${decidedCount} of ${changeKeys.length} changes reviewed · Not saved`
                : "Select a marked change to accept or reject it · Not saved"}
      </p>
    </section>
  );
}
