import { useMemo, useState } from "react";

import { Button } from "@/components/ui/button";
import { authenticatedFetch } from "@/lib/identity";
import type { O3RoutingProposal } from "@/lib/o3RoutingReview";

interface TB4FloorRecord {
  status?: string;
  policy_version?: string;
  user_floor_percent?: number;
  adviser_floor_percent?: number;
  assigned_arm?: "user" | "adviser" | "same";
  assignment_propensity?: number;
  executed_floor_percent?: number;
  adviser_confidence?: number;
  adviser_rationale?: string;
  baseline_policy_version?: string;
  eligible_count?: number;
}

function recordFor(proposal: O3RoutingProposal): TB4FloorRecord | null {
  const raw = proposal.audit?.tb4_floor_experiment;
  return raw && typeof raw === "object" ? (raw as TB4FloorRecord) : null;
}

export function tb4FloorApplied(proposal: O3RoutingProposal): boolean {
  return recordFor(proposal)?.status === "applied";
}

export function TB4FloorExperimentControl({
  proposal,
  onProposalChange,
}: {
  proposal: O3RoutingProposal;
  onProposalChange: (proposal: O3RoutingProposal) => void;
}) {
  const record = useMemo(() => recordFor(proposal), [proposal]);
  const [floor, setFloor] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (record?.status === "applied") {
    const arm =
      record.assigned_arm === "same"
        ? "Same floor"
        : record.assigned_arm === "user"
          ? "Your floor"
          : "Adviser floor";
    return (
      <div
        className="rounded-md border border-border bg-muted/20 px-3 py-2 text-sm"
        data-testid="tb4-floor-assignment"
      >
        <div className="flex flex-wrap gap-x-4 gap-y-1">
          <span>
            Your TB4 floor: <strong>{record.user_floor_percent}%</strong>
          </span>
          <span>
            Adviser: <strong>{record.adviser_floor_percent}%</strong>
          </span>
          <span>
            Executed: <strong>{arm} · {record.executed_floor_percent}%</strong>
          </span>
        </div>
        <p className="mt-1 text-xs text-muted-foreground">
          {record.assigned_arm === "same"
            ? "Both independently selected the same floor."
            : `50/50 assignment · propensity ${record.assignment_propensity ?? 0.5}`}
          {typeof record.eligible_count === "number"
            ? ` · ${record.eligible_count} eligible configurations`
            : ""}
        </p>
        {record.adviser_rationale && (
          <details className="mt-1 text-xs text-muted-foreground">
            <summary className="cursor-pointer">Adviser rationale</summary>
            <p className="mt-1">{record.adviser_rationale}</p>
          </details>
        )}
      </div>
    );
  }

  const numeric = Number(floor);
  const valid = floor.trim() !== "" && Number.isFinite(numeric) && numeric >= 0 && numeric <= 100;

  async function apply(): Promise<void> {
    if (!valid || busy) return;
    setBusy(true);
    setError(null);
    try {
      const response = await authenticatedFetch(
        `/v1/o3/routing-review/proposals/${encodeURIComponent(proposal.proposal_id)}/floor-experiment`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ user_floor_percent: numeric }),
        },
      );
      if (!response.ok) {
        let message = "TB4 floor could not be applied";
        try {
          const body = (await response.json()) as { detail?: string; error?: { message?: string } };
          message = body.error?.message ?? body.detail ?? message;
        } catch {
          // Preserve the generic error for non-JSON responses.
        }
        throw new Error(message);
      }
      onProposalChange((await response.json()) as O3RoutingProposal);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "TB4 floor could not be applied");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div
      className="rounded-md border border-border bg-muted/20 px-3 py-3"
      data-testid="tb4-floor-input"
    >
      <div className="flex flex-col gap-1">
        <span className="text-sm font-semibold">Choose your exact TB4 floor</span>
        <p className="text-xs text-muted-foreground">
          Enter the minimum Terminal-Bench 4 score you think this task needs. The adviser chooses
          its own numeric floor independently and remains hidden until your choice is committed.
        </p>
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-2">
        <label className="flex items-center gap-2 text-sm">
          TB4 ≥
          <input
            type="number"
            min="0"
            max="100"
            step="0.1"
            inputMode="decimal"
            value={floor}
            disabled={busy}
            aria-label="Your TB4 floor"
            className="h-10 w-24 rounded-md border border-input bg-background px-2 tabular-nums"
            onChange={(event) => setFloor(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && valid) void apply();
            }}
          />
          %
        </label>
        <Button type="button" size="sm" disabled={!valid || busy} onClick={() => void apply()}>
          {busy ? "Applying…" : "Commit floor"}
        </Button>
      </div>
      {error && (
        <p className="mt-2 text-xs text-destructive" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}
