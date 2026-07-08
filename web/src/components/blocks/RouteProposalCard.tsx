import { useMemo, useState } from "react";
import { CheckIcon, PencilIcon, XIcon } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Textarea } from "@/components/ui/textarea";
import type { SubmitApprovalFn } from "./ApprovalCard";

export interface RouterProfilePayload {
  profile: string;
  model_family: string;
  reasoning: string;
  role: "route_recommender";
  model_id?: string | null;
  provider_id?: string | null;
  display_label?: string | null;
}

export interface RouteProposalPayload {
  proposal_id: string;
  task_type: string;
  recommended_harness: string;
  model_policy: string;
  model_lane: string;
  preferred_model?: string | null;
  reasoning_effort: string;
  permission_mode: string;
  allowed_billing_classes: string[];
  forbidden_billing_classes: string[];
  execution_fallback_policy: string;
  alternatives?: Array<{ harness: string; model_policy: string; rationale: string }>;
  rationale: string;
  router_primary_profile: RouterProfilePayload;
  router_fallback_profile: RouterProfilePayload;
  router_used_profile: RouterProfilePayload;
  router_fallback_used: boolean;
  router_invoked?: boolean;
  proposal_source?: "default_route_policy" | "llm_router";
  proposal_source_label?: string;
  non_api_billed_constraint: string;
}

interface RouteProposalCardProps {
  elicitationId: string;
  proposal: RouteProposalPayload;
  status: "pending" | "responded";
  response: {
    action: "accept" | "decline" | "cancel" | "auto_resolved";
    content?: Record<string, unknown>;
  } | null;
  onSubmit: SubmitApprovalFn;
}

const effortOptions = ["low", "medium", "high"];
const permissionOptions = ["ask before edits", "read-only", "auto-approve safe reads"];
const laneOptions = [
  "coding/default subscription-or-free lane",
  "default subscription-or-free lane",
  "review/exploration subscription-or-free lane",
];

function routerDisplayName(profile: RouterProfilePayload): string {
  if (profile.display_label) return profile.display_label;
  if (!profile.reasoning || profile.reasoning === "provider_default") return profile.model_family;
  return `${profile.model_family} · ${profile.reasoning} reasoning`;
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="grid gap-1 sm:grid-cols-[11rem_1fr] sm:gap-3">
      <dt className="text-xs font-medium uppercase tracking-wide text-muted-foreground">{label}</dt>
      <dd className="text-sm text-foreground">{value}</dd>
    </div>
  );
}

export function RouteProposalCard({
  elicitationId,
  proposal,
  status,
  response,
  onSubmit,
}: RouteProposalCardProps) {
  const [modelLane, setModelLane] = useState(proposal.model_lane || proposal.model_policy);
  const [effort, setEffort] = useState(proposal.reasoning_effort);
  const [permissionMode, setPermissionMode] = useState(proposal.permission_mode);
  const [comment, setComment] = useState("");
  const changed =
    modelLane !== proposal.model_lane ||
    effort !== proposal.reasoning_effort ||
    permissionMode !== proposal.permission_mode;

  const content = useMemo(
    () => ({
      model_policy: modelLane,
      model_lane: modelLane,
      reasoning_effort: effort,
      permission_mode: permissionMode,
      comment,
    }),
    [comment, effort, modelLane, permissionMode],
  );

  const disabled = status !== "pending";
  const routerInvoked = proposal.router_invoked === true;
  const fallbackRouter = routerDisplayName(proposal.router_fallback_profile);
  const fallbackText = routerInvoked
    ? proposal.router_fallback_used
      ? `${fallbackRouter} used after primary router failed`
      : `${fallbackRouter} not used`
    : `Configured fallback: ${fallbackRouter} (not invoked)`;

  return (
    <Card className="border-amber-500/40 bg-amber-50/40 dark:bg-amber-950/10">
      <CardHeader className="pb-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle className="text-base">Execution Route Proposal</CardTitle>
          <Badge variant={proposal.router_fallback_used ? "secondary" : "outline"}>
            {fallbackText}
          </Badge>
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        <dl className="space-y-2">
          <Row
            label="Proposal source"
            value={proposal.proposal_source_label ?? "Default route policy proposal"}
          />
          <Row
            label={routerInvoked ? "Recommended by" : "Configured router"}
            value={
              routerInvoked
                ? routerDisplayName(proposal.router_used_profile)
                : `${routerDisplayName(proposal.router_used_profile)} (not invoked)`
            }
          />
          <Row label="Harness" value={proposal.recommended_harness} />
          <Row label="Execution policy" value="Non-API only" />
          <Row label="Model policy" value={proposal.model_policy || proposal.model_lane} />
          <Row label="Reasoning" value={proposal.reasoning_effort} />
          <Row label="Permission" value={proposal.permission_mode} />
          <Row label="Fallback" value={proposal.execution_fallback_policy} />
          <Row label="API-billed fallback" value="forbidden" />
        </dl>

        <div className="rounded-md border bg-background/70 p-3 text-sm">
          <div className="mb-1 font-medium">Why</div>
          <p className="text-muted-foreground">{proposal.rationale}</p>
        </div>

        {proposal.alternatives && proposal.alternatives.length > 0 ? (
          <div className="space-y-1 text-sm">
            <div className="font-medium">Alternatives</div>
            <ul className="list-disc pl-5 text-muted-foreground">
              {proposal.alternatives.map((alt) => (
                <li key={`${alt.harness}-${alt.model_policy}`}>
                  {alt.harness}: {alt.model_policy} — {alt.rationale}
                </li>
              ))}
            </ul>
          </div>
        ) : null}

        <div className="grid gap-3 sm:grid-cols-3">
          <label className="space-y-1 text-sm">
            <span className="font-medium">Model lane</span>
            <select
              className="w-full rounded-md border bg-background px-3 py-2 text-sm"
              value={modelLane}
              disabled={disabled}
              onChange={(event) => setModelLane(event.target.value)}
            >
              {Array.from(
                new Set([proposal.model_lane, proposal.model_policy, ...laneOptions]),
              ).map((option) => (
                <option key={option} value={option}>
                  {option}
                </option>
              ))}
            </select>
          </label>
          <label className="space-y-1 text-sm">
            <span className="font-medium">Reasoning / effort</span>
            <select
              className="w-full rounded-md border bg-background px-3 py-2 text-sm"
              value={effort}
              disabled={disabled}
              onChange={(event) => setEffort(event.target.value)}
            >
              {Array.from(new Set([proposal.reasoning_effort, ...effortOptions])).map((option) => (
                <option key={option} value={option}>
                  {option}
                </option>
              ))}
            </select>
          </label>
          <label className="space-y-1 text-sm">
            <span className="font-medium">Permission mode</span>
            <select
              className="w-full rounded-md border bg-background px-3 py-2 text-sm"
              value={permissionMode}
              disabled={disabled}
              onChange={(event) => setPermissionMode(event.target.value)}
            >
              {Array.from(new Set([proposal.permission_mode, ...permissionOptions])).map(
                (option) => (
                  <option key={option} value={option}>
                    {option}
                  </option>
                ),
              )}
            </select>
          </label>
        </div>

        <label className="space-y-1 text-sm">
          <span className="font-medium">Comment</span>
          <Textarea
            value={comment}
            disabled={disabled}
            placeholder="Optional note for this routing decision"
            onChange={(event) => setComment(event.target.value)}
          />
        </label>

        {status === "responded" ? (
          <div className="text-sm text-muted-foreground">
            {response?.action === "accept" ? "Approved" : "Declined or cancelled"}
          </div>
        ) : (
          <div className="flex flex-wrap gap-2">
            <Button size="sm" onClick={() => onSubmit(elicitationId, "accept", { comment })}>
              <CheckIcon className="mr-2 h-4 w-4" /> Approve
            </Button>
            <Button
              size="sm"
              variant="secondary"
              onClick={() => onSubmit(elicitationId, "accept", content)}
            >
              <PencilIcon className="mr-2 h-4 w-4" />{" "}
              {changed ? "Modify + approve" : "Modify + approve"}
            </Button>
            <Button
              size="sm"
              variant="outline"
              onClick={() => onSubmit(elicitationId, "decline", { comment })}
            >
              <XIcon className="mr-2 h-4 w-4" /> Decline
            </Button>
            <Button
              size="sm"
              variant="ghost"
              onClick={() => onSubmit(elicitationId, "cancel", { comment })}
            >
              Cancel
            </Button>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
