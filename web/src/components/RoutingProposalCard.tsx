import { O3DecisionSummary } from "./O3DecisionInspect";
import { useEffect, useMemo, useState } from "react";
import {
  ClockIcon,
  ListTreeIcon,
  Loader2Icon,
  PlayIcon,
  SlidersHorizontalIcon,
  XIcon,
  TriangleAlertIcon,
} from "lucide-react";

import { ExecutionProfile } from "./ExecutionProfile";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import type {
  O3BenchmarkSlice,
  O3Difficulty,
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

function titleCase(value: string): string {
  return value
    .split("_")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
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
  const [difficulty, setDifficulty] = useState<O3Difficulty>(constraints.difficulty);
  const [effort, setEffort] = useState(constraints.reasoning_effort);
  const [risk, setRisk] = useState(constraints.risk);
  const [evidencePolicy, setEvidencePolicy] = useState(constraints.evidence_policy);
  const [costPreference, setCostPreference] = useState(constraints.cost_quota_preference);

  useEffect(() => {
    setSliceKey(`${benchmark.benchmark_id}|${benchmark.version}|${benchmark.slice_id}`);
    setDifficulty(constraints.difficulty);
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
    constraints.difficulty,
    constraints.evidence_policy,
    constraints.reasoning_effort,
    constraints.risk,
  ]);

  const eligible = useMemo(
    () => proposal.evaluations.filter((item) => item.status !== "excluded"),
    [proposal.evaluations],
  );
  const recommendation = proposal.recommendation ?? null;
  const terminal = proposal.decision === "decline" || proposal.decision === "defer";
  const waiting = proposal.decision === "wait";
  const approved = proposal.decision === "approve" || proposal.decision === "run_anyway";
  const catalogueEligible = recommendation?.execution_set?.eligible_count ?? 0;
  const originalAdviser =
    proposal.original_adviser ?? (proposal.constraint_version === 1 ? proposal.adviser : null);
  const executionEfforts = Array.from(
    new Set([
      constraints.reasoning_effort,
      ...(originalAdviser ? [originalAdviser.proposed_reasoning_effort] : []),
      ...proposal.evaluations.flatMap((item) => item.candidate.supported_reasoning_efforts),
      ...(recommendation?.execution_set?.eligible ?? []).map((item) => item.reasoning_mode),
      ...(recommendation?.execution_set?.excluded ?? []).map((item) => item.reasoning_mode),
    ]),
  ).filter((value) => ["low", "medium", "high", "xhigh"].includes(value));
  const canApprove =
    (eligible.length > 0 ||
      catalogueEligible > 0 ||
      (proposal.execution_options?.length ?? 0) > 0) &&
    (proposal.decision === null || proposal.decision === "wait");
  const canResumeLaunch =
    approved &&
    (proposal.derived_combo_name !== null ||
      proposal.selected_execution?.mode === "hard_tool_free");
  const hasProvisional =
    eligible.some((item) => item.status === "provisional") ||
    proposal.selected_execution?.mode === "hard_tool_free";

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
    if (!chosen) {
      setError("Choose a valid benchmark slice.");
      return;
    }
    setBusy("adjust");
    setError(null);
    try {
      const updated = await onAdjust({
        ...(sliceKey !== [benchmark.benchmark_id, benchmark.version, benchmark.slice_id].join("|")
          ? {
              benchmark_id: chosen.benchmark_id,
              version: chosen.version,
              slice_id: chosen.slice_id,
            }
          : {}),
        ...(difficulty !== constraints.difficulty ? { difficulty } : {}),
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

  async function adjustRequirements(adjustment: O3ProposalAdjustment): Promise<void> {
    setBusy("capabilities");
    setError(null);
    try {
      onProposalChange(await onAdjust(adjustment));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Requirements could not be validated");
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
            <O3DecisionSummary proposal={proposal} />
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
      <div className="flex flex-col gap-1 px-3 py-2">
        <O3DecisionSummary proposal={proposal} />
        <details className="text-sm" data-testid="o3-execution-reasoning">
          <summary className="min-h-11 cursor-pointer py-3">Execution settings</summary>
          <div className="flex items-center gap-2 py-2">
            <span>Execution effort</span>
            <Select
              value={constraints.reasoning_effort}
              disabled={busy !== null || terminal || approved}
              onValueChange={(value) => void adjustRequirements({ reasoning_effort: value })}
            >
              <SelectTrigger className="h-11 w-32" aria-label="Execution reasoning">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {executionEfforts.map((value) => (
                  <SelectItem key={value} value={value}>
                    {titleCase(value)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Button
              variant="outline"
              size="sm"
              disabled={busy !== null || approved || originalAdviser === null}
              onClick={() => void adjustRequirements({ reset_reasoning_effort: true })}
              data-testid="o3-reset-reasoning"
            >
              Reset
            </Button>
          </div>
          <p className="text-xs text-muted-foreground">
            Estimator recommendation:{" "}
            {originalAdviser
              ? titleCase(originalAdviser.proposed_reasoning_effort)
              : "unavailable for this older review"}
          </p>
          <ExecutionProfile
            key={`${proposal.proposal_id}-${proposal.constraint_version ?? 1}`}
            proposal={proposal}
            disabled={busy !== null || !(proposal.decision === null || waiting)}
            onAdjust={adjustRequirements}
          />
        </details>
        {proposal.frontier.capability_gap && catalogueEligible === 0 && (
          <p className="text-sm text-amber-600" role="status" data-testid="o3-capability-gap">
            {proposal.frontier.capability_gap}
          </p>
        )}
        {proposal.resource_advice?.action === "wait" && (
          <p className="text-xs text-muted-foreground" data-testid="o3-resource-advice">
            {proposal.resource_advice.reason}
          </p>
        )}
        {reviewingSplit && (
          <pre
            data-testid="o3-decomposition"
            className="max-h-64 overflow-auto whitespace-pre-wrap text-xs"
          >
            {JSON.stringify(proposal.adviser.decomposition, null, 2)}
          </pre>
        )}
        {adjusting && (proposal.decision === null || waiting) && (
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
              Difficulty
              <select
                className={fieldClass}
                value={difficulty}
                onChange={(event) => setDifficulty(event.target.value as O3Difficulty)}
                data-testid="o3-adjust-difficulty"
              >
                {(["easy", "normal", "moderate", "hard", "frontier"] as const).map((value) => (
                  <option key={value} value={value}>
                    {titleCase(value)}
                  </option>
                ))}
              </select>
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

        {runAnyway && (proposal.decision === null || waiting) && (
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

        {proposal.selected_execution && (
          <div
            className="rounded-md border border-border px-3 py-2 text-sm"
            data-testid="o3-execution-mode"
          >
            <p>
              Execution:{" "}
              {proposal.selected_execution.mode === "hard_tool_free" ? "Tool-free" : "Tool-capable"}
            </p>
            <p>
              {proposal.selected_execution.route} · {proposal.selected_execution.cost_class}
            </p>
            <p className="text-xs text-muted-foreground">{proposal.selected_execution.reason}</p>
            {proposal.selected_execution.mode === "hard_tool_free" && (
              <p className="text-xs text-muted-foreground">
                No callable tools. Follow-ups require a new routing review. Approval accepts
                provisional quality evidence for this route.
              </p>
            )}
          </div>
        )}

        {waiting && (
          <div
            className="rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-sm"
            role="status"
            data-testid="o3-waiting"
          >
            Waiting preserves task, capability floor, and constraint version{" "}
            {proposal.constraint_version ?? 1}. Continue rechecks the approved route before launch;
            Adjust creates a new constraint version.
          </div>
        )}

        <div className="flex flex-wrap items-center justify-between gap-2 border-t border-border pt-3">
          {approved ? (
            <p
              className="min-w-0 truncate text-xs text-muted-foreground"
              data-testid="o3-approved-route"
            >
              Approved route: {proposal.selected_execution?.route ?? proposal.derived_combo_name}
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
                onClick={() => void decide({ action: "wait" }, false)}
                data-testid="o3-wait"
              >
                <ClockIcon className="mr-1 size-3.5" /> Wait
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
              {canResumeLaunch
                ? "Resume approved launch"
                : waiting
                  ? "Continue after recheck"
                  : "Approve and run"}
            </Button>
          )}
        </div>
      </div>
    </section>
  );
}
