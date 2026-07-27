import { useId, useState } from "react";
import { CheckIcon, ChevronDownIcon, ChevronRightIcon, PencilIcon, XIcon } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { RouteProposalCard } from "./RouteProposalCard";

type RouteAction = "accept" | "decline" | "auto_resolved" | "cancel";
type SubmitRouteApproval = (
  action: "accept" | "decline",
  content?: Record<string, unknown>,
) => void | Promise<void>;

export interface RouteApprovalCardProps {
  proposal: Record<string, unknown>;
  action: RouteAction;
  elicitationId: string;
  responseContent?: Record<string, unknown>;
}

export interface PendingRouteApprovalCardProps {
  proposal: Record<string, unknown>;
  elicitationId: string;
  onSubmit: SubmitRouteApproval;
}

interface RouteSelection {
  route: string;
  reasoning: string;
  permission: string;
}

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function strings(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return [
    ...new Set(value.filter((entry): entry is string => typeof entry === "string" && !!entry)),
  ];
}

function firstList(proposal: Record<string, unknown>, keys: string[]): string[] {
  for (const key of keys) {
    const values = strings(proposal[key]);
    if (values.length > 0) return values;
  }
  return [];
}

function initialSelection(proposal: Record<string, unknown>): RouteSelection {
  return {
    route: text(proposal.omniroute_route_id),
    reasoning: text(proposal.reasoning_effort),
    permission: text(proposal.permission_mode),
  };
}

function selectionOptions(proposal: Record<string, unknown>) {
  return {
    routes: firstList(proposal, [
      "eligible_route_ids",
      "eligible_routes",
      "allowed_route_ids",
      "available_route_ids",
    ]),
    reasoning: firstList(proposal, [
      "eligible_reasoning_efforts",
      "allowed_reasoning_efforts",
      "available_reasoning_efforts",
    ]),
    permissions: firstList(proposal, [
      "eligible_permission_modes",
      "allowed_permission_modes",
      "available_permission_modes",
    ]),
  };
}

function finalSelection(
  proposal: Record<string, unknown>,
  responseContent?: Record<string, unknown>,
): RouteSelection {
  const original = initialSelection(proposal);
  const nested = ["final_selection", "route_selection", "route_adjustment", "selection"]
    .map((key) => responseContent?.[key])
    .find(
      (value): value is Record<string, unknown> =>
        value !== null && typeof value === "object" && !Array.isArray(value),
    );
  const source = nested ?? responseContent ?? {};
  return {
    route:
      text(source.omniroute_route_id) ||
      text(source.route_id) ||
      text(source.route) ||
      original.route,
    reasoning: text(source.reasoning_effort) || text(source.reasoning) || original.reasoning,
    permission: text(source.permission_mode) || text(source.permission) || original.permission,
  };
}

function SelectionRows({ selection }: { selection: RouteSelection }) {
  return (
    <div className="grid gap-1 text-xs sm:grid-cols-[max-content_1fr]">
      <div className="text-muted-foreground">Route</div>
      <div className="break-words">
        <code>{selection.route || "—"}</code>
      </div>
      <div className="text-muted-foreground">Reasoning effort</div>
      <div className="break-words">{selection.reasoning || "—"}</div>
      <div className="text-muted-foreground">Permission mode</div>
      <div className="break-words">{selection.permission || "—"}</div>
    </div>
  );
}

export function PendingRouteApprovalCard({
  proposal,
  elicitationId,
  onSubmit,
}: PendingRouteApprovalCardProps) {
  const formId = useId();
  const original = initialSelection(proposal);
  const options = selectionOptions(proposal);
  const [adjusting, setAdjusting] = useState(false);
  const [selection, setSelection] = useState(original);
  const [mutating, setMutating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const hasEligibleAdjustments =
    options.routes.length > 0 || options.reasoning.length > 0 || options.permissions.length > 0;

  const mutate = (action: "accept" | "decline", content?: Record<string, unknown>) => {
    if (mutating) return;
    setMutating(true);
    setError(null);
    try {
      const result = onSubmit(action, content);
      if (result && typeof result.then === "function") {
        void result.catch((cause) => {
          setMutating(false);
          setError(cause instanceof Error ? cause.message : "Could not update the route");
        });
      }
    } catch (cause) {
      setMutating(false);
      setError(cause instanceof Error ? cause.message : "Could not update the route");
    }
  };

  const applyAdjustment = () => {
    mutate("accept", {
      omniroute_route_id: selection.route,
      reasoning_effort: selection.reasoning,
      permission_mode: selection.permission,
    });
  };

  return (
    <div
      className="flex min-w-0 flex-col gap-3"
      data-testid="pending-route-approval-card"
      data-elicitation-id={elicitationId}
      aria-busy={mutating}
    >
      <RouteProposalCard proposal={proposal} />
      {adjusting && (
        <fieldset
          className="grid min-w-0 gap-3 rounded-md border p-3 sm:grid-cols-3"
          data-testid="route-adjustment-controls"
          disabled={mutating}
        >
          <legend className="px-1 text-sm font-medium">Change routing recommendation</legend>
          <RouteSelect
            id={`${formId}-route`}
            label="Route"
            value={selection.route}
            options={options.routes}
            onChange={(route) => setSelection((current) => ({ ...current, route }))}
          />
          <RouteSelect
            id={`${formId}-reasoning`}
            label="Reasoning effort"
            value={selection.reasoning}
            options={options.reasoning}
            onChange={(reasoning) => setSelection((current) => ({ ...current, reasoning }))}
          />
          <RouteSelect
            id={`${formId}-permission`}
            label="Permission mode"
            value={selection.permission}
            options={options.permissions}
            onChange={(permission) => setSelection((current) => ({ ...current, permission }))}
          />
        </fieldset>
      )}
      {error && (
        <p role="alert" className="text-destructive text-sm">
          {error}
        </p>
      )}
      <div
        className="flex flex-col gap-2 sm:flex-row sm:flex-wrap"
        data-testid="route-approval-actions"
      >
        {adjusting ? (
          <>
            <Button
              size="sm"
              className="min-h-11 w-full sm:w-auto"
              disabled={mutating}
              onClick={applyAdjustment}
            >
              <CheckIcon className="mr-1 size-3.5" aria-hidden="true" />
              Apply changes
            </Button>
            <Button
              size="sm"
              variant="outline"
              className="min-h-11 w-full sm:w-auto"
              disabled={mutating}
              onClick={() => {
                setSelection(original);
                setAdjusting(false);
              }}
            >
              Cancel changes
            </Button>
          </>
        ) : (
          <>
            <Button
              size="sm"
              className="min-h-11 w-full sm:w-auto"
              disabled={mutating}
              onClick={() => mutate("accept")}
            >
              <CheckIcon className="mr-1 size-3.5" aria-hidden="true" />
              Approve
            </Button>
            <Button
              size="sm"
              variant="outline"
              className="min-h-11 w-full sm:w-auto"
              disabled={mutating || !hasEligibleAdjustments}
              title={
                hasEligibleAdjustments
                  ? undefined
                  : "No alternative routes, reasoning efforts, or permission modes were provided."
              }
              onClick={() => setAdjusting(true)}
            >
              <PencilIcon className="mr-1 size-3.5" aria-hidden="true" />
              Change / Adjust
            </Button>
            <Button
              size="sm"
              variant="outline"
              className="min-h-11 w-full sm:w-auto"
              disabled={mutating}
              onClick={() => mutate("decline")}
            >
              <XIcon className="mr-1 size-3.5" aria-hidden="true" />
              Reject
            </Button>
          </>
        )}
      </div>
    </div>
  );
}

function RouteSelect({
  id,
  label,
  value,
  options,
  onChange,
}: {
  id: string;
  label: string;
  value: string;
  options: string[];
  onChange: (value: string) => void;
}) {
  const eligible = options.length > 0 ? options : value ? [value] : [];
  return (
    <label htmlFor={id} className="grid min-w-0 gap-1 text-xs font-medium">
      {label}
      <select
        id={id}
        value={value}
        disabled={options.length === 0}
        onChange={(event) => onChange(event.target.value)}
        className="min-h-11 w-full min-w-0 rounded-md border bg-background px-2 text-sm"
      >
        {eligible.map((option) => (
          <option key={option} value={option}>
            {option}
          </option>
        ))}
      </select>
    </label>
  );
}

export function RouteApprovalCard({
  proposal,
  action,
  elicitationId,
  responseContent,
}: RouteApprovalCardProps) {
  const [expanded, setExpanded] = useState(false);
  const original = initialSelection(proposal);
  const final = finalSelection(proposal, responseContent);
  const adjusted =
    original.route !== final.route ||
    original.reasoning !== final.reasoning ||
    original.permission !== final.permission;
  const labels: Record<RouteAction, string> = {
    accept: adjusted ? "Approved with changes" : "Approved",
    decline: "Rejected",
    auto_resolved: "Resolved elsewhere",
    cancel: "Cancelled",
  };
  const dataState =
    action === "accept"
      ? "approved"
      : action === "decline"
        ? "rejected"
        : action === "cancel"
          ? "cancelled"
          : "resolved-elsewhere";
  const detailsId = `route-approval-details-${elicitationId}`;
  const billing =
    text(proposal.billing_summary) ||
    `${strings(proposal.allowed_billing_classes).join(", ")} allowed; ${strings(proposal.forbidden_billing_classes).join(", ")} forbidden`;

  return (
    <Alert
      data-testid="route-approval-card"
      data-state={dataState}
      data-elicitation-id={elicitationId}
      className="flex min-w-0 flex-col gap-2 border-muted"
    >
      <AlertTitle className="flex flex-wrap items-center gap-2 text-sm">
        {action === "accept" && <CheckIcon className="size-4 text-success" aria-hidden="true" />}
        <span data-testid="route-approval-card-label">{labels[action]}</span>
        {final.route && (
          <span className="text-muted-foreground text-xs" data-testid="route-approval-route-id">
            · Route <code>{final.route}</code>
          </span>
        )}
        <Button
          variant="ghost"
          size="sm"
          className="ml-auto min-h-11 px-2 text-xs sm:h-7 sm:min-h-0"
          onClick={() => setExpanded((value) => !value)}
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
      <AlertDescription className="flex min-w-0 flex-col gap-3 text-xs text-foreground">
        <div
          className="grid gap-3 sm:grid-cols-2"
          data-testid="route-approval-summary"
          aria-label="Original and final route selection"
        >
          <section aria-label="Original recommendation" className="min-w-0 rounded-md border p-2">
            <h4 className="mb-1 font-medium">Original recommendation</h4>
            <SelectionRows selection={original} />
          </section>
          <section aria-label="Final selection" className="min-w-0 rounded-md border p-2">
            <h4 className="mb-1 font-medium">Final selection</h4>
            {action === "accept" ? <SelectionRows selection={final} /> : <p>Not applied</p>}
          </section>
          <div className="sm:col-span-2 grid gap-1 sm:grid-cols-[max-content_1fr]">
            <div className="text-muted-foreground">Harness</div>
            <div className="break-words">
              {text(proposal.recommended_harness) || "OpenCode Native"}
            </div>
            {billing && (
              <>
                <div className="text-muted-foreground">Billing</div>
                <div className="break-words">{billing}</div>
              </>
            )}
            {typeof proposal.evaluator_fallback_used === "boolean" && (
              <>
                <div className="text-muted-foreground">Fallback used</div>
                <div className="break-words">{proposal.evaluator_fallback_used ? "yes" : "no"}</div>
              </>
            )}
          </div>
        </div>
        {proposal.omniroute_requires_explicit_approval === true && (
          <div className="font-medium text-yellow-800 dark:text-yellow-300">
            Explicit approval required — pro/premium routing.
          </div>
        )}
        {expanded && (
          <div
            id={detailsId}
            data-testid="route-approval-details"
            className="mt-1 overflow-visible"
          >
            <RouteProposalCard proposal={proposal} />
          </div>
        )}
      </AlertDescription>
    </Alert>
  );
}
