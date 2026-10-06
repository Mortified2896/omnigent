import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { SuggestedFeedback } from "./SuggestedFeedback";

const original = { outcome: "partial", comment: "Instructions issue", tags: ["Instructions"] };
const proposed = { comment: "The dependency was unavailable", tags: ["Environment"] };
beforeEach(() => localStorage.clear());
afterEach(cleanup);
it("shows proposed changes immediately and saves only on acceptance", async () => {
  const apply = vi.fn().mockResolvedValue(undefined);
  render(
    <SuggestedFeedback
      id="branch:answer"
      original={original}
      proposed={proposed}
      onApply={apply}
    />,
  );
  expect(screen.getByLabelText("Suggested feedback")).toHaveAttribute("open");
  expect(apply).not.toHaveBeenCalled();
  fireEvent.click(screen.getByText("Accept changes"));
  await waitFor(() => expect(screen.getByText("Undo")).toBeInTheDocument());
  expect(apply).toHaveBeenCalledWith(proposed);
  cleanup();
  render(
    <SuggestedFeedback
      id="branch:answer"
      original={{ ...original, ...proposed }}
      proposed={proposed}
      onApply={apply}
    />,
  );
  fireEvent.click(screen.getByText("Undo"));
  await waitFor(() =>
    expect(apply).toHaveBeenLastCalledWith({ comment: original.comment, tags: original.tags }),
  );
});
it("rejecting a suggestion does not change saved feedback", () => {
  const apply = vi.fn();
  render(<SuggestedFeedback original={original} proposed={proposed} onApply={apply} />);
  fireEvent.click(screen.getByText("Reject"));
  expect(apply).not.toHaveBeenCalled();
  expect(screen.getByText("Review again")).toBeInTheDocument();
});
it("keeps a failed save pending and shows the error", async () => {
  const apply = vi.fn().mockRejectedValue(new Error("Connection lost"));
  render(<SuggestedFeedback original={original} proposed={proposed} onApply={apply} />);
  fireEvent.click(screen.getByText("Accept changes"));
  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Connection lost"));
  expect(screen.getByText("Accept changes")).toBeInTheDocument();
  expect(screen.queryByText("Undo")).not.toBeInTheDocument();
});
