import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import {
  FeedbackDiscussionPrototype,
  type DiscussionVariant,
  type StartingPoint,
} from "./FeedbackDiscussionPrototype";

afterEach(cleanup);
function mount(
  variant: DiscussionVariant = "footer",
  startingPoint: StartingPoint = "before-feedback",
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
  await waitFor(() =>
    expect(screen.getAllByTestId("discussion-turn").at(-1)).toHaveAttribute(
      "data-complete",
      "true",
    ),
  );
}
it("discusses saved feedback first, then accepts and undoes explicitly requested edits", async () => {
  mount();
  expect(original().queryByRole("button", { name: "Feedback discussion" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Save feedback" })).not.toBeInTheDocument();
  await saveFeedback();
  fireEvent.click(original().getByRole("button", { name: "Feedback discussion" }));
  expect(screen.getByRole("textbox", { name: "Message the agent" })).toHaveValue(
    "Discuss my feedback on this answer. Do you agree with my assessment?",
  );
  expect(screen.queryByTestId("discussion-turn")).not.toBeInTheDocument();
  await sendPreparedPrompt();
  expect(screen.queryByLabelText("Suggested feedback")).not.toBeInTheDocument();
  expect(screen.getByTestId("discussion-turn")).toHaveAttribute("data-intent", "discuss");
  expect(original().getByRole("textbox", { name: "Task review comment" })).toHaveValue(
    "Needs live verification.",
  );
  fireEvent.click(screen.getByRole("button", { name: "Suggest feedback changes" }));
  await sendPreparedPrompt();
  expect(screen.getByLabelText("Suggested feedback")).toBeInTheDocument();
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
  fireEvent.click(screen.getByRole("button", { name: "Suggest feedback changes" }));
  expect(input).toHaveValue(
    "Keep this existing draft.\n\nSuggest clearer wording and tags for my feedback on this answer. Keep my outcome unchanged.",
  );
  await sendPreparedPrompt();
  expect(screen.getAllByTestId("discussion-turn")).toHaveLength(1);
  expect(screen.getByLabelText("Suggested feedback")).toBeInTheDocument();
});
it("sends inline discussion in the same conversation and leaves a later rejected edit unapplied", async () => {
  mount("inline", "feedback-saved");
  fireEvent.click(original().getByRole("button", { name: "Feedback discussion" }));
  fireEvent.change(screen.getByRole("textbox", { name: "What would you like to discuss?" }), {
    target: { value: "Does my comment explain the missing verification?" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Send to chat" }));
  await waitFor(() =>
    expect(screen.getByTestId("discussion-turn")).toHaveAttribute("data-complete", "true"),
  );
  expect(screen.queryByLabelText("Suggested feedback")).not.toBeInTheDocument();
  expect(
    within(screen.getByRole("region", { name: "Ongoing conversation" })).getByText(
      "Discuss my feedback on this answer: Does my comment explain the missing verification?",
    ),
  ).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Suggest feedback changes" }));
  await sendPreparedPrompt();
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
      target: { value: "Explain why you agree with my assessment." },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(screen.getAllByTestId("discussion-turn")).toHaveLength(2));
    await waitFor(() =>
      expect(screen.getAllByTestId("discussion-turn").at(-1)).toHaveAttribute(
        "data-complete",
        "true",
      ),
    );
    expect(screen.queryByLabelText("Suggested feedback")).not.toBeInTheDocument();
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
it.each<DiscussionVariant>(["footer", "quick-prompts", "inline"])(
  "keeps inspecting actions separate from discussing or editing feedback in %s",
  async (variant) => {
    mount(variant, "feedback-saved");
    fireEvent.click(original().getByRole("button", { name: "Self Reflection" }));
    expect(screen.getByRole("textbox", { name: "Message the agent" })).toHaveValue(
      "Inspect your actions and tool results for this answer. What was verified, and what is still unverified?",
    );
    expect(screen.getByText("Self Reflection · original answer")).toBeInTheDocument();
    await sendPreparedPrompt();
    expect(screen.getByTestId("discussion-turn")).toHaveAttribute("data-intent", "inspect");
    expect(screen.getByText(/The action record supports/)).toBeInTheDocument();
    expect(screen.queryByLabelText("Suggested feedback")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Suggest feedback changes" }),
    ).not.toBeInTheDocument();
    expect(original().getByRole("textbox", { name: "Task review comment" })).toHaveValue(
      "Needs live verification.",
    );
    expect(original().getByRole("button", { name: "Partial" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getAllByRole("region", { name: "Ongoing conversation" })).toHaveLength(1);
  },
);
it("lets the independent inspection start while a feedback comment is still saving", async () => {
  mount("footer", "feedback-saved");
  fireEvent.change(original().getByRole("textbox", { name: "Task review comment" }), {
    target: { value: "A comment that has not saved yet." },
  });
  expect(original().getByRole("button", { name: "Feedback discussion" })).toBeDisabled();
  expect(original().getByRole("button", { name: "Self Reflection" })).not.toBeDisabled();
  fireEvent.click(original().getByRole("button", { name: "Self Reflection" }));
  await sendPreparedPrompt();
  expect(screen.getByTestId("discussion-turn")).toHaveAttribute("data-intent", "inspect");
  expect(screen.queryByLabelText("Suggested feedback")).not.toBeInTheDocument();
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
it.each([
  ["discussion-ready", "discuss", "Feedback discussion"],
  ["inspection-ready", "inspect", "Self Reflection"],
  ["suggestion-ready", "discuss", "Feedback discussion"],
] as const)(
  "preserves the ongoing composer context at %s",
  async (startingPoint, intent, label) => {
    mount("footer", startingPoint);
    expect(screen.getByText(`${label} · original answer`)).toBeInTheDocument();
    const proposalsBefore = screen.queryAllByLabelText("Suggested feedback").length;
    fireEvent.change(screen.getByRole("textbox", { name: "Message the agent" }), {
      target: { value: "Explain that in more detail." },
    });
    await sendPreparedPrompt();
    expect(screen.getAllByTestId("discussion-turn").at(-1)).toHaveAttribute("data-intent", intent);
    expect(screen.queryAllByLabelText("Suggested feedback")).toHaveLength(proposalsBefore);
  },
);
