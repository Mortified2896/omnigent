import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ScheduledReportSettings } from "./ScheduledReportSettings";
import type { ScheduledTask } from "@/lib/scheduledTasksApi";

const api = vi.hoisted(() => ({ get: vi.fn(), update: vi.fn() }));
vi.mock("@/lib/scheduledTasksApi", () => ({
  getScheduledTask: api.get,
  updateScheduledTask: api.update,
}));

const task: ScheduledTask = {
  id: "a".repeat(32),
  name: "Daily Report",
  prompt: "Keep the original news sections.",
  rrule: "FREQ=DAILY;BYHOUR=7",
  ownerUserId: null,
  agentId: "agent-1",
  timezone: "Asia/Hong_Kong",
  createdAt: 1,
  updatedAt: 1,
  modelOverride: "model-1",
  reasoningEffort: "low",
  permissionMode: null,
  audioEnabled: true,
  audioVoiceProfile: "elevenlabs:daniel",
  workspace: "/reports",
  hostId: "host-1",
  executionTarget: "connected_host",
  state: "active",
  lastRunAt: 1,
  lastRunStatus: "succeeded",
  lastRunConversationId: "report-1",
  nextRunAt: "2026-10-15T07:00:00+08:00",
};

function mount(onChooseActions?: () => boolean) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  render(
    <MemoryRouter>
      <QueryClientProvider client={client}>
        <ScheduledReportSettings taskId={task.id} onChooseActions={onChooseActions} />
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

async function openSettings() {
  fireEvent.click(screen.getByRole("button", { name: "Report settings" }));
  await screen.findByRole("textbox", { name: "What should future reports do differently?" });
  await waitFor(() => expect(screen.queryByText("Loading saved prompt…")).not.toBeInTheDocument());
}

beforeEach(() => {
  api.get.mockResolvedValue(task);
  api.update.mockImplementation(async (_id: string, input: { prompt: string }) => ({
    ...task,
    ...input,
  }));
});
afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

describe("ScheduledReportSettings", () => {
  it("prepares action choices for an existing report without editing or opening its automation", () => {
    const prepare = vi.fn(() => true);
    mount(prepare);
    fireEvent.click(screen.getByRole("button", { name: "Choose actions" }));
    expect(prepare).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("status")).toHaveTextContent("Press Send to get action choices");
    expect(api.get).not.toHaveBeenCalled();
    expect(api.update).not.toHaveBeenCalled();
  });
  it("loads only when opened and adds an instruction to the latest prompt without changing settings", async () => {
    mount();
    expect(api.get).not.toHaveBeenCalled();
    await openSettings();
    fireEvent.change(screen.getByRole("textbox"), {
      target: { value: "Please add Codex reset timing." },
    });
    api.get.mockResolvedValue({ ...task, prompt: "Another edit made from chat." });
    fireEvent.click(screen.getByRole("button", { name: "Save change" }));
    expect(await screen.findByText("Saved for future runs.")).toBeInTheDocument();
    expect(api.update).toHaveBeenCalledWith(task.id, {
      prompt:
        "Another edit made from chat.\n\nAdditional instructions for future reports:\nPlease add Codex reset timing.",
    });
    expect(screen.getByRole("textbox")).toHaveValue("");
  });

  it("edits the full saved prompt and acknowledges only the server result", async () => {
    mount();
    await openSettings();
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Edit full prompt" }), {
      button: 0,
      ctrlKey: false,
    });
    expect(screen.getByRole("textbox", { name: "Saved report prompt" })).toHaveValue(task.prompt);
    fireEvent.change(screen.getByRole("textbox"), {
      target: { value: "A revised complete prompt." },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save prompt" }));
    await screen.findByText("Saved for future runs.");
    expect(api.update).toHaveBeenCalledWith(task.id, { prompt: "A revised complete prompt." });
  });

  it("keeps a failed save in the editor for retry", async () => {
    api.update.mockRejectedValueOnce(new Error("Connection interrupted"));
    mount();
    await openSettings();
    fireEvent.change(screen.getByRole("textbox"), {
      target: { value: "Shorter reports, please." },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save change" }));
    await screen.findByText("Connection interrupted");
    expect(screen.getByRole("textbox")).toHaveValue("Shorter reports, please.");
    expect(screen.queryByText("Saved for future runs.")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Save change" }));
    await screen.findByText("Saved for future runs.");
  });

  it("protects a full edit when chat has changed the prompt since the dialog opened", async () => {
    mount();
    await openSettings();
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Edit full prompt" }), {
      button: 0,
      ctrlKey: false,
    });
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "My unsaved draft." } });
    api.get.mockResolvedValue({ ...task, prompt: "Saved from chat." });
    fireEvent.click(screen.getByRole("button", { name: "Save prompt" }));
    await screen.findByRole("alert");
    expect(api.update).not.toHaveBeenCalled();
    expect(screen.getByRole("textbox")).toHaveValue("My unsaved draft.");
  });

  it("reloads the current prompt on reopening rather than seeding from cached data", async () => {
    mount();
    await openSettings();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    api.get.mockResolvedValue({ ...task, prompt: "Updated elsewhere." });
    await openSettings();
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Edit full prompt" }), {
      button: 0,
      ctrlKey: false,
    });
    await waitFor(() => expect(screen.getByRole("textbox")).toHaveValue("Updated elsewhere."));
  });

  it("offers retry when the task is unavailable without presenting save controls", async () => {
    api.get.mockRejectedValue(new Error("Not found"));
    mount();
    fireEvent.click(screen.getByRole("button", { name: "Report settings" }));
    await screen.findByRole("alert");
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save change" })).toBeDisabled();
    api.get.mockResolvedValue(task);
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await screen.findByRole("textbox");
  });
});
