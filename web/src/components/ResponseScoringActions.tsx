import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import {
  useSaveScoringEligibility,
  useSessionScoringPolicy,
  type ExclusionReason,
} from "@/hooks/useScoringEligibility";

const REASONS: { value: ExclusionReason; label: string }[] = [
  { value: "test_fixture", label: "Test / fixture" },
  { value: "duplicate", label: "Duplicate" },
  { value: "out_of_scope", label: "Out of scope" },
  { value: "other", label: "Other" },
];

/** Kept outside the outcome mutation so exclusion cannot overwrite human notes. */
export function ResponseScoringActions({
  sessionId,
  responseId,
  compact = false,
}: {
  sessionId: string;
  responseId: string;
  compact?: boolean;
}) {
  const policy = useSessionScoringPolicy(sessionId);
  const mutation = useSaveScoringEligibility(sessionId, responseId);
  if (policy.isError) {
    return (
      <div className="flex flex-wrap items-center gap-2 text-xs" role="alert">
        Scoring settings could not be loaded.
        <Button type="button" variant="ghost" onClick={() => void policy.refetch()}>
          Retry scoring settings
        </Button>
      </div>
    );
  }
  if (!policy.isSuccess) {
    return <span className="text-xs text-muted-foreground">Loading scoring settings…</span>;
  }
  const sessionExcluded = policy.data.score_eligible === false;
  const saved = policy.data.responses?.[responseId];
  const excluded = sessionExcluded || saved?.score_eligible === false;
  const reason = saved?.exclusion_reason ?? "";

  return (
    <div className="flex flex-col gap-1" aria-label="Scoring eligibility">
      <div className="flex flex-wrap items-center gap-2">
        {compact ? (
          <label className="flex min-h-10 cursor-pointer items-center gap-2 pl-[11px] text-xs text-muted-foreground md:min-h-7">
            <Switch
              className="data-[state=checked]:bg-primary data-[state=unchecked]:bg-input [&_[data-slot=switch-thumb]]:data-[state=checked]:translate-x-[calc(100%-2px)] [&_[data-slot=switch-thumb]]:data-[state=unchecked]:translate-x-0"
              aria-label="Do not score"
              checked={excluded}
              disabled={sessionExcluded || mutation.isPending}
              title="Exclude this response from scoring without changing its outcome or notes"
              onCheckedChange={(checked) =>
                mutation.mutate({ score_eligible: !checked, exclusion_reason: null })
              }
            />
            <span>Do not score</span>
          </label>
        ) : (
          <Button
            type="button"
            size="sm"
            variant={excluded ? "secondary" : "ghost"}
            className="min-h-10 text-xs md:min-h-7"
            aria-pressed={excluded}
            disabled={sessionExcluded || mutation.isPending}
            title="Exclude this response from scoring without changing its outcome or notes"
            onClick={() => mutation.mutate({ score_eligible: excluded, exclusion_reason: null })}
          >
            Do not score
          </Button>
        )}
        {excluded && !sessionExcluded && (
          <label className="flex min-w-0 flex-wrap items-center gap-2 text-xs">
            Reason (optional)
            <select
              aria-label="Scoring exclusion reason"
              className="min-h-10 max-w-full rounded-md border bg-background px-2 text-sm"
              value={reason}
              disabled={mutation.isPending}
              onChange={(event) =>
                mutation.mutate({
                  score_eligible: false,
                  exclusion_reason: (event.target.value || null) as ExclusionReason | null,
                })
              }
            >
              <option value="">No reason selected</option>
              {REASONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
        )}
        {excluded && <span className="text-xs text-muted-foreground">Excluded from scoring</span>}
      </div>
      {policy.data.is_test && (
        <span className="text-xs font-medium" role="status">
          {policy.data.retention === "keep_for_inspection"
            ? "Test evidence · Kept for inspection"
            : "Test session · Excluded from scoring"}
        </span>
      )}
      {sessionExcluded && !policy.data.is_test && (
        <span className="text-xs text-muted-foreground">The whole session is excluded.</span>
      )}
      {mutation.isError && (
        <span className="text-xs text-destructive" role="alert">
          Scoring setting was not saved. Your previous setting is unchanged. Please try again.
        </span>
      )}
    </div>
  );
}
