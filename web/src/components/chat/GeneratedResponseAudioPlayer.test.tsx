import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { GeneratedResponseAudioPlayer } from "./GeneratedResponseAudioPlayer";

const authenticatedFetch = vi.hoisted(() => vi.fn());
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
  authenticatedFetch.mockReset();
  vi.restoreAllMocks();
});

describe("GeneratedResponseAudioPlayer", () => {
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
    expect(player).toHaveAttribute("src", "blob:generated-audio");
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
