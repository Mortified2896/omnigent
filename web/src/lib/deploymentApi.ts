import { authenticatedFetch } from "@/lib/identity";

export interface DeploymentPeerDisplay {
  instance: "O1" | "O2" | null;
  official_version: string | null;
  upstream_ref: string | null;
  package_version: string | null;
  custom_version: string | null;
  source_sha: string | null;
  release_acceptance_digest: string | null;
  schema_revision: string | null;
  healthy: boolean | null;
  observed_at: number | null;
}

export interface DeploymentPlan {
  object?: "deployment";
  status: "ready" | "blocked" | "already_current";
  blockers: string[];
  can_sync: boolean;
  source: DeploymentPeerDisplay | null;
  target: DeploymentPeerDisplay | null;
  expires_at: number | null;
  plan_id: string | null;
}

export interface DeploymentJob {
  id: string;
  status: "queued" | "running" | "succeeded" | "failed" | "recovery_required";
  requested_by?: string;
  created_at?: number;
  updated_at?: number;
  reason?: string | null;
}

function jsonError(status: number): Error {
  if (status === 401) return new Error("Authentication required.");
  if (status === 403) return new Error("Administrator access is required.");
  if (status === 423) return new Error("Writes are fenced while deployment is in progress.");
  if (status === 503) return new Error("The external deployment controller is unavailable.");
  return new Error("The deployment request was rejected.");
}

export async function fetchDeploymentPlan(): Promise<DeploymentPlan> {
  const response = await authenticatedFetch("/v1/deployment");
  if (!response.ok) throw jsonError(response.status);
  return (await response.json()) as DeploymentPlan;
}

export async function enqueueDeployment(
  planId: string,
  idempotencyKey: string,
): Promise<DeploymentJob> {
  const response = await authenticatedFetch("/v1/deployment/sync-o2", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ plan_id: planId, idempotency_key: idempotencyKey }),
  });
  if (!response.ok) throw jsonError(response.status);
  const payload = (await response.json()) as { job?: DeploymentJob };
  if (!payload.job) throw new Error("The controller returned an invalid job.");
  return payload.job;
}

export async function fetchDeploymentJob(jobId: string): Promise<DeploymentJob> {
  const response = await authenticatedFetch(`/v1/deployment/jobs/${encodeURIComponent(jobId)}`);
  if (!response.ok) throw jsonError(response.status);
  const payload = (await response.json()) as { job?: DeploymentJob };
  if (!payload.job) throw new Error("The controller returned an invalid job.");
  return payload.job;
}
