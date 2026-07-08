import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { RouteApprovalControl } from "./RouteApprovalControl";

afterEach(() => cleanup());

describe("RouteApprovalControl", () => {
  it("renders enabled when server capability is true", () => {
    render(<RouteApprovalControl value="off" onChange={vi.fn()} serverEnabled />);

    const trigger = screen.getByTestId("route-approval-toggle");
    expect(trigger.hasAttribute("disabled")).toBe(false);
    expect(trigger.getAttribute("data-server-enabled")).toBe("true");
  });

  it("renders disabled when server capability is false", () => {
    render(<RouteApprovalControl value="off" onChange={vi.fn()} serverEnabled={false} />);

    const trigger = screen.getByTestId("route-approval-toggle");
    expect(trigger.hasAttribute("disabled")).toBe(true);
    expect(trigger.getAttribute("data-server-enabled")).toBe("false");
  });

  it("flips on/off and reports the new mode", () => {
    const onChange = vi.fn();
    render(<RouteApprovalControl value="off" onChange={onChange} serverEnabled />);

    fireEvent.click(screen.getByTestId("route-approval-toggle"));
    expect(onChange).toHaveBeenCalledWith("on");

    cleanup();
    render(<RouteApprovalControl value="on" onChange={onChange} serverEnabled />);

    fireEvent.click(screen.getByTestId("route-approval-toggle"));
    expect(onChange).toHaveBeenCalledWith("off");
  });
});
