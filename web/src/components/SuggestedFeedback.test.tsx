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
  expect(screen.getByLabelText("Suggested feedback")).toBeInTheDocument();
  expect(screen.queryByLabelText("Suggested feedback comment")).not.toBeInTheDocument();
  fireEvent.mouseDown(screen.getByRole("tab", { name: "Final text" }), {
    button: 0,
    ctrlKey: false,
  });
  expect(screen.getByLabelText("Suggested feedback comment preview")).toHaveTextContent(
    proposed.comment,
  );
  expect(apply).not.toHaveBeenCalled();
  fireEvent.click(screen.getByText("Accept changes"));
  await waitFor(() => expect(screen.getByText("Undo")).toBeInTheDocument());
  expect(apply).toHaveBeenCalledWith({ ...proposed, outcome: original.outcome });
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
    expect(apply).toHaveBeenLastCalledWith({
      outcome: original.outcome,
      comment: original.comment,
      tags: original.tags,
    }),
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
it("persists edited rating, tags, and comment; acceptance and undo apply all three", async () => {
  const apply = vi.fn().mockResolvedValue(undefined);
  const props = {
    id: "editable",
    original,
    proposed: { ...proposed, outcome: "success" as const },
    onApply: apply,
  };
  render(<SuggestedFeedback {...props} />);
  fireEvent.click(screen.getByRole("button", { name: "Edit feedback suggestion" }));
  fireEvent.click(screen.getByLabelText("Suggested feedback rating"));
  fireEvent.click(screen.getByRole("option", { name: "Failed" }));
  fireEvent.change(screen.getByLabelText("Suggested feedback comment"), {
    target: { value: "Still blocked" },
  });
  fireEvent.click(screen.getByLabelText("Remove suggested tag Environment"));
  fireEvent.change(screen.getByLabelText("Suggested feedback tag"), { target: { value: "Tools" } });
  fireEvent.click(screen.getByRole("button", { name: "Add proposed tag" }));
  expect(apply).not.toHaveBeenCalled();
  cleanup();
  render(<SuggestedFeedback {...props} />);
  fireEvent.click(screen.getByRole("button", { name: "Edit feedback suggestion" }));
  expect(screen.getByLabelText("Suggested feedback rating")).toHaveTextContent("Failed");
  expect(screen.getByLabelText("Suggested feedback comment")).toHaveValue("Still blocked");
  fireEvent.click(screen.getByText("Accept changes"));
  await waitFor(() => expect(screen.getByText("Undo")).toBeInTheDocument());
  expect(apply).toHaveBeenLastCalledWith({
    outcome: "failed",
    tags: ["Tools"],
    comment: "Still blocked",
  });
  fireEvent.click(screen.getByText("Undo"));
  await waitFor(() => expect(apply).toHaveBeenLastCalledWith(original));
});
it("reveals original comment without changing the edited proposal", () => {
  const apply = vi.fn();
  render(<SuggestedFeedback original={original} proposed={proposed} onApply={apply} />);
  fireEvent.click(screen.getByRole("button", { name: "Edit feedback suggestion" }));
  fireEvent.change(screen.getByLabelText("Suggested feedback comment"), {
    target: { value: "My adjusted proposal" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Show original comment" }));
  expect(screen.getByRole("button", { name: "Hide original comment" })).toHaveAttribute(
    "aria-expanded",
    "true",
  );
  expect(screen.getByLabelText("Original feedback comment")).toHaveTextContent(original.comment);
  expect(screen.getByLabelText("Suggested feedback comment")).toHaveValue("My adjusted proposal");
  expect(apply).not.toHaveBeenCalled();
});

it("previews adjusted feedback after Done and waits for explicit acceptance", async () => {
  const apply = vi.fn().mockResolvedValue(undefined);
  render(<SuggestedFeedback original={original} proposed={proposed} onApply={apply} />);
  fireEvent.click(screen.getByRole("button", { name: "Edit feedback suggestion" }));
  fireEvent.change(screen.getByLabelText("Suggested feedback comment"), {
    target: { value: "Adjusted explanation" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Done editing suggestion" }));
  expect(screen.queryByLabelText("Suggested feedback comment")).not.toBeInTheDocument();
  fireEvent.mouseDown(screen.getByRole("tab", { name: "Final text" }), {
    button: 0,
    ctrlKey: false,
  });
  expect(screen.getByLabelText("Suggested feedback comment preview")).toHaveTextContent(
    "Adjusted explanation",
  );
  expect(apply).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Accept changes" }));
  await waitFor(() =>
    expect(apply).toHaveBeenCalledWith({
      outcome: original.outcome,
      comment: "Adjusted explanation",
      tags: proposed.tags,
    }),
  );
});

it("shows tracked rating, tag, and word changes and switches to the exact final text", async () => {
  const apply = vi.fn().mockResolvedValue(undefined);
  const before = { outcome: "partial", comment: "Local checks passed.", tags: ["Environment"] };
  const after = {
    outcome: "failed" as const,
    comment: "Live checks passed.",
    tags: ["Tests/verification"],
  };
  render(<SuggestedFeedback original={before} proposed={after} onApply={apply} />);
  const rating = screen.getByLabelText("Proposed feedback rating");
  expect(rating.querySelector("del")).toHaveTextContent("Partial");
  expect(rating.querySelector("ins")).toHaveTextContent("Failed");
  const tags = screen.getByLabelText("Proposed feedback tags");
  expect(tags.querySelector("del")).toHaveTextContent("Environment");
  expect(tags.querySelector("ins")).toHaveTextContent("Tests/verification");
  const comment = screen.getByLabelText("Suggested feedback comment preview");
  expect(comment.querySelector("del")).toHaveTextContent("Local");
  expect(comment.querySelector("ins")).toHaveTextContent("Live");
  expect(comment).toHaveTextContent("checks passed.");
  fireEvent.mouseDown(screen.getByRole("tab", { name: "Final text" }), {
    button: 0,
    ctrlKey: false,
  });
  const final = screen.getByLabelText("Suggested feedback comment preview");
  expect(final).toHaveTextContent(after.comment);
  expect(final.querySelector("del, ins")).toBeNull();
  expect(screen.getByLabelText("Proposed feedback tags")).not.toHaveTextContent("Environment");
  expect(apply).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Accept changes" }));
  await waitFor(() => expect(apply).toHaveBeenCalledWith(after));
});

it("renders proposed markup as literal text in both review views", () => {
  const view = render(
    <SuggestedFeedback
      original={original}
      proposed={{ ...proposed, comment: '<script>unsafe()</script><img src="x">' }}
      onApply={vi.fn()}
    />,
  );
  expect(view.container.querySelector("script, img")).toBeNull();
  fireEvent.mouseDown(screen.getByRole("tab", { name: "Final text" }), {
    button: 0,
    ctrlKey: false,
  });
  expect(screen.getByLabelText("Suggested feedback comment preview")).toHaveTextContent(
    '<script>unsafe()</script><img src="x">',
  );
  expect(view.container.querySelector("script, img")).toBeNull();
});
