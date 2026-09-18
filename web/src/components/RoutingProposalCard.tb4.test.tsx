import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { O3RoutingProposal } from "@/lib/o3RoutingReview";
import { authenticatedFetch } from "@/lib/identity";
import { RoutingProposalCard } from "./RoutingProposalCard";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));

const mockedFetch = vi.mocked(authenticatedFetch);

function fixture(experiment?: Record<string, unknown>): O3RoutingProposal {
  return {
    schema_version: 2,
    proposal_id: "01234567-89ab-cdef-0123-456789abcdef",
    created_at: "2026-09-17T00:00:00Z",
    updated_at: "2026-09-17T00:00:00Z",
    expires_at: "2026-09-18T00:00:00Z",
    prompt_fingerprint: `sha256:${"0".repeat(64)}`,
    workspace_summary: "",
    audit: experiment ? { tb4_floor_experiment: experiment } : {},
    adviser: {
      task_summary: "Edit a repository",
      task_classification: "coding",
      difficulty: "normal",
      risk: "low",
      requirements: {
        terminal: true,
        tools: true,
        minimum_context_tokens: 0,
        vision: false,
      },
      benchmark_requirements: [
        {
          benchmark_id: "terminal-bench",
          version: "4.0.0",
          slice_id: "tb4.overall",
          reason: "coding task",
        },
      ],
      proposed_reasoning_effort: "high",
      evidence_policy: "provisional",
      disposition: "route",
      confidence: 0.8,
      rationale: "Use a coding-capable model.",
      decomposition: [],
    },
    approved_constraints: {
      benchmark: {
        benchmark_id: "terminal-bench",
        version: "4.0.0",
        slice_id: "tb4.overall",
        minimum_score: 0.385,
        reason: "exact floor",
        difficulty: "easy",
        calibration_version: "tb4-exact-floor-ab-v1",
      },
      difficulty: "easy",
      calibration_version: "tb4-exact-floor-ab-v1",
      reasoning_effort: "high",
      risk: "low",
      evidence_policy: "provisional",
      cost_quota_preference: "preserve_subscription",
    },
    evaluations: [],
    frontier: {
      requested_minimum: 0.385,
      global_measured_frontier: null,
      accessible_configured_frontier: null,
      healthy_available_frontier: null,
      passing_exact_candidates: [],
      provisional_candidates: [],
      capability_gap: null,
    },
    disposition: "route",
    decision: null,
    decision_reason: null,
    derived_combo_name: null,
    derived_combo_definition: null,
    session_id: null,
    actual_provider: null,
    actual_model: null,
    actual_reasoning_effort: null,
    execution_provenance: [],
    execution_status: null,
    provenance_synced_at: null,
    task_outcome: null,
    terminal_disposition: null,
  };
}

function renderCard(value: O3RoutingProposal) {
  const onProposalChange = vi.fn();
  render(
    <RoutingProposalCard
      proposal={value}
      slices={[]}
      onAdjust={vi.fn()}
      onDecision={vi.fn()}
      onProposalChange={onProposalChange}
      onApproved={vi.fn()}
      onReset={vi.fn()}
    />,
  );
  return onProposalChange;
}

describe("RoutingProposalCard TB4 floor experiment", () => {
  beforeEach(() => mockedFetch.mockReset());

  it("hides adviser output until the user commits an exact TB4 floor", async () => {
    const value = fixture();
    mockedFetch.mockResolvedValue(
      new Response(JSON.stringify(fixture({
        policy_version: "tb4-exact-floor-ab-v1",
        user_floor_percent: 38.5,
        adviser_floor_percent: 44.2,
        assigned_arm: "adviser",
        assignment_propensity: 0.5,
        executed_floor_percent: 44.2,
        adviser_confidence: 0.8,
        adviser_rationale: "harder coding task",
        status: "applied",
      })), { status: 200, headers: { "Content-Type": "application/json" } }),
    );
    const onProposalChange = renderCard(value);

    expect(screen.getByTestId("o3-tb4-floor-gate")).toBeInTheDocument();
    expect(screen.queryByText(/Adviser:/)).not.toBeInTheDocument();
    fireEvent.change(screen.getByTestId("o3-user-tb4-floor"), { target: { value: "38.5" } });
    fireEvent.click(screen.getByTestId("o3-apply-tb4-floor"));

    await waitFor(() => expect(mockedFetch).toHaveBeenCalledTimes(1));
    const [url, init] = mockedFetch.mock.calls[0];
    expect(url).toContain("/floor-experiment");
    expect(init?.method).toBe("POST");
    expect(JSON.parse(String(init?.body))).toEqual({ user_floor_percent: 38.5 });
    await waitFor(() => expect(onProposalChange).toHaveBeenCalledTimes(1));
  });

  it("shows user, adviser, and executed exact floors after assignment", () => {
    renderCard(
      fixture({
        policy_version: "tb4-exact-floor-ab-v1",
        user_floor_percent: 31.0,
        adviser_floor_percent: 42.5,
        assigned_arm: "adviser",
        assignment_propensity: 0.5,
        executed_floor_percent: 42.5,
        adviser_confidence: 0.9,
        adviser_rationale: "multi-step repository task",
        status: "applied",
      }),
    );

    const summary = screen.getByTestId("o3-tb4-floor-summary");
    expect(summary).toHaveTextContent("Your floor: 31.0%");
    expect(summary).toHaveTextContent("Adviser: 42.5%");
    expect(summary).toHaveTextContent("Executed: Adviser · 42.5%");
    expect(screen.queryByTestId("o3-tb4-floor-gate")).not.toBeInTheDocument();
  });
});
