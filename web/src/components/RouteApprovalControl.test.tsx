import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { RouteApprovalControl } from "./RouteApprovalControl";

afterEach(cleanup);

describe("RouteApprovalControl", () => {
  it("keeps manual-mode preservation in hover help, not inline composer text", () => {
    render(<RouteApprovalControl enabled={false} onChange={() => undefined} />);
    const control = screen.getByTestId("route-approval-control");
    expect(control).toHaveTextContent("Model RoutingManual");
    expect(control).not.toHaveTextContent(
      "manual harness, model/route, and reasoning selections are preserved",
    );
    expect(control).toHaveAttribute(
      "title",
      "Manual harness, model/route, and reasoning selections are preserved.",
    );
  });

  it("calls onChange when toggled", () => {
    const onChange = vi.fn();
    render(<RouteApprovalControl enabled={false} onChange={onChange} />);
    fireEvent.click(screen.getByRole("switch", { name: /model routing agent/i }));
    expect(onChange).toHaveBeenCalledWith(true);
  });

  it("reflects the enabled state in the switch aria-checked attribute", () => {
    const { rerender } = render(
      <RouteApprovalControl enabled={false} onChange={() => undefined} />,
    );
    expect(screen.getByRole("switch", { name: /model routing agent/i })).toHaveAttribute(
      "aria-checked",
      "false",
    );
    rerender(<RouteApprovalControl enabled={true} onChange={() => undefined} />);
    expect(screen.getByRole("switch", { name: /model routing agent/i })).toHaveAttribute(
      "aria-checked",
      "true",
    );
  });

  it("forwards the disabled prop to the underlying switch", () => {
    render(<RouteApprovalControl enabled={false} disabled={true} onChange={() => undefined} />);
    expect(screen.getByRole("switch", { name: /model routing agent/i })).toBeDisabled();
  });
});
