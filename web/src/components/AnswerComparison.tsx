import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { authenticatedFetch, getCurrentUserId } from "@/lib/identity";
import { Button } from "@/components/ui/button";
import { MessageResponse } from "@/components/ai-elements/message";

type Choice = "a" | "b" | "tie" | "neither" | "skip";
export interface Comparison {
  state: "eligible" | "pending" | "ready" | "failed" | "ineligible";
  revision?: string;
  error?: string | null;
  vote?: { choice: Choice; preferred_arm: "human" | "jev" | null; comment: string } | null;
  candidates: {
    slot: "a" | "b";
    answer: string;
    arm?: "human" | "jev";
    model?: string;
    reasoning_effort?: string;
  }[];
}

async function comparisonFetch(url: string, method = "GET", body?: object) {
  const response = await authenticatedFetch(url, {
    method,
    ...(body
      ? { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }
      : {}),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => null);
    throw new Error(error?.detail ?? error?.error?.message ?? "Comparison could not be saved");
  }
  return response.json() as Promise<Comparison | null>;
}

export function AnswerComparison({
  sessionId,
  responseId,
  autoStart,
}: {
  sessionId: string;
  responseId: string;
  autoStart: boolean;
}) {
  const url = `/v1/sessions/${encodeURIComponent(sessionId)}/answer-comparisons/${encodeURIComponent(responseId)}`;
  const key = ["answer-comparison", getCurrentUserId(), sessionId, responseId];
  const client = useQueryClient();
  const query = useQuery({
    queryKey: key,
    queryFn: () => comparisonFetch(url),
    refetchInterval: (current) => (current.state.data?.state === "pending" ? 1500 : false),
    retry: false,
  });
  const [open, setOpen] = useState(true);
  const [commentDraft, setCommentDraft] = useState<string | null>(null);
  const comment = commentDraft ?? query.data?.vote?.comment ?? "";
  const attempted = useRef(false);
  const start = useMutation({
    mutationFn: () => comparisonFetch(url, "POST", {}),
    onSuccess: (saved) => client.setQueryData(key, saved),
  });
  const vote = useMutation({
    mutationFn: (choice: Choice) =>
      comparisonFetch(`${url}/preference`, "PUT", { choice, comment }),
    onSuccess: (saved) => client.setQueryData(key, saved),
  });
  const { mutate: startComparison } = start;
  useEffect(() => {
    if (autoStart && query.data?.state === "eligible" && !attempted.current) {
      attempted.current = true;
      startComparison();
    }
  }, [autoStart, query.data?.state, startComparison]);

  const data = query.data;
  if (!data || data.state === "ineligible") return null;
  const error = start.error ?? vote.error;
  const saved = data.vote;
  return (
    <section
      className="my-3 w-full min-w-0 rounded-xl border bg-muted/20 p-3"
      aria-label="Compare your choice and Jev's choice"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <p className="text-sm font-medium">Your choice and Jev disagree</p>
          <p className="text-xs text-muted-foreground">
            Which answer do you prefer? Models are revealed after your vote.
          </p>
        </div>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
        >
          {open ? "Collapse comparison" : "Show comparison"}
        </Button>
      </div>
      {(data.state === "pending" || start.isPending) && (
        <p className="mt-3 text-sm" role="status">
          Preparing the other answer… You can keep using this chat.
        </p>
      )}
      {data.state === "eligible" && !start.isPending && (
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="mt-3"
          onClick={() => start.mutate()}
        >
          Compare answers
        </Button>
      )}
      {data.state === "failed" && (
        <p className="mt-3 text-sm" role="status">
          {data.error} Your original answer is preserved.
        </p>
      )}
      {open && data.state === "ready" && (
        <>
          <div className="mt-3 grid min-w-0 gap-3 lg:grid-cols-2">
            {data.candidates.map((candidate) => (
              <article key={candidate.slot} className="min-w-0 rounded-lg border bg-background p-3">
                <p className="mb-2 text-sm font-medium">Answer {candidate.slot.toUpperCase()}</p>
                {candidate.model && (
                  <p className="mb-2 break-words text-xs text-muted-foreground">
                    {candidate.arm === "human" ? "Your choice" : "Jev's choice"} · {candidate.model}{" "}
                    · {candidate.reasoning_effort}
                  </p>
                )}
                <div className="max-h-[32rem] overflow-auto break-words text-sm">
                  <MessageResponse>{candidate.answer}</MessageResponse>
                </div>
                <Button
                  type="button"
                  className="mt-3"
                  variant={saved?.choice === candidate.slot ? "default" : "outline"}
                  size="sm"
                  disabled={vote.isPending}
                  onClick={() => vote.mutate(candidate.slot)}
                >
                  Prefer {candidate.slot.toUpperCase()}
                </Button>
              </article>
            ))}
          </div>
          <label className="mt-3 block text-xs text-muted-foreground">
            Why? (optional)
            <textarea
              className="mt-1 block min-h-16 w-full rounded-md border bg-background p-2 text-sm"
              maxLength={4000}
              value={comment}
              onChange={(event) => setCommentDraft(event.target.value)}
              placeholder="For example: clearer explanation or better reasoning"
            />
          </label>
          <div className="mt-2 flex flex-wrap gap-2">
            {(
              [
                ["tie", "About equal"],
                ["neither", "Neither"],
                ["skip", "Skip"],
              ] as const
            ).map(([choice, label]) => (
              <Button
                key={choice}
                type="button"
                variant={saved?.choice === choice ? "secondary" : "ghost"}
                size="sm"
                disabled={vote.isPending}
                onClick={() => vote.mutate(choice)}
              >
                {label}
              </Button>
            ))}
          </div>
          {saved && comment !== saved.comment && (
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="mt-2"
              disabled={vote.isPending}
              onClick={() => vote.mutate(saved.choice)}
            >
              Save comment
            </Button>
          )}
          <p className="mt-2 text-xs text-muted-foreground" role="status">
            {vote.isPending
              ? "Saving preference…"
              : saved && comment !== saved.comment
                ? "Comment has unsaved changes."
                : saved
                  ? `Saved to server${saved.preferred_arm ? ` · You preferred ${saved.preferred_arm === "human" ? "your choice" : "Jev's choice"}` : ""}.`
                  : "Your vote helps evaluate Jev's recommendations."}
          </p>
        </>
      )}
      {error && (
        <p className="mt-2 text-sm text-destructive" role="alert">
          {error.message} Try again.
        </p>
      )}
    </section>
  );
}
