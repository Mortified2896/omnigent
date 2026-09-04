import { beforeEach, describe, expect, it, vi } from "vitest";

import { authenticatedFetch } from "./identity";
import {
  clearO3RoutingDraft,
  createO3RoutingProposal,
  decideO3RoutingProposal,
  linkO3RoutingProposalSession,
  readO3RoutingDraft,
  writeO3RoutingDraft,
  type O3RoutingDraft,
} from "./o3RoutingReview";

vi.mock("./identity", () => ({ authenticatedFetch: vi.fn() }));

function response(body: unknown, ok = true): Response {
  return {
    ok,
    status: ok ? 200 : 409,
    statusText: ok ? "OK" : "Conflict",
    json: async () => body,
  } as Response;
}

beforeEach(() => {
  vi.mocked(authenticatedFetch).mockReset();
  localStorage.clear();
});

describe("O3 routing-review API", () => {
  it("creates a proposal without changing the prompt", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValueOnce(response({ proposal_id: "proposal-1" }));

    await createO3RoutingProposal("  exact task\n", "Workspace: /tmp/project");

    const [url, init] = vi.mocked(authenticatedFetch).mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/o3/routing-review/proposals");
    expect(JSON.parse(init.body as string)).toEqual({
      prompt: "  exact task\n",
      workspace_summary: "Workspace: /tmp/project",
    });
  });

  it("sends explicit provisional and run-anyway confirmations", async () => {
    vi.mocked(authenticatedFetch)
      .mockResolvedValueOnce(response({ proposal_id: "proposal-1" }))
      .mockResolvedValueOnce(response({ proposal_id: "proposal-1", session_id: "conv_1" }));

    await decideO3RoutingProposal("proposal-1", {
      action: "run_anyway",
      confirm_run_anyway: true,
      reason: "Compatibility was manually verified",
    });
    await linkO3RoutingProposalSession("proposal-1", "conv_1");

    expect(JSON.parse(vi.mocked(authenticatedFetch).mock.calls[0]![1]!.body as string)).toEqual({
      action: "run_anyway",
      confirm_run_anyway: true,
      reason: "Compatibility was manually verified",
    });
    expect(JSON.parse(vi.mocked(authenticatedFetch).mock.calls[1]![1]!.body as string)).toEqual({
      session_id: "conv_1",
    });
  });

  it("surfaces the backend error message", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValueOnce(
      response({ error: { code: "no_adequate_route", message: "No adequate route" } }, false),
    );

    await expect(decideO3RoutingProposal("proposal-1", { action: "approve" })).rejects.toThrow(
      "No adequate route",
    );
  });
});

describe("O3 routing-review draft", () => {
  const draft: O3RoutingDraft = {
    version: 1,
    proposalId: "proposal-1",
    proposalUpdatedAt: "2026-09-04T00:00:00Z",
    initialPrompt: "unchanged prompt",
    composerMessage: "unchanged prompt",
    workspaceSummary: "Workspace: /tmp/project",
    sessionId: null,
  };

  it("round-trips the minimal versioned draft and clears it", () => {
    writeO3RoutingDraft(draft);
    expect(readO3RoutingDraft()).toEqual(draft);
    clearO3RoutingDraft();
    expect(readO3RoutingDraft()).toBeNull();
  });

  it("fails safely for corrupt or unknown-version storage", () => {
    localStorage.setItem("omnigent:o3-routing-review:draft:v1", "not-json");
    expect(readO3RoutingDraft()).toBeNull();

    localStorage.setItem(
      "omnigent:o3-routing-review:draft:v1",
      JSON.stringify({ ...draft, version: 2 }),
    );
    expect(readO3RoutingDraft()).toBeNull();
  });
});
