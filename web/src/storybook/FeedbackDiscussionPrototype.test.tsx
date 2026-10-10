import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { FeedbackDiscussionPrototype, type DiscussionVariant } from "./FeedbackDiscussionPrototype";

afterEach(cleanup);
function mount(
  variant: DiscussionVariant = "footer",
  startingPoint: "before-feedback" | "feedback-saved" | "suggestion-ready" = "before-feedback",
  cacheTelemetry: "reported" | "unreported" = "unreported",
) {
  return render(
    <TooltipProvider>
      <FeedbackDiscussionPrototype
        variant={variant}
        startingPoint={startingPoint}
        cacheTelemetry={cacheTelemetry}
        responseDelayMs={0}
      />
    </TooltipProvider>,
  );
}
function original() {
  return within(screen.getByLabelText("Feedback on the original answer"));
}
async function saveFeedback() {
  fireEvent.click(original().getByRole("button", { name: "Partial" }));
  const comment = await original().findByRole("textbox", { name: "Task review comment" });
  await waitFor(() =>
    expect(original().getByRole("button", { name: "Tests/verification" })).not.toBeDisabled(),
  );
  fireEvent.change(comment, { target: { value: "Needs live verification." } });
  fireEvent.click(original().getByRole("button", { name: "Tests/verification" }));
  await waitFor(() =>
    expect(original().getByLabelText("Feedback save status")).toHaveTextContent("Saved"),
  );
}
async function sendPreparedPrompt() {
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
  await waitFor(() => expect(screen.getByLabelText("Suggested feedback")).toBeInTheDocument());
}
it("uses production auto-save and accepts and undoes suggested edits without changing the outcome", async () => {
  mount();
  expect(original().queryByRole("button", { name: "Feedback discussion" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Save feedback" })).not.toBeInTheDocument();
  await saveFeedback();
  fireEvent.click(original().getByRole("button", { name: "Feedback discussion" }));
  expect(screen.getByRole("textbox", { name: "Message the agent" })).toHaveValue(
    "Suggest clearer wording and tags for my feedback on this answer. Keep my outcome unchanged.",
  );
  expect(screen.queryByTestId("discussion-turn")).not.toBeInTheDocument();
  await sendPreparedPrompt();
  expect(original().getByRole("textbox", { name: "Task review comment" })).toHaveValue(
    "Needs live verification.",
  );
  fireEvent.click(screen.getByRole("button", { name: "Accept changes" }));
  await waitFor(() =>
    expect(original().getByRole("textbox", { name: "Task review comment" })).toHaveValue(
      "Local checks pass; live deployment and authenticated behavior still need verification.",
    ),
  );
  await screen.findByRole("button", { name: "Undo" });
  expect(original().getByRole("button", { name: "Partial" })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  fireEvent.click(screen.getByRole("button", { name: "Undo" }));
  await waitFor(() =>
    expect(original().getByRole("textbox", { name: "Task review comment" })).toHaveValue(
      "Needs live verification.",
    ),
  );
  await waitFor(() =>
    expect(original().getByLabelText("Feedback save status")).toHaveTextContent("Saved"),
  );
});
it("expands quick prompts and preserves an existing draft in the actual composer", async () => {
  mount("quick-prompts", "feedback-saved");
  const input = screen.getByRole("textbox", { name: "Message the agent" });
  fireEvent.change(input, { target: { value: "Keep this existing draft." } });
  fireEvent.click(original().getByRole("button", { name: "Feedback discussion" }));
  expect(screen.getByRole("button", { name: "Discuss my feedback" })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Suggest changes" }));
  expect(input).toHaveValue(
    "Keep this existing draft.\n\nSuggest clearer wording and tags for my feedback on this answer. Keep my outcome unchanged.",
  );
  await sendPreparedPrompt();
  expect(screen.getAllByTestId("discussion-turn")).toHaveLength(1);
});
it("sends an inline change in the same conversation and leaves a rejected edit unapplied", async () => {
  mount("inline", "feedback-saved");
  fireEvent.click(original().getByRole("button", { name: "Feedback discussion" }));
  fireEvent.change(screen.getByRole("textbox", { name: "What would you like to change?" }), {
    target: { value: "Mention the local checks." },
  });
  fireEvent.click(screen.getByRole("button", { name: "Discuss change" }));
  await waitFor(() => expect(screen.getByLabelText("Suggested feedback")).toBeInTheDocument());
  expect(
    within(screen.getByRole("region", { name: "Ongoing conversation" })).getByText(
      "I would like to change my feedback: Mention the local checks.",
    ),
  ).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Reject" }));
  expect(original().getByRole("textbox", { name: "Task review comment" })).toHaveValue(
    "Needs live verification.",
  );
});
it("waits for changed feedback to auto-save before sharing it", async () => {
  mount("footer", "feedback-saved");
  const comment = original().getByRole("textbox", { name: "Task review comment" });
  fireEvent.change(comment, { target: { value: "My revised feedback." } });
  expect(original().getByRole("button", { name: "Feedback discussion" })).toBeDisabled();
  await waitFor(() =>
    expect(original().getByRole("button", { name: "Feedback discussion" })).not.toBeDisabled(),
  );
  expect(original().getByLabelText("Feedback save status")).toHaveTextContent("Saved");
});
it("keeps follow-ups in one conversation, never sends network requests, and restores the fetch transport", async () => {
  const fetch = vi.spyOn(globalThis, "fetch");
  const previous = globalThis.fetch;
  try {
    const mounted = mount("footer", "feedback-saved");
    fireEvent.click(original().getByRole("button", { name: "Feedback discussion" }));
    await sendPreparedPrompt();
    fireEvent.change(screen.getByRole("textbox", { name: "Message the agent" }), {
      target: { value: "Explain that suggested comment." },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(screen.getAllByLabelText("Suggested feedback")).toHaveLength(2));
    expect(screen.getAllByRole("region", { name: "Ongoing conversation" })).toHaveLength(1);
    const blocked = await globalThis.fetch("/v1/sessions/a-real-session/task-outcomes/answer", {
      method: "PUT",
      body: "{}",
    });
    expect(blocked.status).toBe(404);
    expect(fetch).not.toHaveBeenCalled();
    mounted.unmount();
    expect(globalThis.fetch).toBe(previous);
  } finally {
    fetch.mockRestore();
  }
});
it("resets the demo while keeping subsequent auto-saves inside the fixture", async () => {
  const fetch = vi.spyOn(globalThis, "fetch");
  try {
    const mounted = mount("footer", "feedback-saved");
    fireEvent.change(original().getByRole("textbox", { name: "Task review comment" }), {
      target: { value: "First attempt." },
    });
    await waitFor(() =>
      expect(original().getByLabelText("Feedback save status")).toHaveTextContent("Saved"),
    );
    fireEvent.click(screen.getByRole("button", { name: "Start again" }));
    await waitFor(() =>
      expect(original().getByRole("textbox", { name: "Task review comment" })).toHaveValue(
        "Needs live verification.",
      ),
    );
    fireEvent.change(original().getByRole("textbox", { name: "Task review comment" }), {
      target: { value: "After restarting." },
    });
    await waitFor(() =>
      expect(original().getByLabelText("Feedback save status")).toHaveTextContent("Saved"),
    );
    expect(fetch).not.toHaveBeenCalled();
    mounted.unmount();
    expect(globalThis.fetch).toBe(fetch);
  } finally {
    fetch.mockRestore();
  }
});
it("shows unavailable cache usage without guessing zero and can preview reported counters", () => {
  mount();
  expect(screen.getByText("Cache reuse: Unavailable")).toBeInTheDocument();
  expect(screen.queryByText(/Cache reuse: 0%/)).not.toBeInTheDocument();
  cleanup();
  mount("footer", "suggestion-ready", "reported");
  expect(screen.getByText("Cache reuse: 90% · 72,000 / 80,000 input tokens")).toBeInTheDocument();
  expect(screen.getByText("Cache reuse: 88% · 88,000 / 100,000 input tokens")).toBeInTheDocument();
});
