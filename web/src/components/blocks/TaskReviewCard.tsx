// Outcome-review card for a single task run.
//
// Surfaces the human review UI for one terminal task. The card
// is data-collection only: it does NOT influence routing. It
// shows:
//
// - Task status, requested route, selected provider/model, fallback
//   flag, duration, available objective evidence
// - LLM verdict / confidence / reasoning / unresolved issues
// - Editable review controls (outcome, quality, evaluator accuracy,
//   task family, comments) + Save / Skip buttons
//
// Skipping renders the card as "Outcome not reviewed" until the
// operator saves a verdict. The card is collapsible so it doesn't
// dominate the screen on long conversations; it's mounted as a
// standalone panel by ChatPage (NOT inlined into the bubble
// stream — the review surface is post-turn).

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertCircleIcon,
  CheckIcon,
  ChevronDownIcon,
  ChevronRightIcon,
  LoaderIcon,
  XIcon,
} from "lucide-react";
import {
  EVALUATOR_ACCURACY_VALUES,
  TASK_FAMILIES,
  getTaskRun,
  submitTaskRunReview,
} from "@/lib/taskOutcomes";
import type {
  TaskReview,
  TaskRun,
  TaskRunDetailResponse,
  TaskEvaluation,
  UpsertReviewRequest,
} from "@/lib/taskOutcomes";

export interface TaskReviewCardProps {
  taskRunId: string;
  /** Optional initial snapshot from the list endpoint — keeps the
   * card populated while the detail endpoint is fetching. */
  initialRun?: TaskRun | null;
  /** Called after a successful Save so the parent can refresh the
   * unreviewed-outcomes list. */
  onReviewed?: () => void;
  /** Optional controlled open state. */
  isOpen?: boolean;
  onOpenChange?: (open: boolean) => void;
  /** Local-only postponement; must not create a review record. */
  onReviewLater?: () => void;
}

/** Outcome-verdict options for the human review. */
const OUTCOME_OPTIONS: ReadonlyArray<{
  value: TaskReview["verdict"];
  label: string;
}> = [
  { value: "success", label: "Successful" },
  { value: "partial", label: "Partially successful" },
  { value: "failure", label: "Failed" },
  { value: "unsure", label: "Unsure" },
];

/** Map evaluation verdict -> a tiny status badge colour. */
function verdictColor(
  v: TaskRun["terminal_status"] | TaskEvaluation["verdict"] | undefined,
): string {
  if (v === "completed" || v === "success") return "bg-emerald-100 text-emerald-700";
  if (v === "failed" || v === "failure") return "bg-rose-100 text-rose-700";
  if (v === "cancelled") return "bg-slate-100 text-slate-700";
  if (v === "incomplete") return "bg-amber-100 text-amber-700";
  if (v === "partial") return "bg-amber-100 text-amber-700";
  if (v === "inconclusive") return "bg-amber-100 text-amber-700";
  return "bg-blue-100 text-blue-700";
}

/** Format milliseconds as a friendly duration. */
function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "—";
  if (ms < 1000) return `${ms} ms`;
  const seconds = Math.floor(ms / 1000);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${seconds % 60}s`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${minutes % 60}m`;
}

/** Format a token count compactly. */
function formatTokens(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  if (n < 1000) return `${n}`;
  return `${(n / 1000).toFixed(1)}k`;
}

/** Format USD cost. */
function formatCost(usd: number | null | undefined): string {
  if (usd === null || usd === undefined) return "—";
  if (usd < 0.01) return `$${usd.toFixed(4)}`;
  return `$${usd.toFixed(2)}`;
}

export function TaskReviewCard({
  taskRunId,
  initialRun,
  onReviewed,
  isOpen: controlledOpen,
  onOpenChange,
  onReviewLater,
}: TaskReviewCardProps) {
  const [internalOpen, setInternalOpen] = useState<boolean>(false);
  const isControlled = controlledOpen !== undefined;
  const isOpen = isControlled ? controlledOpen : internalOpen;

  const [detail, setDetail] = useState<TaskRunDetailResponse | null>(null);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState<boolean>(false);

  // Local form state. Initialised from the existing review (if any)
  // so re-edits don't wipe the reviewer's previous choices.
  const [outcome, setOutcome] = useState<TaskReview["verdict"] | "">("");
  const [qualityScore, setQualityScore] = useState<number | "">("");
  const [finalFamily, setFinalFamily] = useState<string>("");
  const [evaluatorAccuracy, setEvaluatorAccuracy] = useState<string>("");
  const [routeFit, setRouteFit] = useState<string>("");
  const [failureAttribution, setFailureAttribution] = useState<string>("");
  const [preferredRoute, setPreferredRoute] = useState<string>("");
  const [preferredEffort, setPreferredEffort] = useState<string>("");
  const [comments, setComments] = useState<string>("");

  const run = detail?.run ?? initialRun ?? null;
  const evaluation = detail?.evaluation ?? null;
  const detailsId = `task-review-details-${taskRunId}`;
  const review = detail?.review ?? null;

  const fetchDetail = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const next = await getTaskRun(taskRunId);
      setDetail(next);
      // Hydrate form fields from the saved review (so re-edits
      // preserve prior choices).
      const existing = next.review;
      if (existing) {
        setOutcome(existing.verdict);
        setQualityScore(existing.quality_score ?? "");
        setFinalFamily(existing.final_task_family ?? "");
        setEvaluatorAccuracy(existing.evaluator_accuracy ?? "");
        setRouteFit(existing.route_fit ?? "");
        setFailureAttribution(existing.failure_attribution ?? "");
        setPreferredRoute(existing.preferred_route_id ?? "");
        setPreferredEffort(existing.preferred_reasoning_effort ?? "");
        setComments(existing.comments ?? "");
      } else {
        setOutcome("");
        setQualityScore("");
        setFinalFamily(next.evaluation?.proposed_task_family ?? "");
        setEvaluatorAccuracy("");
        setRouteFit("");
        setFailureAttribution("");
        setPreferredRoute("");
        setPreferredEffort("");
        setComments("");
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, [taskRunId]);

  useEffect(() => {
    if (isOpen) {
      fetchDetail();
    }
  }, [isOpen, fetchDetail]);

  const toggleOpen = useCallback(() => {
    if (isControlled) {
      onOpenChange?.(!controlledOpen);
    } else {
      setInternalOpen((v) => !v);
    }
  }, [isControlled, controlledOpen, onOpenChange]);

  const submit = useCallback(
    async (skipped: boolean) => {
      if (saving) return;
      setSaving(true);
      setError(null);
      try {
        const body: UpsertReviewRequest = skipped
          ? { verdict: "skipped" }
          : {
              action: "adjust",
              source_evaluation_id: evaluation?.id ?? null,
              verdict: (outcome as TaskReview["verdict"]) || review?.verdict || "unsure",
              quality_score: qualityScore === "" ? null : Number(qualityScore),
              final_task_family: finalFamily === "" ? null : finalFamily,
              evaluator_accuracy:
                evaluatorAccuracy === ""
                  ? null
                  : (evaluatorAccuracy as TaskReview["evaluator_accuracy"]),
              route_fit: routeFit === "" ? null : (routeFit as TaskReview["route_fit"]),
              failure_attribution: failureAttribution === "" ? null : failureAttribution,
              preferred_route_id: preferredRoute === "" ? null : preferredRoute,
              preferred_reasoning_effort: preferredEffort === "" ? null : preferredEffort,
              comments: comments === "" ? null : comments,
            };
        await submitTaskRunReview(taskRunId, body);
        await fetchDetail();
        onReviewed?.();
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      } finally {
        setSaving(false);
      }
    },
    [
      taskRunId,
      outcome,
      qualityScore,
      finalFamily,
      evaluatorAccuracy,
      routeFit,
      failureAttribution,
      preferredRoute,
      preferredEffort,
      comments,
      evaluation?.id,
      review?.verdict,
      fetchDetail,
      onReviewed,
      saving,
    ],
  );

  const summary = useMemo(() => {
    if (!run) return null;
    return {
      status: run.terminal_status,
      requested: run.requested_route_id,
      provider: run.selected_provider,
      model: run.selected_model,
      fallback: run.fallback_used,
      duration: run.duration_ms,
      harness: run.harness_id,
    };
  }, [run]);

  return (
    <div
      className="rounded-lg border border-slate-200 bg-white shadow-sm"
      data-testid="task-review-card"
      data-task-run-id={taskRunId}
    >
      <button
        type="button"
        onClick={toggleOpen}
        aria-expanded={isOpen}
        aria-controls={detailsId}
        className="flex min-h-11 w-full items-center gap-2 px-4 py-2 text-left"
      >
        {isOpen ? (
          <ChevronDownIcon className="h-4 w-4 text-slate-500" />
        ) : (
          <ChevronRightIcon className="h-4 w-4 text-slate-500" />
        )}
        <span className="flex-1 truncate text-sm font-medium text-slate-900">
          Task outcome review
        </span>
        {summary && (
          <span
            className={`rounded-full px-2 py-0.5 text-xs font-medium ${verdictColor(summary.status)}`}
          >
            {summary.status}
          </span>
        )}
        {review?.verdict === "skipped" && (
          <span className="rounded-full bg-slate-100 px-2 py-0.5 text-xs font-medium text-slate-600">
            Skipped
          </span>
        )}
        {!review && (
          <span className="rounded-full bg-amber-100 px-2 py-0.5 text-xs font-medium text-amber-700">
            Not reviewed
          </span>
        )}
      </button>
      {isOpen && (
        <div id={detailsId} className="border-t border-slate-100 px-4 py-3 text-sm text-slate-700">
          {loading && (
            <div className="flex items-center gap-2 py-4 text-slate-500">
              <LoaderIcon className="h-4 w-4 animate-spin" />
              Loading…
            </div>
          )}
          {error && (
            <div className="flex items-center gap-2 rounded-md bg-rose-50 px-3 py-2 text-rose-700">
              <AlertCircleIcon className="h-4 w-4" />
              {error}
            </div>
          )}
          {run && (
            <TaskReviewCardBody
              run={run}
              routing={detail?.routing ?? null}
              evaluation={evaluation}
            />
          )}
          {run && (
            <TaskReviewForm
              outcome={outcome}
              setOutcome={setOutcome}
              qualityScore={qualityScore}
              setQualityScore={setQualityScore}
              finalFamily={finalFamily}
              setFinalFamily={setFinalFamily}
              evaluatorAccuracy={evaluatorAccuracy}
              setEvaluatorAccuracy={setEvaluatorAccuracy}
              routeFit={routeFit}
              setRouteFit={setRouteFit}
              failureAttribution={failureAttribution}
              setFailureAttribution={setFailureAttribution}
              preferredRoute={preferredRoute}
              setPreferredRoute={setPreferredRoute}
              preferredEffort={preferredEffort}
              setPreferredEffort={setPreferredEffort}
              comments={comments}
              setComments={setComments}
              saving={saving}
              onSave={() => submit(false)}
              onSkip={() => {
                onReviewLater?.();
                onOpenChange?.(false);
                if (!isControlled) setInternalOpen(false);
              }}
              existingReview={review}
            />
          )}
        </div>
      )}
    </div>
  );
}

function formatPackage(
  value:
    | {
        harness: string | null;
        provider: string | null;
        model: string | null;
        route_id: string | null;
        reasoning_effort: string | null;
        permission_mode: string | null;
      }
    | null
    | undefined,
): string {
  if (!value) return "—";
  return [
    value.harness,
    value.route_id ?? (value.provider && value.model ? `${value.provider}/${value.model}` : null),
    value.reasoning_effort,
    value.permission_mode,
  ]
    .filter(Boolean)
    .join(" · ");
}

function TaskReviewCardBody({
  run,
  routing,
  evaluation,
}: {
  run: TaskRun;
  routing: TaskRunDetailResponse["routing"];
  evaluation: TaskEvaluation | null;
}) {
  return (
    <div className="grid gap-3 pb-3">
      <div className="grid grid-cols-2 gap-x-6 gap-y-1 text-xs">
        <div>
          <span className="text-slate-500">Proposed package</span>
          <div className="font-medium text-slate-900">{formatPackage(routing?.proposed)}</div>
        </div>
        <div>
          <span className="text-slate-500">Approved package</span>
          <div className="font-medium text-slate-900">{formatPackage(routing?.approved)}</div>
        </div>
        <div>
          <span className="text-slate-500">Requested route</span>
          <div className="font-medium text-slate-900">{run.requested_route_id ?? "—"}</div>
        </div>
        <div>
          <span className="text-slate-500">Execution requested</span>
          <div className="font-medium text-slate-900">
            {run.selected_provider
              ? `${run.selected_provider}/${run.selected_model ?? "?"}`
              : (run.selected_model ?? "—")}
          </div>
        </div>
        <div>
          <span className="text-slate-500">Executed provider/model</span>
          <div className="font-medium text-slate-900">
            {run.actual_provider && run.actual_provider_model
              ? `${run.actual_provider}/${run.actual_provider_model}`
              : "Unavailable"}
          </div>
        </div>
        <div>
          <span className="text-slate-500">Execution provenance</span>
          <div className="font-medium text-slate-900">
            {run.actual_provenance_verified ? "Verified" : "Unverified"}
          </div>
        </div>
        <div>
          <span className="text-slate-500">Actual harness</span>
          <div className="font-medium text-slate-900">{run.harness_id ?? "—"}</div>
        </div>
        <div>
          <span className="text-slate-500">Fallback / substitution</span>
          <div className="font-medium text-slate-900">
            {run.fallback_used === null ? "Unavailable" : run.fallback_used ? "yes" : "no"}
          </div>
        </div>
        <div>
          <span className="text-slate-500">Duration</span>
          <div className="font-medium text-slate-900">{formatDuration(run.duration_ms)}</div>
        </div>
        <div>
          <span className="text-slate-500">Tokens (in/out)</span>
          <div className="font-medium text-slate-900">
            {formatTokens(run.input_tokens)} / {formatTokens(run.output_tokens)}
          </div>
        </div>
      </div>

      <ObjectiveEvidence run={run} />

      {evaluation ? (
        <LlmEvaluation evaluation={evaluation} />
      ) : (
        <div className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
          Automated evaluation pending — review will be reviewable later.
        </div>
      )}
    </div>
  );
}

function ObjectiveEvidence({ run }: { run: TaskRun }) {
  const items: Array<{ label: string; value: string }> = [];
  if (run.commit_sha) {
    items.push({
      label: "Commit",
      value: run.commit_sha.slice(0, 12),
    });
  }
  if (run.changed_files && run.changed_files.length > 0) {
    items.push({
      label: "Changed files",
      value: `${run.changed_files.length}`,
    });
  }
  if (run.total_cost_usd !== null && run.total_cost_usd !== undefined) {
    items.push({ label: "Cost", value: formatCost(run.total_cost_usd) });
  }
  if (run.failure_error_code) {
    items.push({
      label: "Failure",
      value: run.failure_error_message ?? run.failure_error_code,
    });
  }
  if (items.length === 0) {
    return <div className="text-xs text-slate-500">No objective evidence available.</div>;
  }
  return (
    <div className="rounded-md border border-slate-100 bg-slate-50 px-3 py-2 text-xs">
      <div className="mb-1 text-slate-500">Available objective evidence</div>
      <ul className="space-y-0.5">
        {items.map((it) => (
          <li key={it.label} className="flex gap-2">
            <span className="w-24 shrink-0 text-slate-500">{it.label}</span>
            <span className="font-mono text-slate-800">{it.value}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function LlmEvaluation({ evaluation }: { evaluation: TaskEvaluation }) {
  const isInconclusive = evaluation.verdict === "inconclusive";
  return (
    <div
      className={`rounded-md border px-3 py-2 text-xs ${isInconclusive ? "border-amber-200 bg-amber-50 text-amber-900" : "border-slate-200 bg-slate-50 text-slate-800"}`}
    >
      <div className="mb-1 flex items-center gap-2 text-slate-500">
        <span>LLM evaluation</span>
        <span
          className={`rounded-full px-2 py-0.5 text-[11px] font-medium ${verdictColor(evaluation.verdict)}`}
        >
          {evaluation.verdict}
        </span>
        {evaluation.confidence !== null && evaluation.confidence !== undefined && (
          <span className="text-slate-500">
            confidence {Math.round(evaluation.confidence * 100)}%
          </span>
        )}
        {evaluation.quality_score !== null && evaluation.quality_score !== undefined && (
          <span className="text-slate-500">quality {evaluation.quality_score}/5</span>
        )}
      </div>
      {evaluation.proposed_task_family && (
        <div className="text-slate-500">
          proposed family:{" "}
          <span className="font-medium text-slate-900">{evaluation.proposed_task_family}</span>
        </div>
      )}
      {evaluation.reasoning && <p className="mt-1 text-slate-700">{evaluation.reasoning}</p>}
      {evaluation.evidence && evaluation.evidence.length > 0 && (
        <ul className="mt-1 list-disc pl-4 text-slate-600">
          {evaluation.evidence.map((e) => (
            <li key={e}>{e}</li>
          ))}
        </ul>
      )}
      {evaluation.unresolved_issues && evaluation.unresolved_issues.length > 0 && (
        <div className="mt-1 text-rose-700">
          <span className="font-medium">Unresolved:</span>
          <ul className="list-disc pl-4">
            {evaluation.unresolved_issues.map((u) => (
              <li key={u}>{u}</li>
            ))}
          </ul>
        </div>
      )}
      {isInconclusive && (
        <p className="mt-1 italic text-amber-800">
          Automated evaluation unavailable — review still required.
        </p>
      )}
    </div>
  );
}

function TaskReviewForm({
  outcome,
  setOutcome,
  qualityScore,
  setQualityScore,
  finalFamily,
  setFinalFamily,
  evaluatorAccuracy,
  setEvaluatorAccuracy,
  routeFit,
  setRouteFit,
  failureAttribution,
  setFailureAttribution,
  preferredRoute,
  setPreferredRoute,
  preferredEffort,
  setPreferredEffort,
  comments,
  setComments,
  saving,
  onSave,
  onSkip,
  existingReview,
}: {
  outcome: TaskReview["verdict"] | "";
  setOutcome: (v: TaskReview["verdict"] | "") => void;
  qualityScore: number | "";
  setQualityScore: (v: number | "") => void;
  finalFamily: string;
  setFinalFamily: (v: string) => void;
  evaluatorAccuracy: string;
  setEvaluatorAccuracy: (v: string) => void;
  routeFit: string;
  setRouteFit: (v: string) => void;
  failureAttribution: string;
  setFailureAttribution: (v: string) => void;
  preferredRoute: string;
  setPreferredRoute: (v: string) => void;
  preferredEffort: string;
  setPreferredEffort: (v: string) => void;
  comments: string;
  setComments: (v: string) => void;
  saving: boolean;
  onSave: () => void;
  onSkip: () => void;
  existingReview: TaskReview | null;
}) {
  const isUpdate = existingReview !== null;
  return (
    <div className="mt-3 grid gap-3 border-t border-slate-100 pt-3">
      <div>
        <div className="mb-1 text-xs font-medium text-slate-600">Actual outcome</div>
        <div className="flex flex-wrap gap-2">
          {OUTCOME_OPTIONS.map((opt) => (
            <label
              key={opt.value}
              className={`flex cursor-pointer items-center gap-1 rounded-md border px-2 py-1 text-xs ${outcome === opt.value ? "border-blue-500 bg-blue-50 text-blue-700" : "border-slate-200 text-slate-700"}`}
            >
              <input
                type="radio"
                name="outcome"
                value={opt.value}
                checked={outcome === opt.value}
                onChange={() => setOutcome(opt.value)}
                className="sr-only"
              />
              {opt.label}
            </label>
          ))}
        </div>
      </div>

      <div>
        <div className="mb-1 text-xs font-medium text-slate-600">Quality (1–5, optional)</div>
        <div className="flex gap-1">
          {[1, 2, 3, 4, 5].map((n) => (
            <button
              type="button"
              key={n}
              onClick={() => {
                setQualityScore(qualityScore === n ? "" : n);
              }}
              aria-label={`Quality ${n}`}
              aria-pressed={qualityScore === n}
              className={`flex min-h-11 min-w-11 items-center justify-center rounded-md border text-xs ${qualityScore === n ? "border-blue-500 bg-blue-50 text-blue-700" : "border-slate-200 text-slate-700"}`}
            >
              {n}
            </button>
          ))}
        </div>
      </div>

      <div>
        <div className="mb-1 text-xs font-medium text-slate-600">
          Was the LLM evaluation accurate?
        </div>
        <div className="flex flex-wrap gap-2">
          {EVALUATOR_ACCURACY_VALUES.map((opt) => (
            <label
              key={opt}
              className={`flex cursor-pointer items-center gap-1 rounded-md border px-2 py-1 text-xs ${evaluatorAccuracy === opt ? "border-blue-500 bg-blue-50 text-blue-700" : "border-slate-200 text-slate-700"}`}
            >
              <input
                type="radio"
                name="evaluatorAccuracy"
                value={opt}
                checked={evaluatorAccuracy === opt}
                onChange={() => setEvaluatorAccuracy(opt)}
                className="sr-only"
              />
              {opt.replace("_", " ")}
            </label>
          ))}
        </div>
      </div>

      <div>
        <div className="mb-1 text-xs font-medium text-slate-600">
          Route fit (separate from task success)
        </div>
        <select
          aria-label="Route fit"
          value={routeFit}
          onChange={(e) => setRouteFit(e.target.value)}
          className="rounded-md border border-slate-200 px-2 py-1 text-xs"
        >
          <option value="">— not specified —</option>
          {["appropriate", "too_weak", "overkill", "wrong_capability", "unsure"].map((value) => (
            <option key={value} value={value}>
              {value.replace("_", " ")}
            </option>
          ))}
        </select>
      </div>

      <div>
        <div className="mb-1 text-xs font-medium text-slate-600">
          Failure attribution (when applicable)
        </div>
        <select
          aria-label="Failure attribution"
          value={failureAttribution}
          onChange={(e) => setFailureAttribution(e.target.value)}
          className="rounded-md border border-slate-200 px-2 py-1 text-xs"
        >
          <option value="">— not specified —</option>
          {[
            "router",
            "model",
            "harness",
            "environment",
            "permissions",
            "task_definition",
            "external_service",
            "unknown",
          ].map((value) => (
            <option key={value} value={value}>
              {value.replace("_", " ")}
            </option>
          ))}
        </select>
      </div>

      <div>
        <div className="mb-1 text-xs font-medium text-slate-600">
          Preferred correction (optional)
        </div>
        <div className="flex flex-wrap gap-2">
          <input
            aria-label="Preferred route"
            value={preferredRoute}
            onChange={(e) => setPreferredRoute(e.target.value)}
            maxLength={64}
            className="rounded-md border border-slate-200 px-2 py-1 text-xs"
            placeholder="OmniRoute route ID"
          />
          <select
            aria-label="Preferred reasoning effort"
            value={preferredEffort}
            onChange={(e) => setPreferredEffort(e.target.value)}
            className="rounded-md border border-slate-200 px-2 py-1 text-xs"
          >
            <option value="">reasoning effort</option>
            {["none", "minimal", "low", "medium", "high", "xhigh", "max"].map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
        </div>
      </div>

      <div>
        <div className="mb-1 text-xs font-medium text-slate-600">Task family (corrected)</div>
        <select
          aria-label="Corrected task family"
          value={finalFamily}
          onChange={(e) => setFinalFamily(e.target.value)}
          className="rounded-md border border-slate-200 px-2 py-1 text-xs"
        >
          <option value="">— keep proposed —</option>
          {TASK_FAMILIES.map((f) => (
            <option key={f} value={f}>
              {f}
            </option>
          ))}
        </select>
      </div>

      <div>
        <div className="mb-1 text-xs font-medium text-slate-600">Comments</div>
        <textarea
          aria-label="Review comments"
          value={comments}
          onChange={(e) => setComments(e.target.value)}
          rows={3}
          className="w-full rounded-md border border-slate-200 px-2 py-1 text-xs"
          placeholder="Optional notes about what worked or didn't."
        />
      </div>

      <div className="flex items-center justify-end gap-2">
        <button
          type="button"
          onClick={onSkip}
          disabled={saving}
          className="inline-flex min-h-11 w-full items-center justify-center gap-1 rounded-md border border-slate-200 px-3 py-1.5 text-xs text-slate-700 hover:bg-slate-50 disabled:opacity-50 sm:w-auto"
        >
          <XIcon className="h-3 w-3" />
          Review later
        </button>
        <button
          type="button"
          onClick={onSave}
          disabled={saving || outcome === ""}
          className="inline-flex min-h-11 w-full items-center justify-center gap-1 rounded-md bg-blue-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-blue-700 disabled:opacity-50 sm:w-auto"
        >
          <CheckIcon className="h-3 w-3" />
          {isUpdate ? "Update review" : "Save review"}
        </button>
      </div>
    </div>
  );
}
