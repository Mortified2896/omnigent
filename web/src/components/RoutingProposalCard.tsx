import { useEffect, useMemo, useState } from "react";
import {
  ChevronDownIcon,
  ChevronUpIcon,
  ClockIcon,
  ListTreeIcon,
  Loader2Icon,
  PlayIcon,
  ShieldCheckIcon,
  SlidersHorizontalIcon,
  TriangleAlertIcon,
  XIcon,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import type {
  O3BenchmarkSlice,
  O3ProposalAdjustment,
  O3ProposalDecision,
  O3RoutingProposal,
} from "@/lib/o3RoutingReview";
import { cn } from "@/lib/utils";

interface RoutingProposalCardProps {
  proposal: O3RoutingProposal;
  slices: O3BenchmarkSlice[];
  onAdjust: (adjustment: O3ProposalAdjustment) => Promise<O3RoutingProposal>;
  onDecision: (decision: O3ProposalDecision) => Promise<O3RoutingProposal>;
  onProposalChange: (proposal: O3RoutingProposal) => void;
  onApproved: (proposal: O3RoutingProposal) => Promise<void>;
  onReset: () => void;
}

const fieldClass =
  "h-9 rounded-md border border-input bg-background px-2.5 text-sm text-foreground outline-none focus-visible:border-ring";

function percent(value: number | null): string {
  return value === null ? "Unknown" : `${Math.round(value * 100)}%`;
}

function titleCase(value: string): string {
  return value
    .split("_")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function evidenceSummary(proposal: O3RoutingProposal): string {
  if (proposal.frontier.passing_exact_candidates.length > 0) return "Exact evidence";
  if (proposal.frontier.provisional_candidates.length > 0) return "Provisional evidence";
  return "No qualifying evidence";
}

function costSummary(proposal: O3RoutingProposal): string {
  const best = proposal.evaluations.find(
    (item) => item.status === "pass" || item.status === "provisional",
  );
  if (!best) return "No eligible route";
  const cost = best.ranking?.estimated_monetary_cost_usd;
  const quota = best.ranking?.quota_remaining_percent;
  const costText =
    cost === 0 ? "free route" : cost == null ? "cost unknown" : `$${cost.toFixed(4)}`;
  return quota == null ? costText : `${costText}, ${Math.round(quota)}% quota left`;
}

export function RoutingProposalCard({
  proposal,
  slices,
  onAdjust,
  onDecision,
  onProposalChange,
  onApproved,
  onReset,
}: RoutingProposalCardProps) {
  const [expanded, setExpanded] = useState(false);
  const [adjusting, setAdjusting] = useState(false);
  const [reviewingSplit, setReviewingSplit] = useState(false);
  const [runAnyway, setRunAnyway] = useState(false);
  const [overrideReason, setOverrideReason] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const constraints = proposal.approved_constraints;
  const benchmark = constraints.benchmark;
  const [sliceKey, setSliceKey] = useState(
    `${benchmark.benchmark_id}|${benchmark.version}|${benchmark.slice_id}`,
  );
  const [minimumScore, setMinimumScore] = useState(String(benchmark.minimum_score));
  const [effort, setEffort] = useState(constraints.reasoning_effort);
  const [risk, setRisk] = useState(constraints.risk);
  const [evidencePolicy, setEvidencePolicy] = useState(constraints.evidence_policy);
  const [costPreference, setCostPreference] = useState(constraints.cost_quota_preference);

  useEffect(() => {
    setSliceKey(`${benchmark.benchmark_id}|${benchmark.version}|${benchmark.slice_id}`);
    setMinimumScore(String(benchmark.minimum_score));
    setEffort(constraints.reasoning_effort);
    setRisk(constraints.risk);
    setEvidencePolicy(constraints.evidence_policy);
    setCostPreference(constraints.cost_quota_preference);
  }, [
    benchmark.benchmark_id,
    benchmark.minimum_score,
    benchmark.slice_id,
    benchmark.version,
    constraints.cost_quota_preference,
    constraints.evidence_policy,
    constraints.reasoning_effort,
    constraints.risk,
  ]);

  const eligible = useMemo(
    () => proposal.evaluations.filter((item) => item.status !== "excluded"),
    [proposal.evaluations],
  );
  const best = eligible[0] ?? null;
  const terminal = proposal.decision === "decline" || proposal.decision === "defer";
  const approved = proposal.decision === "approve" || proposal.decision === "run_anyway";
  const canApprove = eligible.length > 0 && proposal.decision === null;
  const canResumeLaunch = approved && proposal.derived_combo_name !== null;
  const hasProvisional = eligible.some((item) => item.status === "provisional");

  async function decide(decision: O3ProposalDecision, launch: boolean): Promise<void> {
    setBusy(decision.action);
    setError(null);
    try {
      const updated = await onDecision(decision);
      onProposalChange(updated);
      if (launch) await onApproved(updated);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Routing decision failed");
    } finally {
      setBusy(null);
    }
  }

  async function saveAdjustment(): Promise<void> {
    const chosen = slices.find(
      (slice) => `${slice.benchmark_id}|${slice.version}|${slice.slice_id}` === sliceKey,
    );
    const parsedMinimum = Number(minimumScore);
    if (!chosen || !Number.isFinite(parsedMinimum) || parsedMinimum < 0 || parsedMinimum > 1) {
      setError("Choose a valid benchmark slice and a minimum score from 0 to 1.");
      return;
    }
    setBusy("adjust");
    setError(null);
    try {
      const updated = await onAdjust({
        benchmark_id: chosen.benchmark_id,
        version: chosen.version,
        slice_id: chosen.slice_id,
        minimum_score: parsedMinimum,
        reasoning_effort: effort,
        risk,
        evidence_policy: evidencePolicy,
        cost_quota_preference: costPreference,
      });
      onProposalChange(updated);
      setAdjusting(false);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Adjustment could not be validated");
    } finally {
      setBusy(null);
    }
  }

  async function resumeLaunch(): Promise<void> {
    setBusy("resume");
    setError(null);
    try {
      await onApproved(proposal);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "The approved session could not start");
    } finally {
      setBusy(null);
    }
  }

  if (terminal) {
    return (
      <section
        className="rounded-xl border border-border bg-card px-4 py-3"
        data-testid="o3-routing-proposal-terminal"
      >
        <div className="flex items-start justify-between gap-3">
          <div>
            <p className="text-sm font-semibold">Route review {proposal.decision}</p>
            <p className="mt-1 text-sm text-muted-foreground">
              The task remains unsent and no Codex session was created.
            </p>
          </div>
          <Button type="button" size="sm" variant="outline" onClick={onReset}>
            Return to draft
          </Button>
        </div>
      </section>
    );
  }

  return (
    <section
      className={cn(
        "overflow-hidden rounded-xl border bg-card text-card-foreground shadow-sm",
        proposal.disposition === "decompose" || proposal.disposition === "defer"
          ? "border-amber-500/50"
          : "border-emerald-500/40",
      )}
      data-testid="o3-routing-proposal-card"
    >
      <div className="flex flex-col gap-4 p-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <ShieldCheckIcon className="size-4 text-emerald-600" />
              <h2 className="text-sm font-semibold">O3 route review</h2>
              <Badge variant="outline">{titleCase(proposal.disposition)}</Badge>
              <Badge variant="outline">{titleCase(proposal.adviser.difficulty)}</Badge>
              <Badge
                variant="outline"
                className={cn(proposal.approved_constraints.risk === "high" && "border-red-500")}
              >
                {titleCase(proposal.approved_constraints.risk)} risk
              </Badge>
            </div>
            <p className="mt-2 text-sm font-medium" data-testid="o3-task-interpretation">
              {proposal.adviser.task_summary}
            </p>
            <p className="mt-1 text-sm text-muted-foreground">{proposal.adviser.rationale}</p>
          </div>
          <button
            type="button"
            className="inline-flex items-center gap-1 text-xs font-medium text-muted-foreground hover:text-foreground"
            onClick={() => setExpanded((value) => !value)}
            aria-expanded={expanded}
            data-testid="o3-routing-details-toggle"
          >
            Evidence
            {expanded ? (
              <ChevronUpIcon className="size-3.5" />
            ) : (
              <ChevronDownIcon className="size-3.5" />
            )}
          </button>
        </div>

        <dl className="grid grid-cols-2 gap-x-4 gap-y-3 text-sm md:grid-cols-4">
          <div>
            <dt className="text-xs text-muted-foreground">Benchmark</dt>
            <dd className="mt-0.5 font-medium" data-testid="o3-benchmark-requirement">
              {benchmark.slice_id}
            </dd>
          </div>
          <div>
            <dt className="text-xs text-muted-foreground">Conservative floor</dt>
            <dd className="mt-0.5 font-medium">{percent(benchmark.minimum_score)}</dd>
          </div>
          <div>
            <dt className="text-xs text-muted-foreground">Evidence</dt>
            <dd className="mt-0.5 font-medium">{evidenceSummary(proposal)}</dd>
          </div>
          <div>
            <dt className="text-xs text-muted-foreground">Reasoning</dt>
            <dd className="mt-0.5 font-medium">
              {titleCase(proposal.approved_constraints.reasoning_effort)}
            </dd>
          </div>
          <div>
            <dt className="text-xs text-muted-foreground">Passing routes</dt>
            <dd className="mt-0.5 font-medium">
              {proposal.frontier.passing_exact_candidates.length} exact, {eligible.length} usable
            </dd>
          </div>
          <div className="col-span-2 md:col-span-1">
            <dt className="text-xs text-muted-foreground">Best available</dt>
            <dd className="mt-0.5 truncate font-medium" title={best?.candidate.catalogue_model_id}>
              {best?.candidate.catalogue_model_id ?? "None"}
            </dd>
          </div>
          <div className="col-span-2">
            <dt className="text-xs text-muted-foreground">Quota and cost</dt>
            <dd className="mt-0.5 font-medium">{costSummary(proposal)}</dd>
          </div>
        </dl>

        {proposal.frontier.capability_gap && (
          <div
            className="flex gap-2 rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-sm"
            role="status"
            data-testid="o3-capability-gap"
          >
            <TriangleAlertIcon className="mt-0.5 size-4 shrink-0 text-amber-600" />
            <span>{proposal.frontier.capability_gap}</span>
          </div>
        )}

        {reviewingSplit && proposal.adviser.decomposition.length > 0 && (
          <div className="space-y-2" data-testid="o3-decomposition">
            <p className="text-sm font-semibold">Proposed split</p>
            {proposal.adviser.decomposition.map((item) => (
              <div
                key={`${item.dependency_order}-${item.objective}`}
                className="rounded-md border border-border px-3 py-2 text-sm"
              >
                <div className="flex items-center gap-2">
                  <Badge variant="outline">{item.dependency_order}</Badge>
                  <span className="font-medium">{item.objective}</span>
                  {item.blocked && <Badge variant="destructive">Blocked review</Badge>}
                </div>
                <p className="mt-1 text-xs text-muted-foreground">{item.competence_reduction}</p>
                <p className="mt-1 text-xs text-muted-foreground">
                  {item.slice_id} · {percent(item.minimum_score)} · {item.reasoning_effort} ·{" "}
                  {item.passing_candidates.length} passing
                </p>
              </div>
            ))}
          </div>
        )}

        {expanded && (
          <div className="space-y-3 border-t border-border pt-3" data-testid="o3-routing-details">
            <div className="grid gap-2 text-xs sm:grid-cols-3">
              <p>
                Global frontier:{" "}
                <strong>{percent(proposal.frontier.global_measured_frontier)}</strong>
              </p>
              <p>
                Configured frontier:{" "}
                <strong>{percent(proposal.frontier.accessible_configured_frontier)}</strong>
              </p>
              <p>
                Healthy frontier:{" "}
                <strong>{percent(proposal.frontier.healthy_available_frontier)}</strong>
              </p>
            </div>
            <div className="space-y-2">
              {proposal.evaluations.map((item, index) => (
                <details
                  key={item.candidate.candidate_id}
                  className="rounded-md border border-border px-3 py-2"
                  open={index === 0}
                >
                  <summary className="cursor-pointer text-sm font-medium">
                    {item.candidate.catalogue_model_id} · {titleCase(item.status)} ·{" "}
                    {titleCase(item.evidence_class)}
                  </summary>
                  <div className="mt-2 space-y-1 text-xs text-muted-foreground">
                    <p>
                      Admission: {percent(item.admission_score)} · deterministic rank:{" "}
                      {item.ranking?.deterministic_score ?? "n/a"}
                    </p>
                    <p>
                      Health: {percent(item.ranking?.health ?? null)} · scarcity penalty:{" "}
                      {percent(item.ranking?.quota_scarcity_penalty ?? null)} · latency:{" "}
                      {item.ranking?.latency_ms == null
                        ? "unknown"
                        : `${item.ranking.latency_ms} ms`}
                    </p>
                    <p>{item.candidate.cost_source}</p>
                    <p>{item.candidate.quota_source}</p>
                    {item.exclusions.map((reason) => (
                      <p key={reason} className="text-destructive">
                        Excluded: {reason}
                      </p>
                    ))}
                    {item.caveats.map((caveat) => (
                      <p key={caveat} className="text-amber-700 dark:text-amber-400">
                        Caveat: {caveat}
                      </p>
                    ))}
                    {item.evidence.map((evidence) => (
                      <p key={evidence.source_reference}>
                        Source: {evidence.source_reference} ({evidence.evidence_class})
                      </p>
                    ))}
                  </div>
                </details>
              ))}
            </div>
          </div>
        )}

        {adjusting && proposal.decision === null && (
          <div className="grid gap-3 rounded-lg border border-border bg-muted/30 p-3 sm:grid-cols-2">
            <label className="flex flex-col gap-1 text-xs text-muted-foreground sm:col-span-2">
              Benchmark slice
              <select
                className={fieldClass}
                value={sliceKey}
                onChange={(event) => setSliceKey(event.target.value)}
                data-testid="o3-adjust-slice"
              >
                {slices.map((slice) => (
                  <option
                    key={`${slice.benchmark_id}|${slice.version}|${slice.slice_id}`}
                    value={`${slice.benchmark_id}|${slice.version}|${slice.slice_id}`}
                  >
                    {slice.label} ({slice.slice_id})
                  </option>
                ))}
              </select>
            </label>
            <label className="flex flex-col gap-1 text-xs text-muted-foreground">
              Minimum score (0–1)
              <input
                className={fieldClass}
                type="number"
                min="0"
                max="1"
                step="0.01"
                value={minimumScore}
                onChange={(event) => setMinimumScore(event.target.value)}
                data-testid="o3-adjust-minimum"
              />
            </label>
            <label className="flex flex-col gap-1 text-xs text-muted-foreground">
              Reasoning effort
              <select
                className={fieldClass}
                value={effort}
                onChange={(event) => setEffort(event.target.value)}
                data-testid="o3-adjust-effort"
              >
                {(["low", "medium", "high", "xhigh"] as const).map((value) => (
                  <option key={value} value={value}>
                    {titleCase(value)}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex flex-col gap-1 text-xs text-muted-foreground">
              Risk
              <select className={fieldClass} value={risk} onChange={(e) => setRisk(e.target.value)}>
                {(["low", "medium", "high"] as const).map((value) => (
                  <option key={value} value={value}>
                    {titleCase(value)}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex flex-col gap-1 text-xs text-muted-foreground">
              Evidence policy
              <select
                className={fieldClass}
                value={evidencePolicy}
                onChange={(event) =>
                  setEvidencePolicy(event.target.value as "strict" | "provisional")
                }
              >
                <option value="strict">Strict</option>
                <option value="provisional">Allow proxy/advisory</option>
              </select>
            </label>
            <label className="flex flex-col gap-1 text-xs text-muted-foreground sm:col-span-2">
              Cost versus quota
              <select
                className={fieldClass}
                value={costPreference}
                onChange={(event) => setCostPreference(event.target.value)}
              >
                <option value="balanced">Balanced</option>
                <option value="preserve_subscription">Preserve subscription quota</option>
                <option value="lowest_cost">Lowest monetary cost</option>
                <option value="lowest_latency">Lowest latency</option>
              </select>
            </label>
            <div className="flex justify-end gap-2 sm:col-span-2">
              <Button type="button" variant="ghost" size="sm" onClick={() => setAdjusting(false)}>
                Cancel
              </Button>
              <Button
                type="button"
                size="sm"
                onClick={() => void saveAdjustment()}
                disabled={busy !== null}
                data-testid="o3-adjust-save"
              >
                {busy === "adjust" && <Loader2Icon className="mr-1 size-3.5 animate-spin" />}
                Revalidate
              </Button>
            </div>
          </div>
        )}

        {runAnyway && proposal.decision === null && (
          <div
            className="space-y-2 rounded-lg border border-destructive bg-destructive/5 p-3"
            data-testid="o3-run-anyway-confirmation"
          >
            <p className="flex items-center gap-2 text-sm font-semibold text-destructive">
              <TriangleAlertIcon className="size-4" /> Override the benchmark gate
            </p>
            <p className="text-xs text-muted-foreground">
              This does not mean the route passed. Enter why running now is justified; the reason is
              stored in the audit record.
            </p>
            <textarea
              className="min-h-20 w-full rounded-md border border-input bg-background px-3 py-2 text-sm outline-none focus-visible:border-ring"
              value={overrideReason}
              onChange={(event) => setOverrideReason(event.target.value)}
              placeholder="Reason for overriding (at least 8 characters)"
              data-testid="o3-run-anyway-reason"
            />
            <div className="flex justify-end gap-2">
              <Button type="button" variant="ghost" size="sm" onClick={() => setRunAnyway(false)}>
                Cancel
              </Button>
              <Button
                type="button"
                variant="destructive"
                size="sm"
                disabled={overrideReason.trim().length < 8 || busy !== null}
                onClick={() =>
                  void decide(
                    {
                      action: "run_anyway",
                      confirm_run_anyway: true,
                      reason: overrideReason.trim(),
                    },
                    true,
                  )
                }
                data-testid="o3-run-anyway-confirm"
              >
                {busy === "run_anyway" && <Loader2Icon className="mr-1 size-3.5 animate-spin" />}
                Confirm and run
              </Button>
            </div>
          </div>
        )}

        {error && (
          <p className="text-sm text-destructive" role="alert" data-testid="o3-routing-error">
            {error}
          </p>
        )}

        <div className="flex flex-wrap items-center justify-between gap-2 border-t border-border pt-3">
          {approved ? (
            <p
              className="min-w-0 truncate text-xs text-muted-foreground"
              data-testid="o3-approved-route"
            >
              Approved route: {proposal.derived_combo_name}
            </p>
          ) : (
            <div className="flex flex-wrap gap-1.5">
              {proposal.adviser.decomposition.length > 0 && (
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() => setReviewingSplit((value) => !value)}
                  disabled={busy !== null}
                  aria-expanded={reviewingSplit}
                  data-testid="o3-review-split"
                >
                  <ListTreeIcon className="mr-1 size-3.5" />
                  {reviewingSplit ? "Hide split" : "Review split"}
                </Button>
              )}
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => setAdjusting((value) => !value)}
                disabled={busy !== null}
                data-testid="o3-adjust"
              >
                <SlidersHorizontalIcon className="mr-1 size-3.5" /> Adjust
              </Button>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                disabled={busy !== null}
                onClick={() => void decide({ action: "defer" }, false)}
                data-testid="o3-defer"
              >
                <ClockIcon className="mr-1 size-3.5" /> Defer
              </Button>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                disabled={busy !== null}
                onClick={() => void decide({ action: "decline" }, false)}
                data-testid="o3-decline"
              >
                <XIcon className="mr-1 size-3.5" /> Decline
              </Button>
              {!runAnyway && (
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  className="border-destructive/50 text-destructive hover:bg-destructive/10"
                  onClick={() => setRunAnyway(true)}
                  disabled={busy !== null}
                  data-testid="o3-run-anyway"
                >
                  Run anyway
                </Button>
              )}
            </div>
          )}
          {(canApprove || canResumeLaunch) && (
            <Button
              type="button"
              size="sm"
              disabled={busy !== null}
              onClick={() =>
                canResumeLaunch
                  ? void resumeLaunch()
                  : void decide(
                      { action: "approve", acknowledge_provisional: hasProvisional },
                      true,
                    )
              }
              data-testid="o3-approve"
            >
              {busy === "approve" || busy === "resume" ? (
                <Loader2Icon className="mr-1 size-3.5 animate-spin" />
              ) : (
                <PlayIcon className="mr-1 size-3.5" />
              )}
              {canResumeLaunch ? "Resume approved launch" : "Approve and run"}
            </Button>
          )}
        </div>
      </div>
    </section>
  );
}
