import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { handoffPromptTiming, startPromptTiming, estimateDuration } from "@/lib/promptTiming";
import { ResponseTiming } from "./ResponseTiming";

const fixture = vi.hoisted(() => ({
  state: {} as Record<string, unknown>,
  bubbles: [] as unknown[],
}));
vi.mock("@/hooks/useConversationEntryState", () => ({
  useConversationEntryState: () => fixture.state,
}));
vi.mock("@/lib/renderItems", () => ({ buildBubbles: () => fixture.bubbles }));
vi.mock("@/lib/identity", () => ({ getCurrentUserId: () => "tester" }));
beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  vi.useFakeTimers();
  fixture.state = {
    status: "idle",
    sessionStatus: "idle",
    sessionHarness: "codex-native",
    sessionModelOverride: "gpt-6.1-sol",
    sessionReasoningEffort: "high",
    blocks: [],
    activeResponse: null,
  };
  fixture.bubbles = [
    { kind: "assistant", responseId: "previous", lifecycle: "completed", workedForS: 2 },
  ];
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});
it("shows timing only while running and does not learn from a cancelled turn", () => {
  const view = render(<ResponseTiming sessionId="cancelled" />);
  expect(screen.queryByRole("status")).not.toBeInTheDocument();
  fixture.state = { ...fixture.state, status: "streaming", sendLatchedAt: Date.now() };
  view.rerender(<ResponseTiming sessionId="cancelled" />);
  expect(screen.getByRole("status")).toHaveTextContent("About 1m left");
  act(() => vi.advanceTimersByTime(3000));
  expect(screen.getByRole("status")).toHaveTextContent("About 57s left");
  fixture.state = { ...fixture.state, status: "idle" };
  view.rerender(<ResponseTiming sessionId="cancelled" />);
  expect(localStorage.length).toBe(0);
});
it("learns a completed reply and shares the estimate with the sidebar", () => {
  fixture.state = { ...fixture.state, status: "streaming", sendLatchedAt: Date.now() };
  const view = render(<ResponseTiming sessionId="learned" />);
  act(() => vi.advanceTimersByTime(120_000));
  fixture.bubbles = [
    ...fixture.bubbles,
    { kind: "assistant", responseId: "new", lifecycle: "completed", workedForS: 120 },
  ];
  fixture.state = { ...fixture.state, status: "idle" };
  view.rerender(<ResponseTiming sessionId="learned" />);
  fixture.state = { ...fixture.state, status: "streaming", sendLatchedAt: Date.now() };
  view.rerender(<ResponseTiming sessionId="learned" compact />);
  expect(screen.getByText("~2m left")).toBeInTheDocument();
});
it("estimates from observed replies with a bounded default before learning", () => {
  expect(estimateDuration([])).toBe(60);
  expect(estimateDuration([10, 20, 30, 40, 100])).toBe(40);
});

it("carries one estimate through Advisor, startup, response and sidebar, learning the whole prompt", () => {
  const timing = startPromptTiming();
  act(() => vi.advanceTimersByTime(20_000));
  handoffPromptTiming("advisor-handoff", timing);
  const view = render(<ResponseTiming sessionId="advisor-handoff" />);
  expect(screen.getByRole("status")).toHaveTextContent("About 40s left");
  fixture.state = { ...fixture.state, status: "streaming", sendLatchedAt: Date.now() };
  view.rerender(<ResponseTiming sessionId="advisor-handoff" />);
  expect(screen.getByRole("status")).toHaveTextContent("About 40s left");
  act(() => vi.advanceTimersByTime(10_000));
  view.rerender(<ResponseTiming sessionId="advisor-handoff" compact />);
  expect(screen.getByText("~30s left")).toBeInTheDocument();
  act(() => vi.advanceTimersByTime(35_000));
  view.rerender(<ResponseTiming sessionId="advisor-handoff" />);
  expect(screen.getByRole("status")).toHaveTextContent("Taking longer than estimated");
  fixture.bubbles = [
    ...fixture.bubbles,
    { kind: "assistant", responseId: "full-prompt", lifecycle: "completed", workedForS: 45 },
  ];
  fixture.state = { ...fixture.state, status: "idle" };
  view.rerender(<ResponseTiming sessionId="advisor-handoff" />);
  expect(screen.queryByRole("status")).not.toBeInTheDocument();
  expect(startPromptTiming()).toMatchObject({ estimatedSeconds: 65, sampleCount: 1 });
  expect(sessionStorage.length).toBe(0);
});

it("clears a handed-off estimate when chat startup fails without learning a success", () => {
  handoffPromptTiming("failed-startup", startPromptTiming());
  fixture.state = { ...fixture.state, sessionStatus: "failed" };
  render(<ResponseTiming sessionId="failed-startup" />);
  expect(screen.queryByRole("status")).not.toBeInTheDocument();
  expect(localStorage.length).toBe(0);
  expect(sessionStorage.length).toBe(0);
});
