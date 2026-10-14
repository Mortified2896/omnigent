import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ReportVideoDialog from "./ReportVideoDialog";

const api = vi.hoisted(() => ({ fetch: vi.fn(), export: vi.fn() }));
vi.mock("@/lib/identity", () => ({ authenticatedFetch: api.fetch }));
vi.mock("./export", () => ({ exportVideo: api.export }));
const base = "/v1/sessions/chat/generated-audio/answer/video";
const project = {
  schema_version: 1,
  audio_sha256: "a".repeat(64),
  duration: 8.13,
  timing: "estimated",
  units: [],
  images: {},
  saved: null,
  ffmpeg_available: true,
  scenes: [
    {
      start: 0,
      title: "Omnigent release",
      summary: "Recovery improved.",
      image_query: "Omnigent",
      image_id: null,
    },
  ],
};
function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  render(
    <QueryClientProvider client={client}>
      <ReportVideoDialog sessionId="chat" onClose={vi.fn()} />
    </QueryClientProvider>,
  );
}
beforeEach(() => {
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(null);
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
  vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:local-media");
  vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {});
  api.export.mockResolvedValue(new Blob(["video-only"], { type: "video/mp4" }));
  api.fetch.mockImplementation(async (path: string, init?: RequestInit) => {
    if (path.endsWith("/generated-audio"))
      return Response.json({
        data: [{ response_id: "answer", status: "ready", duration_seconds: 8.13, updated_at: 1 }],
      });
    if (path.endsWith("/content?format=mp3"))
      return { ok: true, blob: async () => new Blob(["saved original narration"]) };
    if (path === base && init?.method === "POST")
      return Response.json({ video_sha256: "b".repeat(64) });
    if (path === base) return Response.json(project);
    throw Error(`Unexpected request: ${path}`);
  });
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.resetAllMocks();
});
describe("report video", () => {
  it("previews saved audio and labels estimated timing without generating anything", async () => {
    mount();
    expect(await screen.findByLabelText("Title")).toHaveValue("Omnigent release");
    expect(screen.getByText(/Estimated scene timing/)).toBeInTheDocument();
    await screen.findByLabelText("Video narration preview");
    expect(api.export).not.toHaveBeenCalled();
    expect(api.fetch.mock.calls.every(([, init]) => !init?.method)).toBe(true);
  });
  it("exports edited cards and binds the complete original recording before saving", async () => {
    mount();
    fireEvent.change(await screen.findByLabelText("Title"), {
      target: { value: "Release highlights" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create video" }));
    await screen.findByText("Video saved. Watch or download it below.");
    const upload = api.fetch.mock.calls.find(
      ([path, init]) => path === base && init?.method === "POST",
    );
    expect(upload).toBeDefined();
    const content = await (upload![1].body as Blob).text();
    const [header, visuals] = content.split("\n");
    const manifest = JSON.parse(header);
    expect(manifest.audio_sha256).toBe(project.audio_sha256);
    expect(manifest.duration).toBe(8.13);
    expect(manifest.scenes[0].title).toBe("Release highlights");
    expect(visuals).toBe("video-only");
    expect(api.export.mock.calls[0][1]).toBe(8.13);
  });
  it("cancels browser encoding before any video upload", async () => {
    api.export.mockImplementation(
      (_draw, _duration, _progress, signal: AbortSignal) =>
        new Promise((_resolve, reject) => {
          signal.addEventListener("abort", () => reject(Error("cancelled")), { once: true });
        }),
    );
    mount();
    await screen.findByLabelText("Title");
    fireEvent.click(screen.getByRole("button", { name: "Create video" }));
    await waitFor(() => expect(api.export).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: "Cancel creation" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("cancelled");
    expect(api.fetch.mock.calls.some(([, init]) => init?.method === "POST")).toBe(false);
  });
});
