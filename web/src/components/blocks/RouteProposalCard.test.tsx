import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { RouteProposalCard } from "./RouteProposalCard";

afterEach(cleanup);

describe("RouteProposalCard", () => {
  it("shows native route id and reasoning separately", () => {
    render(
      <RouteProposalCard
        proposal={{
          proposal_source_label: "Router recommendation",
          recommended_harness: "OpenCode Native",
          omniroute_route_id: "auto/coding",
          reasoning_effort: "medium",
          permission_mode: "ask_before_edits",
          billing_summary: "free, subscription allowed; api_billed, unknown forbidden",
          risk_note: "Standard coding route.",
          rationale: ["Normal repository coding task."],
        }}
      />,
    );

    expect(screen.getByTestId("route-proposal-card")).toBeDefined();
    expect(screen.getByText(/Proposal source: Router recommendation/)).toBeDefined();
    expect(screen.getByText(/Harness:/)).toHaveTextContent("OpenCode Native");
    expect(screen.getByText("auto/coding")).toBeDefined();
    expect(screen.getByText(/Reasoning effort:/)).toHaveTextContent("medium");
    expect(screen.getByText(/Permission mode:/)).toHaveTextContent("ask_before_edits");
  });

  it("warns for explicit approval routes", () => {
    render(
      <RouteProposalCard
        proposal={{
          omniroute_route_id: "auto/coding:pro",
          reasoning_effort: "high",
          omniroute_requires_explicit_approval: true,
          rationale: ["Hard coding task."],
        }}
      />,
    );

    expect(screen.getByText(/Explicit approval required/)).toBeDefined();
    expect(screen.getByText(/pro\/premium routing/)).toBeDefined();
  });
});
