// Classify a single DELETE /v1/sessions/{id} response into the per-item
// buckets the bulk-delete UI consumes.
//
// Status mapping:
//   200, 204       → deleted
//   404            → alreadyDeleted (idempotent — no-op on retry)
//   403            → forbidden (shared / insufficient permission)
//   409            → activeSession (server asked caller to stop first)
//   5xx, network   → failed (retryable unless the message names a
//                    non-retryable condition like a permission boundary)
//
// We avoid coupling this to the server's response model; just inspect
// the HTTP status and try to recover a human-readable reason from the
// JSON error body when present.

export type BulkDeleteOutcome =
  | { kind: "deleted"; id: string }
  | { kind: "alreadyDeleted"; id: string }
  | { kind: "forbidden"; id: string; reason: string }
  | { kind: "activeSession"; id: string; reason: string }
  | { kind: "failed"; id: string; reason: string; retryable: boolean };

interface ErrorBody {
  message?: string;
  code?: string;
  detail?: { message?: string; code?: string } | string;
}

async function readError(res: Response): Promise<string> {
  try {
    const body = (await res.json()) as ErrorBody;
    if (typeof body.detail === "string" && body.detail) return body.detail;
    if (body.detail && typeof body.detail === "object" && body.detail.message) {
      return body.detail.message;
    }
    if (body.message) return body.message;
    return res.statusText || `${res.status}`;
  } catch {
    return res.statusText || `${res.status}`;
  }
}

export async function classifyDeleteResponse(
  id: string,
  res: Response,
): Promise<BulkDeleteOutcome> {
  if (res.ok) return { kind: "deleted", id };
  if (res.status === 404) return { kind: "alreadyDeleted", id };
  const reason = await readError(res);
  if (res.status === 403) return { kind: "forbidden", id, reason };
  if (res.status === 409) return { kind: "activeSession", id, reason };
  if (res.status === 429) return { kind: "failed", id, reason, retryable: true };
  if (res.status >= 500) return { kind: "failed", id, reason, retryable: true };
  return { kind: "failed", id, reason, retryable: false };
}