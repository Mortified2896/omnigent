import { useMemo } from "react";
import { Badge } from "@/components/ui/badge";
import { diffFeedbackText, feedbackOutcomeLabel } from "@/lib/feedbackChanges";

interface PreviewDetails {
  outcome: string;
  comment: string;
  tags: string[];
}

const ADDED =
  "bg-success/10 text-foreground underline decoration-success decoration-2 underline-offset-2";
const REMOVED =
  "bg-destructive/10 text-muted-foreground line-through decoration-destructive decoration-2";

function MarkedText({ kind, text }: { kind: "unchanged" | "removed" | "added"; text: string }) {
  if (kind === "added")
    return (
      <ins className={ADDED} aria-label={`Added: ${text}`}>
        {text}
      </ins>
    );
  if (kind === "removed")
    return (
      <del className={REMOVED} aria-label={`Removed: ${text}`}>
        {text}
      </del>
    );
  return text;
}

/** Word-style change marks use the same values that explicit acceptance saves. */
export function SuggestedFeedbackPreview({
  original,
  current,
  trackChanges,
}: {
  original: PreviewDetails;
  current: PreviewDetails;
  trackChanges: boolean;
}) {
  const commentChanges = useMemo(() => {
    let offset = 0;
    return diffFeedbackText(original.comment, current.comment).map((change) => {
      const key = `${change.kind}:${offset}`;
      offset += change.text.length;
      return { ...change, key };
    });
  }, [original.comment, current.comment]);
  const tags = trackChanges
    ? [
        ...original.tags.map((tag) => ({
          tag,
          kind: current.tags.includes(tag) ? ("unchanged" as const) : ("removed" as const),
        })),
        ...current.tags
          .filter((tag) => !original.tags.includes(tag))
          .map((tag) => ({
            tag,
            kind: "added" as const,
          })),
      ]
    : current.tags.map((tag) => ({ tag, kind: "unchanged" as const }));
  const ratingChanged = trackChanges && current.outcome !== original.outcome;
  return (
    <div className="flex min-w-0 flex-col gap-2.5">
      <div className="flex flex-wrap items-center gap-3" aria-label="Proposed feedback rating">
        <span className="w-10 shrink-0 text-xs text-muted-foreground">Rating</span>
        {ratingChanged && (
          <MarkedText kind="removed" text={feedbackOutcomeLabel(original.outcome)} />
        )}
        {ratingChanged ? (
          <MarkedText kind="added" text={feedbackOutcomeLabel(current.outcome)} />
        ) : (
          <Badge variant="secondary">{feedbackOutcomeLabel(current.outcome)}</Badge>
        )}
      </div>
      {tags.length > 0 && (
        <div className="flex items-start gap-3">
          <span className="w-10 shrink-0 pt-1 text-xs text-muted-foreground">Tags</span>
          <div
            className="flex min-w-0 flex-wrap items-center gap-1.5"
            aria-label="Proposed feedback tags"
          >
            {tags.map(({ tag, kind }) =>
              kind === "unchanged" ? (
                <Badge
                  key={tag}
                  variant="outline"
                  className="h-auto max-w-full whitespace-normal break-words"
                >
                  {tag}
                </Badge>
              ) : (
                <span key={`${kind}:${tag}`} className="min-w-0 max-w-full break-words text-sm">
                  <MarkedText kind={kind} text={tag} />
                </span>
              ),
            )}
          </div>
        </div>
      )}
      <p
        className="whitespace-pre-wrap break-words text-ui leading-relaxed [&>del+ins]:ms-1 [&>ins+del]:ms-1"
        aria-label="Suggested feedback comment preview"
      >
        {trackChanges
          ? commentChanges.length
            ? commentChanges.map(({ key, ...change }) => <MarkedText key={key} {...change} />)
            : "No comment"
          : current.comment || "No comment"}
      </p>
    </div>
  );
}
