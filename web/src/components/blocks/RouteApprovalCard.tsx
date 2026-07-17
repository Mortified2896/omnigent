// Approved-route-proposal card. Rendered in place of the pending
// elicitation once the user accepts (or rejects) a route-proposal
// elicitation. The compact summary above the fold is the minimum the
// operator must still be able to read in a one-line glance — harness,
// route id, reasoning, fallback, billing class — so the card stays
// useful after approval without expanding. A toggle reveals the full
// `RouteProposalCard` contents (rationale, risk note, evaluator
// metadata, selection strategy, decision id, …) when more depth is
// needed. The expansion state is local to the card; the persisted
// approval data is the source of truth across reloads, so this view
// always re-renders against the same stored payload.

import { useState } from "react";
import { ChevronDownIcon, ChevronRightIcon, CheckIcon } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { getOmniRouteComboDisplayName, isCuratedOmniRouteCombo } from "@/lib/omnirouteCombos";
import { RouteProposalCard } from "./RouteProposalCard";

export interface RouteApprovalCardProps {
  /** Persisted route-proposal payload. Same shape `RouteProposalCard` reads. */
  proposal: Record<string, unknown>;
  /**
   * Live approval verdict (when present). Drives the data-state testid;
   * the card always renders against `proposal`, never against this
   * string, so an approved/rejected resubmit can't lose details.
   */
  action: "accept" | "decline" | "auto_resolved" | "cancel";
  /** Stable identifier used to key React nodes across re-renders. */
  elicitationId: string;
  /**
   * Optional override of the lookup map used to resolve a combo id to a
   * curated display name. Defaults to the bundled curated map; callers
   * can pass a live-catalog-derived map so the card matches what the
   * picker showed when the user made the pick.
   */
  comboDisplayNames?: Record<string, string>;
}

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function list(value: unknown): string {
  return Array.isArray(value)
    ? value.filter((v): v is string => typeof v === "string").join(", ")
    : "";
}

export function RouteApprovalCard({
  proposal,
  action,
  elicitationId,
  comboDisplayNames,
}: RouteApprovalCardProps) {
  // Default-collapsed — the summary above carries the essential
  // selected route info the operator needs at a glance; the toggle is
  // there for "what was the rationale?" follow-ups.
  const [expanded, setExpanded] = useState(false);

  const labelMap: Record<RouteApprovalCardProps["action"], string> = {
    accept: "Approved",
    decline: "Rejected",
    auto_resolved: "Resolved elsewhere",
    cancel: "Cancelled",
  };

  // Essential selected-route summary the user wants to keep readable
  // even when collapsed. Each field falls back to "—" so a partial
  // proposal still renders something coherent — never an ellipsis
  // clip.
  const harness = text(proposal.recommended_harness) || "OpenCode Native";
  const routeId = text(proposal.omniroute_route_id);
  const lookup = comboDisplayNames ?? undefined;
  const routeDisplayName =
    (lookup && routeId && lookup[routeId]) || getOmniRouteComboDisplayName(routeId);
  const isCurated = isCuratedOmniRouteCombo(routeId);
  const reasoning = text(proposal.reasoning_effort);
  const permission = text(proposal.permission_mode);
  const billing =
    text(proposal.billing_summary) ||
    `${list(proposal.allowed_billing_classes)} allowed; ${list(proposal.forbidden_billing_classes)} forbidden`;
  const fallback = proposal.evaluator_fallback_used;
  const explicit = proposal.omniroute_requires_explicit_approval === true;

  const dataState =
    action === "accept"
      ? "approved"
      : action === "decline"
        ? "rejected"
        : action === "cancel"
          ? "cancelled"
          : "resolved-elsewhere";

  const detailsId = `route-approval-details-${elicitationId}`;

  return (
    <Alert
      data-testid="route-approval-card"
      data-state={dataState}
      data-elicitation-id={elicitationId}
      className="flex flex-col gap-2 border-muted"
    >
      <AlertTitle className="flex flex-wrap items-center gap-2 text-sm">
        {action === "accept" ? (
          <CheckIcon className="size-4 text-success" aria-hidden="true" />
        ) : null}
        <span data-testid="route-approval-card-label">{labelMap[action]}</span>
        {routeId ? (
          <span className="text-muted-foreground text-xs" data-testid="route-approval-route-id">
            · Provider OmniRoute
            {isCurated ? (
              <>
                {" "}
                · Route{" "}
                <span data-testid="route-approval-route-display-name">{routeDisplayName}</span>{" "}
                <code data-testid="route-approval-route-id-code">{routeId}</code>
              </>
            ) : (
              <>
                {" "}
                · Route <code data-testid="route-approval-route-id-code">{routeId}</code>
              </>
            )}
          </span>
        ) : null}
        <Button
          variant="ghost"
          size="sm"
          className="ml-auto h-7 px-2 text-xs"
          onClick={() => setExpanded((v) => !v)}
          aria-expanded={expanded}
          aria-controls={detailsId}
          data-testid="route-approval-details-toggle"
          data-expanded={expanded ? "true" : "false"}
        >
          {expanded ? (
            <ChevronDownIcon className="mr-1 size-3.5" aria-hidden="true" />
          ) : (
            <ChevronRightIcon className="mr-1 size-3.5" aria-hidden="true" />
          )}
          {expanded ? "Hide details" : "Show details"}
        </Button>
      </AlertTitle>
      <AlertDescription className="flex flex-col gap-1 text-xs text-foreground">
        {/* Summary: always visible, no truncation. Wraps on narrow
            viewports via the default whitespace handling. */}
        <div
          className="grid gap-1 text-xs sm:grid-cols-[max-content_1fr]"
          data-testid="route-approval-summary"
        >
          <div className="text-muted-foreground">Harness</div>
          <div className="break-words">{harness}</div>
          <div className="text-muted-foreground">Provider</div>
          <div className="break-words" data-testid="route-approval-provider">
            OmniRoute
          </div>
          <div className="text-muted-foreground">Route</div>
          <div className="break-words" data-testid="route-approval-route-cell">
            {isCurated && routeId ? (
              <>
                <span data-testid="route-approval-summary-route-display-name">
                  {routeDisplayName}
                </span>{" "}
                <code data-testid="route-approval-summary-route-id">{routeId}</code>
              </>
            ) : (
              <code data-testid="route-approval-summary-route-id">{routeId || "—"}</code>
            )}
          </div>
          {reasoning ? (
            <>
              <div className="text-muted-foreground">Reasoning effort</div>
              <div className="break-words">{reasoning}</div>
            </>
          ) : null}
          {permission ? (
            <>
              <div className="text-muted-foreground">Permission mode</div>
              <div className="break-words">{permission}</div>
            </>
          ) : null}
          {billing ? (
            <>
              <div className="text-muted-foreground">Billing</div>
              <div className="break-words">{billing}</div>
            </>
          ) : null}
          {typeof fallback === "boolean" ? (
            <>
              <div className="text-muted-foreground">Fallback used</div>
              <div className="break-words">{fallback ? "yes" : "no"}</div>
            </>
          ) : null}
          {explicit ? (
            <div className="col-span-full font-medium text-yellow-800 dark:text-yellow-300">
              Explicit approval required — pro/premium routing.
            </div>
          ) : null}
        </div>
        {/* Expanded: full original proposal contents. No truncate /
            line-clamp / fixed-height clipping — the operator asked
            for more, give them the whole payload. */}
        {expanded ? (
          <div
            id={detailsId}
            data-testid="route-approval-details"
            className="mt-2 overflow-visible"
          >
            <RouteProposalCard proposal={proposal} comboDisplayNames={comboDisplayNames} />
          </div>
        ) : null}
      </AlertDescription>
    </Alert>
  );
}
