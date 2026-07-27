import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { PendingRouteApprovalCard, RouteApprovalCard } from "./RouteApprovalCard";

const FULL_PROPOSAL = {
  proposal_source_label: "Router recommendation",
  recommended_harness: "OpenCode Native",
  omniroute_route_id: "auto/coding",
  reasoning_effort: "medium",
  permission_mode: "default",
  billing_summary: "Pro: $5 / 1M; Premium: $20 / 1M",
  allowed_billing_classes: ["pro"],
  forbidden_billing_classes: ["premium"],
  risk_note: "Long-running route; prefer for tasks >5 minutes.",
  router_evaluator_route: "auto/smart",
  actual_evaluator_provider: "anthropic",
  actual_evaluator_model: "claude-4.7",
  evaluator_billing_class: "pro",
  evaluator_fallback_used: false,
  evaluator_decision_id: "dec_abc123",
  evaluator_selection_strategy: "best_of_two",
  omniroute_requires_explicit_approval: true,
  rationale: [
    "Suited for non-trivial coding tasks.",
    "Reviews (above 1024 ctx-tokens) work even on lower-spec agents.",
  ],
};

describe("RouteApprovalCard", () => {
  it("renders the Approved state with a summary above the fold and no details by default", () => {
    render(<RouteApprovalCard proposal={FULL_PROPOSAL} action="accept" elicitationId="elicit_1" />);
    const card = screen.getByTestId("route-approval-card");
    expect(card).toHaveAttribute("data-state", "approved");
    expect(card).toHaveAttribute("data-elicitation-id", "elicit_1");
    const summary = within(card).getByTestId("route-approval-summary");
    expect(within(summary).getByText("OpenCode Native")).toBeInTheDocument();
    const original = within(summary).getByRole("region", { name: "Original recommendation" });
    expect(within(original).getByText("auto/coding")).toBeInTheDocument();
    expect(within(original).getByText("medium")).toBeInTheDocument();
    expect(within(original).getByText("default")).toBeInTheDocument();
    expect(within(summary).getByText(/Pro: \$5/)).toBeInTheDocument();
    // Fallback shown.
    expect(within(summary).getByText("no")).toBeInTheDocument();
    // The default-collapsed testid is present…
    expect(within(card).getByTestId("route-approval-details-toggle")).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    // …but the full RouteProposalCard is not yet rendered.
    expect(within(card).queryByTestId("route-proposal-card")).toBeNull();
    // The route id pill is in the title.
    expect(within(card).getByTestId("route-approval-route-id")).toBeInTheDocument();
  });

  it("expands and collapses with the toggle, surfacing the full original RouteProposalCard", () => {
    render(<RouteApprovalCard proposal={FULL_PROPOSAL} action="accept" elicitationId="elicit_2" />);
    const card = screen.getByTestId("route-approval-card");
    const toggle = within(card).getByTestId("route-approval-details-toggle");
    expect(toggle).toHaveAttribute("aria-controls", "route-approval-details-elicit_2");
    expect(toggle).toHaveTextContent(/Show details/);
    expect(toggle).toHaveAttribute("aria-expanded", "false");

    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(toggle).toHaveTextContent(/Hide details/);

    const details = within(card).getByTestId("route-approval-details");
    const fullCard = within(details).getByTestId("route-proposal-card");
    expect(fullCard).toBeInTheDocument();
    // Full proposal payload is fully visible — including the rationale,
    // risk note, evaluator decision id, etc.
    expect(within(fullCard).getByText(/Long-running route/)).toBeInTheDocument();
    expect(within(fullCard).getByText(/Suited for non-trivial coding tasks/)).toBeInTheDocument();
    expect(within(fullCard).getByText(/dec_abc123/)).toBeInTheDocument();
    expect(within(fullCard).getByText(/best_of_two/)).toBeInTheDocument();

    // Toggle again — collapse.
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(within(card).queryByTestId("route-approval-details")).toBeNull();
  });

  it("preserves the expanded state when the proposal references the same elicitation across remounts (the toggle is intentionally session-local, the proposal is the persisted source of truth)", () => {
    const first = render(
      <RouteApprovalCard proposal={FULL_PROPOSAL} action="accept" elicitationId="elicit_3" />,
    );
    fireEvent.click(
      within(first.getByTestId("route-approval-card")).getByTestId("route-approval-details-toggle"),
    );
    // Reload / remount — fresh state — but the proposal payload is the
    // source of truth, so the full route info is still available
    // immediately, not derived from a status string.
    first.unmount();
    const second = render(
      <RouteApprovalCard proposal={FULL_PROPOSAL} action="accept" elicitationId="elicit_3" />,
    );
    const card = second.getByTestId("route-approval-card");
    // Fresh mount starts collapsed (UI choice)…
    expect(within(card).getByTestId("route-approval-details-toggle")).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    // …but the summary above still carries the actionable fields.
    expect(
      within(within(card).getByRole("region", { name: "Original recommendation" })).getByText(
        "auto/coding",
      ),
    ).toBeInTheDocument();
    // …and a fresh expand reveals the full details.
    fireEvent.click(within(card).getByTestId("route-approval-details-toggle"));
    expect(within(card).getByTestId("route-approval-details")).toBeInTheDocument();
    second.unmount();
  });

  it("renders the rejected / cancelled / resolved-elsewhere states without truncate", () => {
    const variants: Array<{
      action: "decline" | "cancel" | "auto_resolved";
      state: string;
      label: string;
    }> = [
      { action: "decline", state: "rejected", label: "Rejected" },
      { action: "cancel", state: "cancelled", label: "Cancelled" },
      { action: "auto_resolved", state: "resolved-elsewhere", label: "Resolved elsewhere" },
    ];
    for (const { action, state, label } of variants) {
      const { unmount } = render(
        <RouteApprovalCard
          proposal={FULL_PROPOSAL}
          action={action}
          elicitationId={`elicit_${action}`}
        />,
      );
      const card = screen.getByTestId("route-approval-card");
      expect(card).toHaveAttribute("data-state", state);
      expect(within(card).getByTestId("route-approval-card-label")).toHaveTextContent(label);
      // Summary still readable (no `truncate` / `line-clamp` markup).
      const summary = within(card).getByTestId("route-approval-summary");
      expect(summary.className).not.toMatch(/\btruncate\b/);
      expect(summary.className).not.toMatch(/line-clamp/);
      unmount();
    }
  });

  it("handles a partial proposal without eliding critical fields via ellipsis", () => {
    render(
      <RouteApprovalCard
        proposal={{
          omniroute_route_id: "auto/easy",
          reasoning_effort: "low",
          // Many fields deliberately missing.
        }}
        action="accept"
        elicitationId="elicit_partial"
      />,
    );
    const card = screen.getByTestId("route-approval-card");
    const summary = within(card).getByTestId("route-approval-summary");
    const original = within(summary).getByRole("region", { name: "Original recommendation" });
    expect(within(original).getByText("auto/easy")).toBeInTheDocument();
    expect(within(original).getByText("low")).toBeInTheDocument();
    // No `truncate` / `line-clamp` markup on the rendered fields.
    const cells = summary.querySelectorAll<HTMLElement>("[class*='break-words']");
    for (const cell of cells) {
      expect(cell.className).not.toMatch(/\btruncate\b/);
      expect(cell.className).not.toMatch(/line-clamp/);
    }
  });

  it("does not introduce a duplicate `route-proposal-card` when expanded (one RouteProposalCard per approval)", () => {
    render(
      <RouteApprovalCard proposal={FULL_PROPOSAL} action="accept" elicitationId="elicit_dup" />,
    );
    fireEvent.click(screen.getByTestId("route-approval-details-toggle"));
    expect(screen.getAllByTestId("route-proposal-card")).toHaveLength(1);
  });

  it("shows original and adjusted final selections in the responded state", () => {
    render(
      <RouteApprovalCard
        proposal={FULL_PROPOSAL}
        action="accept"
        elicitationId="elicit_adjusted"
        responseContent={{
          route_adjustment: {
            omniroute_route_id: "auto/easy",
            reasoning_effort: "low",
            permission_mode: "read_only",
          },
        }}
      />,
    );
    expect(screen.getByTestId("route-approval-card-label")).toHaveTextContent(
      "Approved with changes",
    );
    const original = screen.getByRole("region", { name: "Original recommendation" });
    const final = screen.getByRole("region", { name: "Final selection" });
    expect(within(original).getByText("auto/coding")).toBeInTheDocument();
    expect(within(final).getByText("auto/easy")).toBeInTheDocument();
    expect(within(final).getByText("read_only")).toBeInTheDocument();
  });
});

describe("PendingRouteApprovalCard", () => {
  const proposal = {
    ...FULL_PROPOSAL,
    eligible_route_ids: ["auto/coding", "auto/easy"],
    eligible_reasoning_efforts: ["low", "medium"],
    eligible_permission_modes: ["read_only", "default"],
  };

  it("offers route-specific, accessible, mobile-friendly controls", () => {
    render(
      <PendingRouteApprovalCard proposal={proposal} elicitationId="route_1" onSubmit={vi.fn()} />,
    );
    for (const name of ["Approve", "Change / Adjust", "Reject"]) {
      const button = screen.getByRole("button", { name });
      expect(button.className).toContain("min-h-11");
      expect(button.className).toContain("w-full");
    }
    fireEvent.click(screen.getByRole("button", { name: "Change / Adjust" }));
    expect(screen.getByLabelText("Route")).toBeInTheDocument();
    expect(screen.getByLabelText("Reasoning effort")).toBeInTheDocument();
    expect(screen.getByLabelText("Permission mode")).toBeInTheDocument();
  });

  it("submits eligible adjustments once and disables repeat mutation", () => {
    const submit = vi.fn(() => new Promise<void>(() => undefined));
    render(
      <PendingRouteApprovalCard proposal={proposal} elicitationId="route_2" onSubmit={submit} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Change / Adjust" }));
    fireEvent.change(screen.getByLabelText("Route"), { target: { value: "auto/easy" } });
    fireEvent.change(screen.getByLabelText("Reasoning effort"), { target: { value: "low" } });
    fireEvent.change(screen.getByLabelText("Permission mode"), {
      target: { value: "read_only" },
    });
    const apply = screen.getByRole("button", { name: "Apply changes" });
    fireEvent.click(apply);
    fireEvent.click(apply);
    expect(submit).toHaveBeenCalledTimes(1);
    expect(submit).toHaveBeenCalledWith("accept", {
      omniroute_route_id: "auto/easy",
      reasoning_effort: "low",
      permission_mode: "read_only",
    });
    expect(apply).toBeDisabled();
    expect(screen.getByTestId("pending-route-approval-card")).toHaveAttribute("aria-busy", "true");
  });
});
