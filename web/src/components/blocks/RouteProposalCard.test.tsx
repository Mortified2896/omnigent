import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { RouteProposalCard, type RouteProposalPayload } from "./RouteProposalCard";

const proposal: RouteProposalPayload = {
  proposal_id: "route_1",
  task_type: "coding",
  recommended_harness: "OpenCode native",
  model_policy: "coding/default subscription-or-free lane",
  model_lane: "coding/default subscription-or-free lane",
  preferred_model: null,
  reasoning_effort: "medium",
  permission_mode: "ask before edits",
  allowed_billing_classes: ["subscription", "free", "local", "session", "no-cost"],
  forbidden_billing_classes: ["api_billed", "unknown"],
  execution_fallback_policy: "MiniMax-M3 subscription only, no API-billed fallback",
  alternatives: [{ harness: "pi", model_policy: "review lane", rationale: "Read-only review" }],
  rationale: "This looks like a coding/repo task.",
  router_primary_profile: {
    profile: "gpt-5.5-small-reasoning",
    model_family: "GPT-5.5",
    reasoning: "small",
    role: "route_recommender",
  },
  router_fallback_profile: {
    profile: "minimax-m3-routing",
    model_family: "MiniMax-M3",
    reasoning: "provider_default",
    role: "route_recommender",
  },
  router_used_profile: {
    profile: "gpt-5.5-small-reasoning",
    model_family: "GPT-5.5",
    reasoning: "small",
    role: "route_recommender",
  },
  router_fallback_used: false,
  router_invoked: false,
  proposal_source: "default_route_policy",
  proposal_source_label: "Default route policy proposal",
  non_api_billed_constraint: "Execution policy: non-API only; API-billed fallback: forbidden",
};

afterEach(() => cleanup());

describe("RouteProposalCard", () => {
  it("renders route proposal and non-API policy", () => {
    render(
      <RouteProposalCard
        elicitationId="elic_route_1"
        proposal={proposal}
        status="pending"
        response={null}
        onSubmit={vi.fn()}
      />,
    );

    expect(screen.getByText("Execution Route Proposal")).toBeDefined();
    expect(screen.getByText("Default route policy proposal")).toBeDefined();
    expect(screen.getByText("GPT-5.5 · small reasoning (not invoked)")).toBeDefined();
    expect(screen.getByText("Configured fallback: MiniMax-M3 (not invoked)")).toBeDefined();
    expect(screen.getByText("OpenCode native")).toBeDefined();
    expect(screen.getByText("Non-API only")).toBeDefined();
    expect(screen.getByText("forbidden")).toBeDefined();
  });

  it("submits approve, modify approve, decline, and cancel", () => {
    const onSubmit = vi.fn();
    render(
      <RouteProposalCard
        elicitationId="elic_route_1"
        proposal={proposal}
        status="pending"
        response={null}
        onSubmit={onSubmit}
      />,
    );

    fireEvent.change(screen.getByPlaceholderText("Optional note for this routing decision"), {
      target: { value: "ship it" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^approve/i }));
    expect(onSubmit).toHaveBeenCalledWith("elic_route_1", "accept", { comment: "ship it" });

    fireEvent.click(screen.getByRole("button", { name: /modify \+ approve/i }));
    expect(onSubmit).toHaveBeenLastCalledWith(
      "elic_route_1",
      "accept",
      expect.objectContaining({
        model_lane: "coding/default subscription-or-free lane",
        reasoning_effort: "medium",
        permission_mode: "ask before edits",
        comment: "ship it",
      }),
    );

    fireEvent.click(screen.getByRole("button", { name: /decline/i }));
    expect(onSubmit).toHaveBeenLastCalledWith("elic_route_1", "decline", { comment: "ship it" });

    fireEvent.click(screen.getByRole("button", { name: /cancel/i }));
    expect(onSubmit).toHaveBeenLastCalledWith("elic_route_1", "cancel", { comment: "ship it" });
  });
});
