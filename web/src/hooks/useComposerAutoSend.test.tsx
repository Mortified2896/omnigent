import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { useComposerAutoSend } from "./useComposerAutoSend";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});
it("sends exactly once at five seconds using the latest callback", () => {
  vi.useFakeTimers();
  const first = vi.fn();
  const latest = vi.fn();
  const { result, rerender } = renderHook(({ send }) => useComposerAutoSend(send), {
    initialProps: { send: first },
  });
  act(() => result.current.start());
  act(() => vi.advanceTimersByTime(4900));
  expect(first).not.toHaveBeenCalled();
  rerender({ send: latest });
  act(() => vi.advanceTimersByTime(100));
  expect(latest).toHaveBeenCalledOnce();
  act(() => vi.advanceTimersByTime(10000));
  expect(latest).toHaveBeenCalledOnce();
});
it("editing cancellation and unmount prevent delayed sends", () => {
  vi.useFakeTimers();
  const send = vi.fn();
  const { result, unmount } = renderHook(() => useComposerAutoSend(send));
  act(() => result.current.start());
  act(() => vi.advanceTimersByTime(2000));
  act(() => result.current.stop());
  act(() => vi.advanceTimersByTime(5000));
  expect(send).not.toHaveBeenCalled();
  act(() => result.current.start());
  unmount();
  act(() => vi.advanceTimersByTime(5000));
  expect(send).not.toHaveBeenCalled();
});
it("hiding the page cancels the countdown", () => {
  vi.useFakeTimers();
  const send = vi.fn();
  const { result } = renderHook(() => useComposerAutoSend(send));
  act(() => result.current.start());
  const hidden = vi.spyOn(document, "hidden", "get").mockReturnValue(true);
  act(() => document.dispatchEvent(new Event("visibilitychange")));
  act(() => vi.advanceTimersByTime(5000));
  expect(send).not.toHaveBeenCalled();
  hidden.mockRestore();
});
