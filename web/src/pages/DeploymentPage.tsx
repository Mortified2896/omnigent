import { useEffect, useMemo, useState } from "react";
import {
  AlertTriangleIcon,
  CheckCircle2Icon,
  Clock3Icon,
  RefreshCwIcon,
  ShieldIcon,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { PageScroll } from "@/components/PageScroll";
import { useIsAdmin } from "@/hooks/useIsAdmin";
import {
  enqueueDeployment,
  fetchDeploymentJob,
  fetchDeploymentPlan,
  type DeploymentJob,
  type DeploymentPeerDisplay,
  type DeploymentPlan,
} from "@/lib/deploymentApi";

function short(value: string | null, length = 12): string {
  return value ? value.slice(0, length) : "unknown";
}

function observedAt(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return "unknown";
  return new Date(value * 1000).toLocaleString();
}

function blockerLabel(value: string): string {
  return value
    .replaceAll("_", " ")
    .replace(/^controller /, "controller: ")
    .replace(/^target /, "target: ")
    .replace(/^source /, "source: ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function PeerCard({
  peer,
  role,
}: {
  peer: DeploymentPeerDisplay | null;
  role: "source" | "target";
}) {
  if (peer === null) {
    return (
      <div
        className="rounded-xl border border-dashed border-border p-4"
        data-testid={`deployment-peer-${role}`}
      >
        <div className="font-medium">{role === "source" ? "O1 source" : "O2 target"}</div>
        <p className="mt-2 text-sm text-muted-foreground">
          Trusted controller evidence is unavailable.
        </p>
      </div>
    );
  }
  return (
    <div
      className="rounded-xl border border-border bg-card p-4"
      data-testid={`deployment-peer-${role}`}
    >
      <div className="flex items-center justify-between gap-3">
        <div>
          <div className="text-sm text-muted-foreground">
            {role === "source" ? "Source" : "Target"}
          </div>
          <h2 className="text-lg font-semibold">{peer.instance ?? "Instance unknown"}</h2>
        </div>
        <Badge variant={peer.healthy === true ? "secondary" : "outline"}>
          {peer.healthy === true ? "Healthy" : peer.healthy === false ? "Unhealthy" : "Unknown"}
        </Badge>
      </div>
      <dl className="mt-4 grid gap-2 text-sm sm:grid-cols-2">
        <div>
          <dt className="text-muted-foreground">Official/base version</dt>
          <dd>{peer.official_version ?? "not recorded"}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">Upstream reference</dt>
          <dd className="break-all">{peer.upstream_ref ?? "not recorded"}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">Custom build</dt>
          <dd
            className="font-mono"
            title={peer.source_sha ?? undefined}
            aria-label={peer.source_sha ? `Full custom build SHA: ${peer.source_sha}` : undefined}
          >
            {peer.custom_version ?? "unknown"}
          </dd>
        </div>
        <div>
          <dt className="text-muted-foreground">Installed package</dt>
          <dd>{peer.package_version ?? "unknown"}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">Acceptance digest</dt>
          <dd className="font-mono" title={peer.release_acceptance_digest ?? undefined}>
            {short(peer.release_acceptance_digest, 16)}
          </dd>
        </div>
        <div>
          <dt className="text-muted-foreground">Schema revision</dt>
          <dd>{peer.schema_revision ?? "unknown"}</dd>
        </div>
        <div className="sm:col-span-2">
          <dt className="text-muted-foreground">Observed</dt>
          <dd>{observedAt(peer.observed_at)}</dd>
        </div>
      </dl>
    </div>
  );
}

export function DeploymentPage() {
  const isAdmin = useIsAdmin();
  const [plan, setPlan] = useState<DeploymentPlan | null>(null);
  const [job, setJob] = useState<DeploymentJob | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function refresh() {
    setLoading(true);
    setError(null);
    try {
      setPlan(await fetchDeploymentPlan());
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to read deployment state.");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    if (isAdmin) void refresh();
  }, [isAdmin]);

  useEffect(() => {
    if (job === null || !["queued", "running"].includes(job.status)) return;
    const timer = window.setInterval(async () => {
      try {
        setJob(await fetchDeploymentJob(job.id));
      } catch (reason) {
        setError(reason instanceof Error ? reason.message : "Unable to read deployment job.");
      }
    }, 2000);
    return () => window.clearInterval(timer);
  }, [job]);

  const exactSource = useMemo(() => {
    if (!plan?.source) return "unknown source";
    return `${plan.source.custom_version ?? "unknown build"} (${plan.source.source_sha ?? "unknown SHA"}) · acceptance ${short(plan.source.release_acceptance_digest, 16)}`;
  }, [plan?.source]);

  const exactTarget = useMemo(() => {
    if (!plan?.target) return "unknown target";
    return `${plan.target.custom_version ?? "unknown build"} (${plan.target.source_sha ?? "unknown SHA"})`;
  }, [plan?.target]);

  async function submit() {
    if (!plan?.can_sync || !plan.plan_id) return;
    setSubmitting(true);
    setError(null);
    try {
      setJob(await enqueueDeployment(plan.plan_id, crypto.randomUUID()));
      setConfirming(false);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to enqueue deployment.");
    } finally {
      setSubmitting(false);
    }
  }

  if (!isAdmin) {
    return (
      <PageScroll contentClassName="px-8" extraBottom="2.5rem">
        <p className="text-muted-foreground">Administrator access is required.</p>
      </PageScroll>
    );
  }

  return (
    <PageScroll contentClassName="px-8" extraBottom="2.5rem">
      <section className="mx-auto max-w-4xl" data-testid="deployment-panel">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h1 className="text-2xl font-semibold">Deployment</h1>
            <p className="mt-1 text-muted-foreground">
              Verified runtime identity and the deterministic O1 → O2 update.
            </p>
          </div>
          <Button variant="outline" size="sm" onClick={() => void refresh()} disabled={loading}>
            <RefreshCwIcon className={loading ? "animate-spin" : undefined} /> Refresh evidence
          </Button>
        </div>

        {error && (
          <div
            className="mt-6 flex gap-2 rounded-xl border border-destructive/40 bg-destructive/5 p-4 text-sm"
            role="alert"
          >
            <AlertTriangleIcon className="mt-0.5 shrink-0 text-destructive" />
            <span>{error}</span>
          </div>
        )}

        {plan && (
          <>
            <div className="mt-6 grid gap-4 md:grid-cols-2">
              <PeerCard peer={plan.source} role="source" />
              <PeerCard peer={plan.target} role="target" />
            </div>

            <div className="mt-6 rounded-xl border border-border p-4">
              <div className="flex items-start gap-3">
                {plan.status === "ready" ? (
                  <CheckCircle2Icon className="text-emerald-600" />
                ) : (
                  <ShieldIcon className="text-muted-foreground" />
                )}
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <h2 className="font-medium">
                      {plan.status === "already_current"
                        ? "O2 is already current"
                        : plan.status === "ready"
                          ? "Validated release ready"
                          : "Update unavailable"}
                    </h2>
                    <Badge variant={plan.status === "ready" ? "secondary" : "outline"}>
                      {plan.status}
                    </Badge>
                  </div>
                  {plan.status === "ready" && plan.source && plan.target && (
                    <p className="mt-2 text-sm text-muted-foreground">
                      Exact source: <span className="font-mono text-foreground">{exactSource}</span>
                      <br />
                      Target now: <span className="font-mono text-foreground">{exactTarget}</span>
                    </p>
                  )}
                  {plan.blockers.length > 0 && (
                    <ul className="mt-3 list-disc space-y-1 pl-5 text-sm text-muted-foreground">
                      {plan.blockers.map((blocker) => (
                        <li key={blocker}>{blockerLabel(blocker)}</li>
                      ))}
                    </ul>
                  )}
                </div>
              </div>

              {confirming && plan.status === "ready" && (
                <div className="mt-4 rounded-lg border border-amber-500/40 bg-amber-500/5 p-3 text-sm">
                  <p className="font-medium">Confirm exact target state</p>
                  <p className="mt-1 text-muted-foreground">
                    O2 will be stopped and restarted on {exactSource}. O1 stays running and is not
                    updated.
                  </p>
                  <div className="mt-3 flex gap-2">
                    <Button size="sm" onClick={() => void submit()} loading={submitting}>
                      Confirm update O2
                    </Button>
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() => setConfirming(false)}
                      disabled={submitting}
                    >
                      Cancel
                    </Button>
                  </div>
                </div>
              )}
              {!confirming && plan.can_sync && plan.plan_id && (
                <Button
                  className="mt-4"
                  onClick={() => setConfirming(true)}
                  data-testid="deployment-update-o2"
                >
                  Update O2 to O1’s custom build
                </Button>
              )}
            </div>
          </>
        )}

        {job && (
          <div
            className="mt-6 rounded-xl border border-border bg-muted/30 p-4"
            data-testid="deployment-job"
          >
            <div className="flex items-center gap-2 font-medium">
              {job.status === "succeeded" ? (
                <CheckCircle2Icon className="text-emerald-600" />
              ) : (
                <Clock3Icon />
              )}
              Update job: {job.status}
            </div>
            {job.reason && <p className="mt-2 text-sm text-muted-foreground">{job.reason}</p>}
            <p className="mt-2 break-all font-mono text-xs text-muted-foreground">{job.id}</p>
          </div>
        )}
      </section>
    </PageScroll>
  );
}
