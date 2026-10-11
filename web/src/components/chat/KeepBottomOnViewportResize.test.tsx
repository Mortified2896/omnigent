import { act, cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { KeepBottomOnViewportResize } from "./chatBubbleParts";

const scrollContext = vi.hoisted(() => ({
  scrollRef: { current: null as HTMLElement | null },
  state: { isAtBottom: true, escapedFromLock: false },
  scrollToBottom: vi.fn(),
}));
vi.mock("use-stick-to-bottom", () => ({ useStickToBottomContext: () => scrollContext }));

const resizeCallbacks: ResizeObserverCallback[] = [];
const frames: FrameRequestCallback[] = [];

beforeEach(() => {
  resizeCallbacks.length = 0;
  frames.length = 0;
  scrollContext.scrollToBottom.mockClear();
  scrollContext.state.isAtBottom = true;
  scrollContext.state.escapedFromLock = false;
  const el = document.createElement("div");
  Object.defineProperties(el, {
    clientHeight: { value: 400, configurable: true },
    scrollHeight: { value: 1000 },
  });
  el.scrollTop = 600;
  scrollContext.scrollRef.current = el;
  vi.stubGlobal(
    "ResizeObserver",
    class {
      constructor(callback: ResizeObserverCallback) {
        resizeCallbacks.push(callback);
      }
      observe() {}
      disconnect() {}
    },
  );
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
    frames.push(callback);
    return frames.length;
  });
  vi.stubGlobal("cancelAnimationFrame", vi.fn());
});

afterEach(() => {
  cleanup();
  scrollContext.scrollRef.current?.remove();
  vi.unstubAllGlobals();
});

function shrinkViewport() {
  Object.defineProperty(scrollContext.scrollRef.current, "clientHeight", { value: 200 });
  act(() => resizeCallbacks[0]!([], {} as ResizeObserver));
}

it("keeps feedback in place when the keyboard shrinks a viewport after follow was released", () => {
  render(<KeepBottomOnViewportResize />);
  // No scroll event has occurred since interacting with feedback, so the
  // resize observer's physical-bottom snapshot still says pinned.
  scrollContext.state.isAtBottom = false;
  scrollContext.state.escapedFromLock = true;
  shrinkViewport();

  expect(scrollContext.scrollRef.current!.scrollTop).toBe(600);
  expect(scrollContext.scrollToBottom).not.toHaveBeenCalled();
  expect(frames).toHaveLength(0);
});

it("still follows ordinary composer growth when the reader remains at the bottom", () => {
  render(<KeepBottomOnViewportResize />);
  shrinkViewport();

  expect(scrollContext.scrollRef.current!.scrollTop).toBe(799);
  expect(scrollContext.scrollToBottom).toHaveBeenCalledOnce();
  act(() => frames[0]!(0));
  expect(scrollContext.scrollToBottom).toHaveBeenCalledTimes(2);
});

it("holds a focused review even if revealing the field made the pane near-bottom again", () => {
  const el = scrollContext.scrollRef.current!;
  const review = document.createElement("div");
  review.dataset.feedbackReview = "";
  const field = document.createElement("textarea");
  review.append(field);
  el.append(review);
  document.body.append(el);
  render(<KeepBottomOnViewportResize />);
  field.focus();
  shrinkViewport();

  expect(scrollContext.scrollRef.current!.scrollTop).toBe(600);
  expect(scrollContext.scrollToBottom).not.toHaveBeenCalled();
});

it("drops a pending follow frame when the reader starts editing feedback", () => {
  render(<KeepBottomOnViewportResize />);
  shrinkViewport();
  scrollContext.state.isAtBottom = false;
  scrollContext.state.escapedFromLock = true;
  scrollContext.scrollRef.current!.scrollTop = 600;
  act(() => frames[0]!(0));

  expect(scrollContext.scrollRef.current!.scrollTop).toBe(600);
  expect(scrollContext.scrollToBottom).toHaveBeenCalledOnce();
});
