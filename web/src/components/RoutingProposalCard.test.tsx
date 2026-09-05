import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { O3CatalogueRecommendation, O3RoutingProposal } from "@/lib/o3RoutingReview";
import { RoutingProposalCard } from "./RoutingProposalCard";

function proposal(overrides: Partial<O3RoutingProposal> = {}): O3RoutingProposal {
  const base: O3RoutingProposal = {
    schema_version: 2,
    proposal_id: "01234567-89ab-cdef-0123-456789abcdef",
    created_at: "2026-09-04T00:00:00Z",
    updated_at: "2026-09-04T00:00:00Z",
    expires_at: "2026-09-04T01:00:00Z",
    prompt_fingerprint: `sha256:${"0".repeat(64)}`,
    workspace_summary: "Workspace: /tmp/project",
    adviser: {
      task_summary: "Inspect a harmless repository state",
      task_classification: "systems",
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
          slice_id: "tb4.cr-systems-db-v1",
          reason: "Terminal competence is relevant",
        },
      ],
      proposed_reasoning_effort: "low",
      evidence_policy: "provisional",
      disposition: "borderline",
      confidence: 0.8,
      rationale: "A terminal-capable route is required.",
      decomposition: [],
    },
    approved_constraints: {
      benchmark: {
        benchmark_id: "terminal-bench",
        version: "4.0.0",
        slice_id: "tb4.cr-systems-db-v1",
        minimum_score: 0.5,
        reason: "Terminal competence is relevant",
        difficulty: "normal",
        calibration_version: "o3-benchmark-difficulty-calibration-v1",
      },
      difficulty: "normal",
      calibration_version: "o3-benchmark-difficulty-calibration-v1",
      reasoning_effort: "low",
      risk: "low",
      evidence_policy: "provisional",
      cost_quota_preference: "preserve_subscription",
    },
    evaluations: [
      {
        candidate: {
          candidate_id: "opencode-big-pickle-low",
          provider_id: "opencode",
          model: "big-pickle",
          catalogue_model_id: "opencode/big-pickle",
          harness: "codex-native",
          supported_reasoning_efforts: ["low"],
          context_tokens: null,
          terminal: true,
          tools: true,
          vision: false,
          responses_api: true,
          monetary_cost_usd: 0,
          cost_source: "OpenCode free route",
          quota_source: "OpenCode shared quota",
          last_full_probe_at: "2026-09-03T00:00:00+08:00",
          probe_reference: "live probe",
          provider_usable: true,
          model_present: true,
          quota_available: true,
          quota_remaining_percent: null,
          quota_reset_at: null,
          recent_success_rate: 0.99,
          recent_retry_rate: 0.01,
          latency_ms: 150,
        },
        status: "provisional",
        evidence_class: "proxy",
        admission_score: 0.6,
        evidence: [
          {
            benchmark_id: "terminal-bench",
            benchmark_version: "4.0.0",
            slice_id: "tb4.cr-systems-db-v1",
            harness: "other-harness",
            harness_version: null,
            model: "big-pickle",
            provider_path: "opencode",
            reasoning_effort: "low",
            point_score: 0.65,
            confidence_lower: 0.6,
            confidence_upper: 0.7,
            number_of_tasks: 10,
            number_of_attempts: 10,
            evidence_class: "proxy",
            source_type: "artifact",
            source_reference: "test://tb4/proxy",
            evaluation_date: "2026-09-03",
          },
        ],
        exclusions: [],
        caveats: ["no exact benchmark evidence exists for this execution configuration"],
        ranking: {
          evidence_confidence: 0.55,
          competence_margin: 0.1,
          health: 0.99,
          estimated_monetary_cost_usd: 0,
          quota_remaining_percent: null,
          quota_reset_at: null,
          quota_scarcity_penalty: 0.25,
          recent_failure_rate: 0.01,
          recent_retry_rate: 0.01,
          latency_ms: 150,
          deterministic_score: 0.72,
        },
      },
    ],
    frontier: {
      requested_minimum: 0.5,
      global_measured_frontier: 0.7,
      accessible_configured_frontier: null,
      healthy_available_frontier: null,
      passing_exact_candidates: [],
      provisional_candidates: ["opencode-big-pickle-low"],
      capability_gap: "No currently accessible configuration has exact evidence.",
    },
    disposition: "borderline",
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
  return { ...base, ...overrides };
}

function recommendation(
  overrides: Partial<O3CatalogueRecommendation> = {},
): O3CatalogueRecommendation {
  const item = {
    route_id: "free/model-a",
    provider_id: "free",
    displayed_model: "Model A",
    equivalence_identity: "model-a",
    reasoning_mode: "high",
    capability_score_central: 90,
    capability_score_lower: 81,
    capability_score_upper: 96,
    estimated_tier: "frontier",
    conservative_tier: "hard",
    capability_confidence: "high",
    estimate_method: "published benchmark prior",
    gap_from_floor: 21,
    live_present: true,
    responses_callability: "callable_now",
    readiness_observed_at: "2026-09-05T00:00:00Z",
    operator_resource_class: "non_codex_zero_marginal_cost",
    raw_cost_class: "free",
    alternate_route_ids: [],
    caveats: ["Recommendation confidence is limited by snapshot age."],
  };
  return {
    policy_version: "test-policy-v1",
    forecast_version: "test-policy-v1",
    forecast_hash: "forecast-hash",
    readiness_version: "test-readiness-v1",
    readiness_hash: "readiness-hash",
    source_snapshot_timestamp: "2026-09-05T00:00:00Z",
    readiness_observed_at: "2026-09-05T00:00:00Z",
    raw_benchmark_floor: 0.6,
    common_capability_floor: 60,
    total_route_count: 3152,
    live_present_count: 42,
    section_counts: {
      callable_non_codex: 1,
      other_above_floor: 1,
      codex_subscription_fallback: 1,
      nearest_below_floor: 1,
    },
    callable_non_codex: [item],
    other_above_floor: [{ ...item, route_id: "other/model-b", provider_id: "other" }],
    codex_subscription_fallback: [
      {
        ...item,
        route_id: "codex/model-c",
        provider_id: "codex",
        operator_resource_class: "codex_subscription",
      },
    ],
    nearest_below_floor: [
      {
        ...item,
        route_id: "free/model-d",
        capability_score_lower: 59,
        gap_from_floor: -1,
      },
    ],
    stale_warning: "Readiness snapshot may be stale.",
    ...overrides,
  };
}

const slices = [
  {
    benchmark_id: "terminal-bench",
    version: "4.0.0",
    slice_id: "tb4.cr-systems-db-v1",
    label: "Systems and databases",
    interpretation: "Systems tasks",
    task_ids: [],
    task_manifest_digest: "sha256:test",
    official: false,
  },
  {
    benchmark_id: "terminal-bench",
    version: "4.0.0",
    slice_id: "tb4.cr-ai-v1",
    label: "AI systems",
    interpretation: "AI tasks",
    task_ids: [],
    task_manifest_digest: "sha256:test-2",
    official: false,
  },
];

function renderCard(value = proposal()) {
  const onAdjust = vi.fn(async () => ({ ...value, updated_at: "2026-09-04T00:01:00Z" }));
  const onDecision = vi.fn(async (decision) => ({
    ...value,
    decision: decision.action,
    derived_combo_name:
      decision.action === "approve" || decision.action === "run_anyway"
        ? "custom/o3-route-0123456789ab"
        : null,
  }));
  const onProposalChange = vi.fn();
  const onApproved = vi.fn(async () => undefined);
  const onReset = vi.fn();
  render(
    <RoutingProposalCard
      proposal={value}
      slices={slices}
      onAdjust={onAdjust}
      onDecision={onDecision}
      onProposalChange={onProposalChange}
      onApproved={onApproved}
      onReset={onReset}
    />,
  );
  return { onAdjust, onDecision, onProposalChange, onApproved, onReset };
}

describe("RoutingProposalCard", () => {
  it("renders recommendation floors, separated route sections, and warnings", () => {
    renderCard(proposal({ recommendation: recommendation() }));

    const recommendations = screen.getByTestId("o3-catalogue-recommendations");
    expect(screen.getByText("Raw benchmark floor").parentElement).toHaveTextContent("0.600000");
    expect(screen.getByText("Common capability floor").parentElement).toHaveTextContent(
      "60 / 100 rough prior",
    );
    expect(recommendations).toHaveTextContent("Callable non-Codex");
    expect(recommendations).toHaveTextContent("Other models above floor");
    expect(recommendations).toHaveTextContent("Codex subscription fallback");
    expect(recommendations).toHaveTextContent("Recommendation confidence is limited");
    expect(screen.getByText("Readiness snapshot may be stale.")).toBeInTheDocument();
  });

  it("renders a useful no-match state with nearest alternatives", () => {
    renderCard(
      proposal({
        recommendation: recommendation({
          section_counts: {
            callable_non_codex: 0,
            other_above_floor: 0,
            codex_subscription_fallback: 0,
            nearest_below_floor: 1,
          },
          callable_non_codex: [],
          other_above_floor: [],
          codex_subscription_fallback: [],
        }),
      }),
    );

    expect(screen.getByText("Above-floor matches").parentElement).toHaveTextContent("0");
    expect(screen.getByText(/No catalogue model meets the conservative floor\./)).toBeVisible();
    expect(screen.getByText("Nearest below floor")).toBeVisible();
    expect(screen.getByText(/free\/Model A/)).toBeVisible();
  });

  it("renders compact requirements and expands candidate evidence", () => {
    renderCard();

    expect(screen.getByTestId("o3-task-interpretation")).toHaveTextContent(
      "Inspect a harmless repository state",
    );
    expect(screen.getByTestId("o3-benchmark-requirement")).toHaveTextContent(
      "tb4.cr-systems-db-v1",
    );
    expect(screen.getByText("Provisional evidence")).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("o3-routing-details-toggle"));
    expect(screen.getByTestId("o3-routing-details")).toHaveTextContent("test://tb4/proxy");
    expect(screen.getByTestId("o3-routing-details")).toHaveTextContent(
      "no exact benchmark evidence",
    );
  });

  it("adjusts the slice and difficulty before deterministic revalidation", async () => {
    const { onAdjust } = renderCard();
    fireEvent.click(screen.getByTestId("o3-adjust"));
    fireEvent.change(screen.getByTestId("o3-adjust-slice"), {
      target: { value: "terminal-bench|4.0.0|tb4.cr-ai-v1" },
    });
    fireEvent.change(screen.getByTestId("o3-adjust-difficulty"), { target: { value: "hard" } });
    fireEvent.change(screen.getByTestId("o3-adjust-effort"), { target: { value: "high" } });
    fireEvent.click(screen.getByTestId("o3-adjust-save"));

    await waitFor(() =>
      expect(onAdjust).toHaveBeenCalledWith(
        expect.objectContaining({
          slice_id: "tb4.cr-ai-v1",
          difficulty: "hard",
          reasoning_effort: "high",
        }),
      ),
    );
  });

  it("offers an explicit decomposition review before showing the split", () => {
    const base = proposal();
    renderCard(
      proposal({
        disposition: "decompose",
        adviser: {
          ...base.adviser,
          disposition: "decompose",
          decomposition: [
            {
              objective: "Inspect the failing subsystem",
              dependency_order: 1,
              benchmark_id: "terminal-bench",
              version: "4.0.0",
              slice_id: "tb4.cr-systems-db-v1",
              difficulty: "easy",
              reasoning_effort: "low",
              risk: "low",
              passing_candidates: ["opencode-big-pickle-low"],
              competence_reduction: "Limits the first pass to one independent subsystem.",
              blocked: false,
            },
          ],
        },
      }),
    );

    expect(screen.queryByTestId("o3-decomposition")).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId("o3-review-split"));
    expect(screen.getByTestId("o3-decomposition")).toHaveTextContent(
      "Inspect the failing subsystem",
    );
    expect(screen.getByTestId("o3-review-split")).toHaveTextContent("Hide split");
  });

  it("requires deliberate provisional acknowledgement before approval handoff", async () => {
    const { onDecision, onApproved } = renderCard();
    fireEvent.click(screen.getByTestId("o3-approve"));

    await waitFor(() =>
      expect(onDecision).toHaveBeenCalledWith({
        action: "approve",
        acknowledge_provisional: true,
      }),
    );
    await waitFor(() => expect(onApproved).toHaveBeenCalledTimes(1));
  });

  it.each(["decline", "defer"] as const)("records %s without launching", async (action) => {
    const { onDecision, onApproved } = renderCard();
    fireEvent.click(screen.getByTestId(`o3-${action}`));

    await waitFor(() => expect(onDecision).toHaveBeenCalledWith({ action }));
    expect(onApproved).not.toHaveBeenCalled();
  });

  it("requires a persisted reason before confirmed run-anyway", async () => {
    const noRoute = proposal({
      evaluations: proposal().evaluations.map((item) => ({
        ...item,
        status: "excluded",
        evidence_class: "unknown",
        admission_score: null,
        ranking: null,
        evidence: [],
        exclusions: ["unknown benchmark evidence cannot qualify a provisional route"],
      })),
      frontier: {
        ...proposal().frontier,
        provisional_candidates: [],
        capability_gap: "No source-pool configuration satisfies all approved hard constraints.",
      },
      disposition: "decompose",
    });
    const { onDecision, onApproved } = renderCard(noRoute);
    fireEvent.click(screen.getByTestId("o3-run-anyway"));
    const confirm = screen.getByTestId("o3-run-anyway-confirm");
    expect(confirm).toBeDisabled();

    fireEvent.change(screen.getByTestId("o3-run-anyway-reason"), {
      target: { value: "Live compatibility probe passed" },
    });
    fireEvent.click(confirm);

    await waitFor(() =>
      expect(onDecision).toHaveBeenCalledWith({
        action: "run_anyway",
        confirm_run_anyway: true,
        reason: "Live compatibility probe passed",
      }),
    );
    await waitFor(() => expect(onApproved).toHaveBeenCalledTimes(1));
  });

  it("resumes an approved launch after the linked session survives a reload", async () => {
    const approved = proposal({
      decision: "approve",
      derived_combo_name: "custom/o3-route-0123456789ab",
      derived_combo_definition: { name: "custom/o3-route-0123456789ab" },
      session_id: "conv_o3_retry",
    });
    const { onDecision, onApproved } = renderCard(approved);

    fireEvent.click(screen.getByTestId("o3-approve"));

    await waitFor(() => expect(onApproved).toHaveBeenCalledWith(approved));
    expect(onDecision).not.toHaveBeenCalled();
    expect(screen.getByTestId("o3-approve")).toHaveTextContent("Resume approved launch");
    expect(screen.getByTestId("o3-approved-route")).toHaveTextContent(
      "custom/o3-route-0123456789ab",
    );
    expect(screen.queryByTestId("o3-adjust")).not.toBeInTheDocument();
    expect(screen.queryByTestId("o3-defer")).not.toBeInTheDocument();
    expect(screen.queryByTestId("o3-decline")).not.toBeInTheDocument();
    expect(screen.queryByTestId("o3-run-anyway")).not.toBeInTheDocument();
  });
});
