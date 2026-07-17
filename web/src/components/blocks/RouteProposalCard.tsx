import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { getOmniRouteComboDisplayName, isCuratedOmniRouteCombo } from "@/lib/omnirouteCombos";

export interface RouteProposalCardProps {
  proposal: Record<string, unknown>;
  /**
   * Optional override of the lookup map used to resolve a combo id to a
   * curated display name. Defaults to the bundled
   * ``OMNIROUTE_COMBO_DISPLAY_NAMES``; callers that fetch a live catalog
   * can pass a richer map so the card mirrors what the picker showed.
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

export function RouteProposalCard({ proposal, comboDisplayNames }: RouteProposalCardProps) {
  const explicit = proposal.omniroute_requires_explicit_approval === true;
  const rationale = Array.isArray(proposal.rationale)
    ? proposal.rationale.filter((v): v is string => typeof v === "string")
    : [];
  const routeId = text(proposal.omniroute_route_id);
  const lookup = comboDisplayNames ?? undefined;
  const routeDisplayName =
    (lookup && routeId && lookup[routeId]) || getOmniRouteComboDisplayName(routeId);
  const isCurated = isCuratedOmniRouteCombo(routeId);
  return (
    <Alert
      data-testid="route-proposal-card"
      className="border-yellow-300 bg-yellow-50/40 dark:bg-yellow-950/10"
    >
      <AlertTitle>
        Proposal source: {text(proposal.proposal_source_label) || "Router recommendation"}
      </AlertTitle>
      <AlertDescription className="mt-2 flex flex-col gap-1 text-sm">
        <div>Harness: {text(proposal.recommended_harness) || "OpenCode Native"}</div>
        <div data-testid="route-proposal-provider">Provider: OmniRoute</div>
        <div data-testid="route-proposal-route">
          <span>Route: </span>
          {isCurated ? (
            <>
              <span data-testid="route-proposal-route-display-name">{routeDisplayName}</span>
              <span> </span>
              <code data-testid="route-proposal-route-id">{routeId}</code>
            </>
          ) : (
            <code data-testid="route-proposal-route-id">{routeId}</code>
          )}
        </div>
        <div>Reasoning effort: {text(proposal.reasoning_effort)}</div>
        <div>Permission mode: {text(proposal.permission_mode)}</div>
        <div>
          Billing:{" "}
          {text(proposal.billing_summary) ||
            `${list(proposal.allowed_billing_classes)} allowed; ${list(proposal.forbidden_billing_classes)} forbidden`}
        </div>
        {text(proposal.risk_note) && <div>Risk note: {text(proposal.risk_note)}</div>}
        {text(proposal.router_evaluator_route) && (
          <div>
            Evaluator route: <code>{text(proposal.router_evaluator_route)}</code>
          </div>
        )}
        {text(proposal.actual_evaluator_provider) && (
          <div>Selected provider: {text(proposal.actual_evaluator_provider)}</div>
        )}
        {text(proposal.actual_evaluator_model) && (
          <div>
            Selected model: <code>{text(proposal.actual_evaluator_model)}</code>
          </div>
        )}
        {text(proposal.evaluator_billing_class) && (
          <div>Evaluator billing: {text(proposal.evaluator_billing_class)}</div>
        )}
        {typeof proposal.evaluator_fallback_used === "boolean" && (
          <div>Evaluator fallback used: {proposal.evaluator_fallback_used ? "true" : "false"}</div>
        )}
        {text(proposal.evaluator_decision_id) && (
          <div>Decision ID: {text(proposal.evaluator_decision_id)}</div>
        )}
        {text(proposal.evaluator_selection_strategy) && (
          <div>Selection strategy: {text(proposal.evaluator_selection_strategy)}</div>
        )}
        {explicit && (
          <div className="font-medium text-yellow-800 dark:text-yellow-300">
            Explicit approval required — this route may use pro/premium routing and should only be
            used for hard tasks.
          </div>
        )}
        {rationale.length > 0 && (
          <ul className="list-disc pl-5">
            {rationale.map((entry) => (
              <li key={entry}>{entry}</li>
            ))}
          </ul>
        )}
      </AlertDescription>
    </Alert>
  );
}
