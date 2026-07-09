import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { RouteApprovalControl } from "./RouteApprovalControl";

afterEach(cleanup);

describe("RouteApprovalControl", () => {
  it("explains manual-mode preservation when disabled", () => {
    render(<RouteApprovalControl enabled={false} onChange={() => undefined} />);
    expect(screen.getByTestId("route-approval-control")).toHaveTextContent(
      "manual harness, model/route, and reasoning selections are preserved",
    );
  });

  it("calls onChange when toggled", () => {
    const onChange = vi.fn();
    render(<RouteApprovalControl enabled={false} onChange={onChange} />);
    fireEvent.click(screen.getByRole("switch", { name: /model routing agent/i }));
    expect(onChange).toHaveBeenCalledWith(true);
  });
});
