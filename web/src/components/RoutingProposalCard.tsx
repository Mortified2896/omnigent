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

import { ExecutionProfile } from "./ExecutionProfile";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import type {
  O3BenchmarkSlice,
  O3CatalogueRecommendationItem,
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
const MAX_VISIBLE_RECOMMENDATIONS = 20;

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
  if (proposal.recommendation?.execution_set?.eligible_count)
    return "Catalogue compatibility evidence";
  if (proposal.frontier.passing_exact_candidates.length > 0) return "Exact evidence";
  if (proposal.frontier.provisional_candidates.length > 0) return "Provisional evidence";
  return "No qualifying evidence";
}

function costSummary(proposal: O3RoutingProposal): string {
  if (proposal.recommendation?.execution_set) {
    return proposal.recommendation.execution_set.eligible_count > 0
      ? "Live quota and cost require recheck"
      : "No eligible route";
  }
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

function readinessLabel(item: O3CatalogueRecommendationItem): string {
  const successful = ["callable", "callable_now", "success", "responses_callable"].includes(
    item.responses_callability,
  );
  if (!successful) return `Unverified · last result: ${titleCase(item.responses_callability)}`;
  const age = item.readiness_observed_at
    ? Date.now() - Date.parse(item.readiness_observed_at)
    : NaN;
  return age >= 0 && age <= 15 * 60 * 1000 ? "Recently verified" : "Previously verified";
}

function RecommendationRows({
  items,
  totalCount = items.length,
}: {
  items: O3CatalogueRecommendationItem[];
  totalCount?: number;
}) {
  const visibleItems = items.slice(0, MAX_VISIBLE_RECOMMENDATIONS);
  const hiddenCount = Math.max(
    totalCount - visibleItems.length,
    items.length - visibleItems.length,
  );
  return (
    <div className="space-y-2">
      {visibleItems.map((item) => (
        <details
          key={item.route_id}
          className="rounded-md border border-border px-3 py-2"
          data-testid="o3-recommendation-row"
        >
          <summary className="cursor-pointer text-sm">
            <span className="font-medium">{item.route_id}</span>{" "}
            <span className="text-muted-foreground">
              · {item.reasoning_mode} · {readinessLabel(item)}
            </span>
          </summary>
          <div className="mt-2 space-y-1 text-xs text-muted-foreground">
            <p>
              Capability: {item.capability_score_lower.toFixed(0)}–
              {item.capability_score_upper?.toFixed(0) ?? "?"} / 100 ·{" "}
              {titleCase(item.capability_confidence)} confidence
            </p>
            <p>
              {item.estimate_method} · readiness {item.readiness_observed_at ?? "unknown"} · raw
              cost {item.raw_cost_class}
            </p>
            {item.alternate_route_ids.length > 0 && (
              <p>
                {item.alternate_route_ids.length} aliases: {item.alternate_route_ids.join(", ")}
              </p>
            )}
            {item.caveats.map((caveat) => (
              <p key={caveat}>{caveat}</p>
            ))}
          </div>
        </details>
      ))}
      {hiddenCount > 0 && (
        <p className="text-xs text-muted-foreground" data-testid="o3-recommendation-truncated">
          Showing the first {visibleItems.length.toLocaleString()} of {totalCount.toLocaleString()}.
          The full catalogue remains in the proposal record.
        </p>
      )}
    </div>
  );
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
  const [alternativesExpanded, setAlternativesExpanded] = useState(false);
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
  const best = eligible[0] ?? null;
  const recommendation = proposal.recommendation ?? null;
  const terminal = proposal.decision === "decline" || proposal.decision === "defer";
  const waiting = proposal.decision === "wait";
  const approved = proposal.decision === "approve" || proposal.decision === "run_anyway";
  const catalogueEligible = recommendation?.execution_set?.eligible_count ?? 0;
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
        benchmark_id: chosen.benchmark_id,
        version: chosen.version,
        slice_id: chosen.slice_id,
        difficulty,
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

        <details open className="rounded-lg border p-3" data-testid="o3-review-activity">
          <summary className="cursor-pointer text-sm font-semibold">Review conversation</summary>
          <div className="mt-3 space-y-3 text-sm">
            {proposal.adviser_exchanges?.length ? (
              proposal.adviser_exchanges.map((exchange) => (
                <div key={JSON.stringify(exchange.request)} className="space-y-3">
                  <div className="ml-6 rounded-lg bg-muted/50 p-3">
                    <p className="font-medium">Context sent to advisor</p>
                    <p className="mt-1 text-xs text-muted-foreground">
                      Requested: {exchange.requested_model} · Reasoning effort:{" "}
                      {exchange.reasoning_effort}
                      {exchange.attempt > 1 ? " · Schema repair retry" : ""}
                    </p>
                    <details className="mt-2">
                      <summary className="cursor-pointer">
                        View exact request and instructions
                      </summary>
                      <pre className="mt-2 max-h-72 overflow-auto whitespace-pre-wrap break-words text-xs">
                        {JSON.stringify(exchange.request, null, 2)}
                      </pre>
                    </details>
                  </div>
                  <div className="mr-6 rounded-lg border p-3">
                    <p className="font-medium">
                      Advisor · {exchange.actual_model ?? "Actual model not reported"}
                    </p>
                    <p className="text-xs text-muted-foreground">
                      Provider: {exchange.actual_provider ?? "Not reported"}
                    </p>
                    <p className="mt-2 whitespace-pre-wrap">{exchange.explanation}</p>
                    {exchange.reasoning_summary ? (
                      <details className="mt-2">
                        <summary className="cursor-pointer">Provider reasoning summary</summary>
                        <p className="mt-2 whitespace-pre-wrap">{exchange.reasoning_summary}</p>
                      </details>
                    ) : (
                      <p className="mt-2 text-xs text-muted-foreground">
                        No reasoning summary was returned by the provider.
                      </p>
                    )}
                  </div>
                </div>
              ))
            ) : (
              <div className="mr-6 rounded-lg border p-3">
                <p className="font-medium">
                  {proposal.adviser_mode === "local_rule"
                    ? "App · Local greeting rule"
                    : "Review explanation"}
                </p>
                <p className="mt-2">{proposal.adviser.rationale}</p>
                <p className="mt-2 text-xs text-muted-foreground">
                  {proposal.adviser_mode === "local_rule"
                    ? "No model was called for this exact standalone greeting."
                    : "No advisor request transcript was recorded for this review."}
                  {proposal.estimator?.actual_model
                    ? ` Reported advisor: ${proposal.estimator.actual_model}.`
                    : ""}
                </p>
              </div>
            )}
            <div className="rounded-lg bg-muted/30 p-3">
              <p className="font-medium">App · Current floor mapping</p>
              <p className="mt-1">
                {titleCase(constraints.difficulty)} → {constraints.benchmark.slice_id} → minimum{" "}
                {percent(constraints.benchmark.minimum_score)}.
              </p>
              <p className="mt-1 text-xs text-muted-foreground">
                Calibration: {constraints.calibration_version}. The app applies the floor and checks
                route eligibility. Route checks verify provider access to a model; they are separate
                from the advisor explanation.
              </p>
            </div>
          </div>
        </details>

        {recommendation && (
          <div
            className="rounded-lg border border-border bg-muted/30 p-3"
            data-testid="o3-compact-recommendation"
          >
            <p className="text-sm font-semibold">
              {catalogueEligible > 0
                ? `${recommendation.section_counts.eligible_models != null ? `${recommendation.section_counts.eligible_models} model groups · ` : ""}${catalogueEligible} eligible routes`
                : "No route meets these requirements"}
            </p>
            <p className="mt-1 text-sm text-muted-foreground">
              {catalogueEligible > 0
                ? "Routes meet the capability and input/output requirements. Access is rechecked when you continue."
                : "Adjust the requirements or wait for availability to change."}
            </p>
            {!proposal.selected_execution &&
              recommendation.model_groups?.find((group) => group.eligible_route_count > 0) && (
                <p className="mt-2 break-words text-sm">
                  Leading option:{" "}
                  <strong>
                    {
                      recommendation.model_groups.find((group) => group.eligible_route_count > 0)
                        ?.displayed_model
                    }
                  </strong>
                  <span className="text-muted-foreground">
                    {" "}
                    · final route selected after access recheck
                  </span>
                </p>
              )}
          </div>
        )}

        {(!recommendation || expanded) && (
          <dl className="grid grid-cols-2 gap-x-4 gap-y-3 text-sm md:grid-cols-4">
            <div>
              <dt className="text-xs text-muted-foreground">Benchmark</dt>
              <dd className="mt-0.5 font-medium" data-testid="o3-benchmark-requirement">
                {benchmark.slice_id}
              </dd>
            </div>
            {recommendation && (
              <>
                <div>
                  <dt className="text-xs text-muted-foreground">Raw benchmark floor</dt>
                  <dd className="mt-0.5 font-medium">
                    {recommendation.raw_benchmark_floor.toFixed(6)}
                  </dd>
                </div>
                <div>
                  <dt className="text-xs text-muted-foreground">Common capability floor</dt>
                  <dd className="mt-0.5 font-medium">
                    {recommendation.common_capability_floor.toFixed(0)} / 100{" "}
                    <span className="text-xs font-normal text-muted-foreground">rough prior</span>
                  </dd>
                </div>
                <div>
                  <dt className="text-xs text-muted-foreground">Catalogue considered</dt>
                  <dd className="mt-0.5 font-medium">
                    {recommendation.total_route_count.toLocaleString()} routes ·{" "}
                    {recommendation.live_present_count.toLocaleString()} live
                  </dd>
                </div>
                <div>
                  <dt className="text-xs text-muted-foreground">Above-floor configurations</dt>
                  <dd className="mt-0.5 font-medium">
                    {(recommendation.section_counts.callable_non_codex ?? 0) +
                      (recommendation.section_counts.other_above_floor ?? 0) +
                      (recommendation.section_counts.codex_subscription_fallback ?? 0)}
                  </dd>
                </div>
              </>
            )}
            <div>
              <dt className="text-xs text-muted-foreground">Difficulty</dt>
              <dd className="mt-0.5 font-medium">{titleCase(constraints.difficulty)}</dd>
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
                {recommendation?.execution_set
                  ? `${catalogueEligible} catalogue-eligible; runtime readiness separate`
                  : `${proposal.frontier.passing_exact_candidates.length} exact, ${eligible.length} usable`}
              </dd>
            </div>
            <div className="col-span-2 md:col-span-1">
              <dt className="text-xs text-muted-foreground">Best available</dt>
              <dd
                className="mt-0.5 truncate font-medium"
                title={best?.candidate.catalogue_model_id}
              >
                {recommendation?.execution_set
                  ? catalogueEligible > 0
                    ? "Selected on approval after recheck"
                    : "None"
                  : (best?.candidate.catalogue_model_id ?? "None")}
              </dd>
            </div>
            <div className="col-span-2">
              <dt className="text-xs text-muted-foreground">Quota and cost</dt>
              <dd className="mt-0.5 font-medium">{costSummary(proposal)}</dd>
            </div>
          </dl>
        )}

        {recommendation && (
          <div>
            <button
              type="button"
              className="flex w-full items-center justify-between gap-2 text-sm font-medium"
              aria-expanded={alternativesExpanded}
              aria-controls="o3-model-alternatives"
              data-testid="o3-alternatives-toggle"
              onClick={() => setAlternativesExpanded((value) => !value)}
            >
              <span>Models and provider routes</span>
              {alternativesExpanded ? (
                <ChevronUpIcon className="size-4" />
              ) : (
                <ChevronDownIcon className="size-4" />
              )}
            </button>
            {alternativesExpanded && (
              <div id="o3-model-alternatives" className="mt-3 space-y-3">
                {recommendation.model_groups?.length ? (
                  <div data-testid="o3-model-groups" className="space-y-2">
                    <p className="text-xs text-muted-foreground">
                      {recommendation.section_counts.above_floor_models} model groups ·{" "}
                      {recommendation.section_counts.above_floor_routes} above-floor routes. Only{" "}
                      {catalogueEligible} routes satisfy all task requirements. Uncertain identities
                      stay separate.
                    </p>
                    {recommendation.model_groups.map((group) => (
                      <details
                        key={group.model_identity}
                        className="rounded-md border border-border p-3"
                        data-testid="o3-model-group"
                      >
                        <summary className="cursor-pointer break-words text-sm">
                          <span className="font-medium">{group.displayed_model}</span>
                          <span className="text-muted-foreground">
                            {" "}
                            · {group.configuration_count} configurations · {group.route_count}{" "}
                            routes · {group.eligible_route_count} eligible
                          </span>
                        </summary>
                        <div className="mt-3">
                          <RecommendationRows
                            items={group.configurations}
                            totalCount={group.route_count}
                          />
                        </div>
                      </details>
                    ))}
                    {recommendation.section_counts.above_floor_models >
                      recommendation.model_groups.length && (
                      <p className="text-xs text-muted-foreground">
                        Showing {recommendation.model_groups.length} model groups, with eligible
                        options first. All execution routes remain in the proposal.
                      </p>
                    )}
                  </div>
                ) : null}
              </div>
            )}
          </div>
        )}

        {recommendation && alternativesExpanded && !recommendation.model_groups?.length && (
          <div className="space-y-4" data-testid="o3-catalogue-recommendations">
            {recommendation.stale_warning && (
              <div
                className="rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-sm"
                role="status"
              >
                {recommendation.stale_warning}
              </div>
            )}
            <div>
              <h3 className="mb-2 text-sm font-semibold">Previously verified non-Codex</h3>
              <RecommendationRows
                items={recommendation.callable_non_codex}
                totalCount={recommendation.section_counts.callable_non_codex}
              />
              {recommendation.callable_non_codex.length === 0 && (
                <p className="text-sm text-muted-foreground">
                  No previously verified non-Codex route meets the conservative floor.
                </p>
              )}
            </div>
            <div>
              <h3 className="mb-2 text-sm font-semibold">Other models above floor</h3>
              <RecommendationRows
                items={recommendation.other_above_floor}
                totalCount={recommendation.section_counts.other_above_floor}
              />
            </div>
            <div>
              <h3 className="mb-2 text-sm font-semibold">Codex subscription fallback</h3>
              <p className="mb-2 text-xs text-muted-foreground">
                Subscription-backed fallback, shown after non-Codex options.
              </p>
              <RecommendationRows
                items={recommendation.codex_subscription_fallback}
                totalCount={recommendation.section_counts.codex_subscription_fallback}
              />
            </div>
            {((recommendation.section_counts.callable_non_codex ?? 0) +
              (recommendation.section_counts.other_above_floor ?? 0) +
              (recommendation.section_counts.codex_subscription_fallback ?? 0) ===
              0 ||
              expanded) && (
              <div>
                {(recommendation.section_counts.callable_non_codex ?? 0) +
                  (recommendation.section_counts.other_above_floor ?? 0) +
                  (recommendation.section_counts.codex_subscription_fallback ?? 0) ===
                  0 && (
                  <p className="mb-2 text-sm text-muted-foreground">
                    No catalogue model meets the conservative floor. Nearest alternatives are shown
                    for review, not as qualifying matches.
                  </p>
                )}
                <h3 className="mb-2 text-sm font-semibold">Nearest below floor</h3>
                <RecommendationRows
                  items={recommendation.nearest_below_floor}
                  totalCount={recommendation.section_counts.nearest_below_floor}
                />
              </div>
            )}
          </div>
        )}

        <ExecutionProfile
          key={`${proposal.proposal_id}-${proposal.constraint_version ?? 1}`}
          proposal={proposal}
          disabled={busy !== null || !(proposal.decision === null || waiting)}
          onAdjust={adjustRequirements}
        />

        {proposal.frontier.capability_gap && catalogueEligible === 0 && (
          <div
            className="flex gap-2 rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-sm"
            role="status"
            data-testid="o3-capability-gap"
          >
            <TriangleAlertIcon className="mt-0.5 size-4 shrink-0 text-amber-600" />
            <span>{proposal.frontier.capability_gap}</span>
          </div>
        )}

        {proposal.selected_execution?.mode !== "hard_tool_free" &&
          proposal.resource_advice &&
          proposal.resource_snapshot && (
            <div
              className="rounded-md border border-border px-3 py-2 text-sm"
              data-testid="o3-resource-advice"
            >
              <p className="font-semibold">
                {proposal.resource_snapshot.usable_routes === 0 &&
                proposal.resource_snapshot.unknown_routes > 0
                  ? "Availability unverified"
                  : titleCase(proposal.resource_advice.action)}
              </p>
              <p className="mt-1 text-muted-foreground">{proposal.resource_advice.reason}</p>
              {expanded && (
                <p className="mt-1 text-xs text-muted-foreground">
                  Current status: {proposal.resource_snapshot.usable_routes} usable ·{" "}
                  {proposal.resource_snapshot.blocked_routes} blocked ·{" "}
                  {proposal.resource_snapshot.unknown_routes} unknown ·{" "}
                  {proposal.resource_snapshot.status_coverage_percent.toFixed(0)}% coverage ·{" "}
                  {proposal.resource_snapshot.serialized_bytes} bytes. Advice source:{" "}
                  {titleCase(proposal.resource_advice.source)}.
                </p>
              )}
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
                  {item.slice_id} · {titleCase(item.difficulty)} · {item.reasoning_effort} ·{" "}
                  {item.passing_candidates.length} passing
                </p>
              </div>
            ))}
          </div>
        )}

        {expanded && (
          <div className="space-y-3 border-t border-border pt-3" data-testid="o3-routing-details">
            {proposal.estimator && (
              <div className="rounded-md border border-border px-3 py-2 text-xs">
                <p className="font-semibold">Estimator policy</p>
                <p className="mt-1 text-muted-foreground">
                  {proposal.estimator.policy.slice_id} · minimum proxy{" "}
                  {proposal.estimator.policy.minimum_common_capability.toFixed(0)} ·{" "}
                  {proposal.estimator.policy.evidence_policy} evidence ·{" "}
                  {proposal.estimator.policy.reasoning_effort} reasoning
                </p>
                <p className="mt-1 text-muted-foreground">
                  {proposal.estimator.evidence_label} · {proposal.estimator.eligible_count} eligible
                  · actual{" "}
                  {proposal.estimator.actual_provider && proposal.estimator.actual_model
                    ? `${proposal.estimator.actual_provider}/${proposal.estimator.actual_model}`
                    : "unknown"}
                </p>
              </div>
            )}
            <p className="text-xs text-muted-foreground" data-testid="o3-calibration-detail">
              {titleCase(constraints.difficulty)} → O3 admission floor{" "}
              {benchmark.minimum_score.toFixed(6)} via {constraints.calibration_version}
            </p>
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
