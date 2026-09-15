import { useMemo, useState, type ReactNode } from "react";
import { Dialog, DialogContent, DialogTitle, DialogDescription } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { copyText } from "@/lib/clipboard";
import type { O3RoutingProposal } from "@/lib/o3RoutingReview";

const missing = "Not recorded for this review";
const pct = (n: number) => `${Number((n * 100).toFixed(3))}%`;

export function RawAudit({ value, label }: { value: unknown; label: string }) {
  const [copyState, setCopyState] = useState("Copy");
  const text =
    value == null ? missing : typeof value === "string" ? value : JSON.stringify(value, null, 2);
  return (
    <div className="min-w-0">
      <div className="flex items-center justify-between gap-2 border-b py-1">
        <span className="text-xs text-muted-foreground">{label}</span>
        <Button
          variant="ghost"
          className="min-h-11 px-3"
          disabled={value == null}
          onClick={() => {
            void copyText(text).then(
              () => setCopyState("Copied"),
              () => setCopyState("Copy failed"),
            );
          }}
        >
          {copyState}
        </Button>
      </div>
      <pre
        tabIndex={0}
        aria-label={label}
        className="max-h-[55dvh] overflow-auto overscroll-contain whitespace-pre-wrap break-words rounded-sm bg-muted/40 p-2 font-mono text-xs leading-relaxed [overflow-wrap:anywhere]"
      >
        {text}
      </pre>
    </div>
  );
}

function Section({
  title,
  children,
  open = false,
}: {
  title: string;
  children: ReactNode;
  open?: boolean;
}) {
  const [expanded, setExpanded] = useState(open);
  return (
    <details open={expanded} className="group min-w-0 border-b border-border">
      <summary
        onClick={(event) => {
          event.preventDefault();
          setExpanded((value) => !value);
        }}
        className="min-h-12 cursor-pointer py-3 text-sm font-medium marker:text-muted-foreground"
      >
        {title}
      </summary>
      {expanded && <div className="min-w-0 space-y-2 pb-3 text-sm">{children}</div>}
    </details>
  );
}

function Rows({ rows }: { rows: [string, unknown][] }) {
  return (
    <dl className="grid grid-cols-[minmax(90px,1fr)_minmax(0,2fr)] gap-x-3 gap-y-1.5 text-xs">
      {rows.map(([name, value]) => (
        <div key={name} className="contents">
          <dt className="text-muted-foreground">{name}</dt>
          <dd className="min-w-0 break-words [overflow-wrap:anywhere]">
            {value == null
              ? missing
              : typeof value === "object"
                ? JSON.stringify(value)
                : String(value)}
          </dd>
        </div>
      ))}
    </dl>
  );
}

export function O3DecisionSummary({ proposal }: { proposal: O3RoutingProposal }) {
  const [open, setOpen] = useState(false);
  const set = proposal.recommendation?.execution_set;
  const eligible =
    set?.eligible_count ?? proposal.evaluations.filter((e) => e.status !== "excluded").length;
  const excluded =
    set?.excluded.length ?? proposal.evaluations.filter((e) => e.status === "excluded").length;
  const reviewer = proposal.adviser_exchanges?.at(-1);
  return (
    <div className="min-w-0 text-sm" data-testid="o3-decision-summary">
      <div className="flex items-baseline justify-between gap-2">
        <h2 className="font-semibold">O3 Review</h2>
        <span className="text-xs capitalize text-muted-foreground">
          {proposal.approved_constraints.difficulty} · {proposal.approved_constraints.risk} risk
        </span>
      </div>
      <p className="mt-1 truncate font-medium" title={proposal.selected_execution?.route}>
        {proposal.selected_execution
          ? `Selected: ${proposal.selected_execution.route}`
          : `${proposal.decision === "approve" ? "Approved" : "Eligible"}: ${eligible} routes`}
      </p>
      <p className="mt-1 break-words text-xs text-muted-foreground">
        {proposal.adviser_mode === "local_rule"
          ? "Local rule · no model call"
          : `Reviewer: ${reviewer?.actual_model ?? reviewer?.requested_model ?? "Not recorded"}${reviewer ? ` · ${reviewer.reasoning_effort}` : ""}`}
      </p>
      <div className="mt-1 flex flex-wrap items-center justify-between gap-x-3 text-xs text-muted-foreground">
        <span>
          {proposal.approved_constraints.benchmark.slice_id} ≥{" "}
          {pct(proposal.approved_constraints.benchmark.minimum_score)}
        </span>
        <span>
          {eligible} eligible · {excluded} excluded
        </span>
      </div>
      <Button variant="ghost" className="mt-1 min-h-11 px-0 text-sm" onClick={() => setOpen(true)}>
        Inspect decision
      </Button>
      <O3DecisionInspect proposal={proposal} open={open} onOpenChange={setOpen} />
    </div>
  );
}

function Candidates({ proposal }: { proposal: O3RoutingProposal }) {
  const [filter, setFilter] = useState("");
  const [status, setStatus] = useState("all");
  const [sort, setSort] = useState("eligible");
  const [selected, setSelected] = useState<unknown>(null);
  const rows = useMemo(() => {
    const set = proposal.recommendation?.execution_set;
    const values = set
      ? [...set.eligible, ...set.excluded].map((c) => ({
          id: c.route_id,
          provider: c.provider_id,
          score: c.capability_score_lower,
          eligible: c.exclusions.length === 0,
          reasons: c.exclusions,
          context: (c.metadata?.forecast as Record<string, unknown> | undefined)?.context_window,
          evidence: c,
        }))
      : proposal.evaluations.map((c) => ({
          id: c.candidate.catalogue_model_id,
          provider: c.candidate.provider_id,
          score: c.admission_score == null ? null : c.admission_score * 100,
          eligible: c.status !== "excluded",
          reasons: c.exclusions,
          context: c.candidate.context_tokens,
          evidence: c,
        }));
    return values
      .filter(
        (c) =>
          (status === "all" || c.eligible === (status === "eligible")) &&
          `${c.id} ${c.provider} ${c.reasons.join(" ")}`
            .toLowerCase()
            .includes(filter.toLowerCase()),
      )
      .sort((a, b) =>
        sort === "score"
          ? (b.score ?? -1) - (a.score ?? -1)
          : sort === "provider"
            ? a.provider.localeCompare(b.provider)
            : sort === "model"
              ? a.id.localeCompare(b.id)
              : Number(b.eligible) - Number(a.eligible) || a.id.localeCompare(b.id),
      );
  }, [proposal, filter, status, sort]);
  return (
    <>
      <div className="flex flex-wrap gap-2">
        <input
          aria-label="Filter candidates"
          placeholder="Model, provider or exclusion…"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          className="h-11 min-w-0 flex-1 rounded border bg-background px-2 text-sm"
        />
        <select
          aria-label="Candidate status"
          className="h-11 rounded border bg-background px-2 text-xs"
          value={status}
          onChange={(e) => setStatus(e.target.value)}
        >
          <option value="all">All</option>
          <option value="eligible">Eligible</option>
          <option value="excluded">Excluded</option>
        </select>
        <select
          aria-label="Sort candidates"
          className="h-11 rounded border bg-background px-2 text-xs"
          value={sort}
          onChange={(e) => setSort(e.target.value)}
        >
          <option value="eligible">Eligible first</option>
          <option value="score">Score</option>
          <option value="provider">Provider</option>
          <option value="model">Model</option>
        </select>
      </div>
      <p className="text-xs text-muted-foreground">
        {rows.length} candidates ·{" "}
        {proposal.recommendation
          ? "Conservative common capability / 100; proxy evidence, not a raw benchmark score."
          : "Benchmark admission score (%)."}{" "}
        Tap a model for all recorded metadata.
      </p>
      <div
        className="max-h-[55dvh] overflow-auto overscroll-contain"
        tabIndex={0}
        aria-label="Candidate evaluation"
      >
        <table
          className="w-full table-fixed border-collapse text-left text-xs"
          data-testid="o3-candidate-table"
        >
          <thead className="sticky top-0 bg-popover">
            <tr>
              <th className="w-[48%] py-2 pr-2 font-medium">Model / provider</th>
              <th className="w-[18%] py-2 font-medium">Score</th>
              <th className="py-2 font-medium">Result</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((c) => (
              <tr key={c.id} className="border-t align-top">
                <td className="break-words pr-2 [overflow-wrap:anywhere]">
                  <button
                    type="button"
                    className="min-h-11 py-2 text-left underline decoration-border underline-offset-4"
                    onClick={() => setSelected(c.evidence)}
                  >
                    {c.id}
                    <span className="block text-muted-foreground">
                      {c.provider} ·{" "}
                      {c.context == null
                        ? "context unknown"
                        : `${Number(c.context).toLocaleString()} ctx`}
                    </span>
                  </button>
                </td>
                <td className="py-2 tabular-nums">
                  {c.score == null ? "Unknown" : Number(c.score.toFixed(2))}
                </td>
                <td className="break-words py-2">
                  <span
                    className={
                      c.eligible
                        ? "text-emerald-600 dark:text-emerald-400"
                        : "text-muted-foreground"
                    }
                  >
                    {c.eligible ? "Eligible" : "Excluded"}
                  </span>
                  {c.reasons.map((reason) => (
                    <p className="mt-1" key={reason}>
                      {reason}
                    </p>
                  ))}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {selected != null && (
        <div className="border-t">
          <Button variant="ghost" className="min-h-11" onClick={() => setSelected(null)}>
            Close candidate evidence
          </Button>
          <RawAudit label="Candidate metadata and checks" value={selected} />
        </div>
      )}
    </>
  );
}

export function O3DecisionInspect({
  proposal: p,
  open,
  onOpenChange,
}: {
  proposal: O3RoutingProposal;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const exchanges = p.adviser_exchanges ?? [];
  const constraints = p.approved_constraints;
  const original = p.original_adviser;
  const requirements = p.effective_requirements ?? p.adviser.requirements;
  const overrideKeys = new Set(Object.keys(p.requirement_overrides ?? {}));
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className="bg-background flex h-dvh max-h-dvh w-full max-w-full flex-col gap-0 rounded-none p-0 sm:h-[90dvh] sm:max-w-5xl sm:rounded-xl"
        aria-describedby="o3-inspect-description"
      >
        <div className="shrink-0 border-b px-4 pb-3 pt-[max(1rem,env(safe-area-inset-top))] pr-16">
          <DialogTitle>Inspect decision</DialogTitle>
          <DialogDescription id="o3-inspect-description" className="mt-1 text-xs">
            O3 Review · {constraints.benchmark.slice_id} ≥{" "}
            {pct(constraints.benchmark.minimum_score)}
          </DialogDescription>
        </div>
        <div
          className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-4 pb-[max(1rem,env(safe-area-inset-bottom))]"
          data-testid="o3-inspect-scroll"
        >
          <Section title="Overview" open>
            <Rows
              rows={[
                ["Classification", p.adviser.task_classification],
                ["Difficulty / risk", `${constraints.difficulty} / ${constraints.risk}`],
                ["Decision", p.decision ?? "Awaiting approval"],
                [
                  "O3 eligible set",
                  p.recommendation?.execution_set?.eligible_count ??
                    p.evaluations.filter((c) => c.status !== "excluded").length,
                ],
                ["Selected execution", p.selected_execution?.route],
                ["Execution observed", p.actual_model],
              ]}
            />
          </Section>
          <Section title="Reviewer">
            {p.adviser_mode === "local_rule" ? (
              <Rows
                rows={[
                  ["Source", "Local rule · no model call"],
                  ["Rule", p.audit?.rule],
                ]}
              />
            ) : exchanges.length ? (
              exchanges.map((e) => (
                <div key={e.attempt} className="border-b pb-3">
                  <Rows
                    rows={[
                      ["Attempt", e.attempt],
                      ["Requested model", e.requested_model],
                      ["Transmitted to gateway", e.transmitted_model],
                      ["Observed model", e.actual_model],
                      ["Observed provider", e.actual_provider],
                      ["Harness / transport", e.harness],
                      ["Requested effort", e.reasoning_effort],
                      ["Transmitted effort", e.transmitted_effort],
                      ["Observed effort", e.observed_effort],
                      [
                        "Latency",
                        e.duration_ms == null ? null : `${(e.duration_ms / 1000).toFixed(2)} s`,
                      ],
                      ["Usage returned", e.response?.usage],
                      ["Parse result", e.parse_error ?? (e.parsed ? "Valid" : missing)],
                    ]}
                  />
                </div>
              ))
            ) : (
              <p>{missing}</p>
            )}
          </Section>
          <Section title="Reviewer input">
            {p.adviser_mode === "local_rule" ? (
              <>
                <p>No reviewer invocation.</p>
                <RawAudit label="Local rule input" value={p.audit?.input} />
              </>
            ) : exchanges.length ? (
              exchanges.map((e) => (
                <RawAudit
                  key={e.attempt}
                  label={`Attempt ${e.attempt} · exact safe request`}
                  value={e.request}
                />
              ))
            ) : (
              <p>{missing}</p>
            )}
            <p className="text-xs text-muted-foreground">
              Captured at the Omnigent → OmniRoute boundary. Upstream rewrites are shown only when
              returned.
            </p>
          </Section>
          <Section title="Raw reviewer output">
            {p.adviser_mode === "local_rule" ? (
              <p>No model call; no reviewer output.</p>
            ) : exchanges.length ? (
              exchanges.map((e) => (
                <div key={e.attempt} className="space-y-2">
                  <RawAudit
                    label={`Attempt ${e.attempt} · raw response verbatim`}
                    value={e.response_text ?? e.response}
                  />
                  <RawAudit label="Complete response object" value={e.response} />
                  <RawAudit label="Returned response headers" value={e.response_headers} />
                  {e.parse_error && <p className="text-amber-600">{e.parse_error}</p>}
                </div>
              ))
            ) : (
              <p>{missing}</p>
            )}
          </Section>
          <Section title="Parsed review">
            <RawAudit label="Original parsed recommendation" value={original} />
            <RawAudit label="Current structured review" value={p.adviser} />
          </Section>
          <Section title="Benchmark & calibration">
            <Rows
              rows={[
                ["Benchmark", constraints.benchmark.slice_id],
                ["Difficulty", constraints.difficulty],
                ["Effective raw floor", pct(constraints.benchmark.minimum_score)],
                ["Calibration version", constraints.calibration_version],
                ["Common capability floor", p.recommendation?.common_capability_floor],
                ["Mapping rule", p.audit?.calibration_rule],
              ]}
            />
            <RawAudit label="Original constraints" value={p.audit?.initial_constraints} />
            <RawAudit label="Exact calibration thresholds" value={p.audit?.calibration} />
            <RawAudit label="Common capability normalization" value={p.audit?.normalization} />
          </Section>
          <Section title="Requirements">
            <Rows
              rows={Object.entries(requirements).map(([key, value]) => {
                const alias = key === "vision" ? "image_input" : key;
                return [
                  key.replaceAll("_", " "),
                  `${JSON.stringify(value)} — ${overrideKeys.has(alias) ? "user override" : p.adviser_mode === "local_rule" ? "local rule" : original ? String((p.audit?.requirement_sources as Record<string, string> | undefined)?.[key] ?? "source not recorded") : "source not recorded"}`,
                ];
              })}
            />
            <RawAudit label="Explicit user overrides" value={p.requirement_overrides} />
          </Section>
          <Section title="Candidates">
            <Candidates proposal={p} />
          </Section>
          <Section title="Selection trace">
            <p className="text-xs">
              {p.selected_execution
                ? "O3 selected the execution option shown below."
                : "O3 passes an approved set to OmniRoute. Eligibility does not identify a single execution model."}
            </p>
            <RawAudit label="Selection policy" value={p.audit?.selection_policy} />
            <RawAudit label="Reviewer availability checks" value={p.audit?.reviewer_route_checks} />
            <RawAudit label="Approval availability checks" value={p.audit?.approval_route_checks} />
            <RawAudit label="Selected execution option" value={p.selected_execution} />
            <RawAudit label="Actual approved Combo definition" value={p.derived_combo_definition} />
            <RawAudit
              label="Resource snapshot / advice"
              value={{ snapshot: p.resource_snapshot, advice: p.resource_advice }}
            />
          </Section>
          <Section title="Execution / OmniRoute">
            <Rows
              rows={[
                ["Requested Combo", p.derived_combo_name],
                ["Observed provider", p.actual_provider],
                ["Observed model", p.actual_model],
                ["Observed effort", p.actual_reasoning_effort],
                ["Status", p.execution_status],
              ]}
            />
            <RawAudit
              label="Execution records (requested and returned facts)"
              value={p.execution_provenance}
            />
            <RawAudit label="Post-selection exclusions" value={p.execution_exclusions} />
            <RawAudit label="Tool-free execution provenance" value={p.tool_free_provenance} />
            <p className="text-xs text-muted-foreground">
              Unreported upstream transmission, fallback reasons and internal reasoning remain
              unknown.
            </p>
          </Section>
          <Section title="Provenance">
            <Rows
              rows={[
                ["Review ID", p.proposal_id],
                ["Created", p.created_at],
                ["Updated", p.updated_at],
                ["Session", p.session_id],
                ["Constraint version", p.constraint_version],
              ]}
            />
            <RawAudit label="Decision revisions · before and after" value={p.audit?.history} />
            <Section title="Complete persisted decision">
              <RawAudit label="Full decision JSON" value={p} />
            </Section>
          </Section>
        </div>
      </DialogContent>
    </Dialog>
  );
}
