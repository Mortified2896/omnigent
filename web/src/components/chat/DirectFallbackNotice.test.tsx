import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { DirectFallbackNotice } from "./DirectFallbackNotice";

const snapshot = vi.hoisted(() => ({ labels: {} as Record<string, string>, status: "idle" }));
const invalidateQueries = vi.hoisted(() => vi.fn());
vi.mock("@/hooks/useSession", () => ({ useSession: () => ({ session: snapshot }) }));
vi.mock("@tanstack/react-query", () => ({ useQueryClient: () => ({ invalidateQueries }) }));
vi.mock("@/store/chatStore", () => ({ useChatStore: () => snapshot.status }));
afterEach(cleanup);

it("does not warn for an intentionally selected Direct connection", () => {
  snapshot.labels = { "omnigent.access_lane": "codex-direct" };
  render(<DirectFallbackNotice sessionId="chat" />);
  expect(screen.queryByRole("alert")).toBeNull();
});

it("keeps the warning and expandable diagnostics when reopening a recovered chat", () => {
  snapshot.labels = {
    "omnigent.access_lane": "codex-direct",
    "omnigent.transport.fallback_from": "omniroute",
    "omnigent.transport.fallback_reason": "codex_turn_error",
    "omnigent.transport.fallback_at": "1792000000",
  };
  const view = render(<DirectFallbackNotice sessionId="chat" />);
  expect(screen.getByRole("alert")).toHaveTextContent("Direct fallback active");
  expect(screen.getByRole("alert")).toHaveTextContent("Investigate the gateway issue");
  fireEvent.click(screen.getByText("Fallback details"));
  expect(screen.getByText("Recorded reason: codex_turn_error").closest("details")).toHaveAttribute(
    "open",
  );
  view.unmount();
  render(<DirectFallbackNotice sessionId="chat" />);
  expect(screen.getByRole("alert")).toHaveTextContent("Direct fallback active");
  expect(screen.getByRole("alert").querySelector("time")).toHaveAttribute("dateTime");
});

it("retains the history without claiming Direct is active after a route change", () => {
  snapshot.labels = {
    "omnigent.access_lane": "omniroute",
    "omnigent.transport.fallback_from": "omniroute",
    "omnigent.transport.fallback_at": "invalid",
  };
  render(<DirectFallbackNotice sessionId="chat" />);
  expect(screen.getByRole("alert")).toHaveTextContent("This chat used Direct fallback");
  expect(screen.queryByText("Direct fallback active")).toBeNull();
  expect(screen.getByRole("alert").querySelector("time")).toBeNull();
});

it("refreshes fallback provenance when a failed chat restarts without navigation", () => {
  snapshot.labels = {};
  snapshot.status = "failed";
  invalidateQueries.mockClear();
  const view = render(<DirectFallbackNotice sessionId="chat" />);
  expect(invalidateQueries).not.toHaveBeenCalled();
  snapshot.status = "running";
  view.rerender(<DirectFallbackNotice sessionId="chat" />);
  expect(invalidateQueries).toHaveBeenCalledWith({ queryKey: ["session", "chat"] });
});
