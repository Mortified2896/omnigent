import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { GeneratedResponseAudioPlayer } from "./GeneratedResponseAudioPlayer";

const authenticatedFetch = vi.hoisted(() => vi.fn());
const hostConfig = vi.hoisted(() => ({ fetcher: undefined as undefined | (() => void) }));
vi.mock("@/lib/host", () => ({ getOmnigentHostConfig: () => hostConfig }));
vi.mock("@/lib/identity", () => ({ authenticatedFetch }));

function response(body: unknown, blob?: Blob) {
  return {
    ok: true,
    json: async () => body,
    blob: async () => blob ?? new Blob(["wav"]),
  };
}

function renderPlayer() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={client}>
      <GeneratedResponseAudioPlayer sessionId="session-1" responseId="response-1" pollForNewAudio />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  hostConfig.fetcher = undefined;
  vi.useRealTimers();
  authenticatedFetch.mockReset();
  vi.restoreAllMocks();
});

describe("GeneratedResponseAudioPlayer", () => {
  it("discovers audio added more than two minutes after the response mounted", async () => {
    vi.useFakeTimers();
    authenticatedFetch.mockResolvedValue(response({ data: [] }));
    const view = renderPlayer();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(180_000);
    });
    authenticatedFetch.mockResolvedValue(
      response({
        data: [
          {
            response_id: "response-1",
            status: "processing",
            duration_seconds: null,
            sample_rate: null,
            error_code: null,
            updated_at: 1,
          },
        ],
      }),
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(31_000);
    });
    expect(screen.getByText("Preparing audio…")).toBeInTheDocument();
    view.unmount();
  });

  it("leaves ordinary responses without an audio row unchanged", async () => {
    authenticatedFetch.mockResolvedValue(response({ data: [] }));
    renderPlayer();
    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Listen to this response")).not.toBeInTheDocument();
  });

  it("shows accessible native playback controls for the matching response", async () => {
    authenticatedFetch
      .mockResolvedValueOnce(
        response({
          data: [
            {
              response_id: "response-1",
              status: "ready",
              duration_seconds: 4.2,
              sample_rate: 24_000,
              error_code: null,
              updated_at: 1,
            },
          ],
        }),
      )
      .mockResolvedValueOnce(response({}, new Blob(["wav"], { type: "audio/wav" })));
    vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:generated-audio");
    vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => undefined);
    renderPlayer();
    const player = await screen.findByLabelText("Listen to this response");
    expect(player.tagName).toBe("AUDIO");
    expect(player).toHaveAttribute("controls");
    expect(player).toHaveAttribute(
      "src",
      "/v1/sessions/session-1/generated-audio/response-1/content",
    );
    expect(authenticatedFetch).toHaveBeenCalledTimes(1);
    expect(screen.queryByText("Loading audio…")).not.toBeInTheDocument();
  });

  it("preserves the authenticated blob transport for embedded hosts", async () => {
    hostConfig.fetcher = () => undefined;
    authenticatedFetch
      .mockResolvedValueOnce(response({ data: [{ response_id: "response-1", status: "ready" }] }))
      .mockResolvedValueOnce(response({}, new Blob(["wav"], { type: "audio/wav" })));
    vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:embedded-audio");
    vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => undefined);
    renderPlayer();
    expect(await screen.findByLabelText("Listen to this response")).toHaveAttribute(
      "src",
      "blob:embedded-audio",
    );
    expect(authenticatedFetch).toHaveBeenCalledTimes(2);
  });

  it("shows only a small failure message when generation failed", async () => {
    authenticatedFetch.mockResolvedValue(
      response({
        data: [
          {
            response_id: "response-1",
            status: "failed",
            duration_seconds: null,
            sample_rate: null,
            error_code: "tts_unavailable",
            updated_at: 1,
          },
        ],
      }),
    );
    renderPlayer();
    expect(await screen.findByText("Audio unavailable")).toBeInTheDocument();
    expect(screen.queryByLabelText("Listen to this response")).not.toBeInTheDocument();
  });
});
