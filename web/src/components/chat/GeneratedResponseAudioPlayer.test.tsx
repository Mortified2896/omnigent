import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { GeneratedResponseAudioPlayer } from "./GeneratedResponseAudioPlayer";
import { clearReadAlongHighlight, setReadAlongHighlight } from "./readAlongHighlight";

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

function renderPlayer(responseId = "response-1", text = "Hello world.") {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return {
    ...render(
      <QueryClientProvider client={client}>
        <div data-response-id={responseId}>
          <div data-testid="assistant-text-section">{text}</div>
          <GeneratedResponseAudioPlayer
            sessionId="session-1"
            responseId={responseId}
            pollForNewAudio
          />
        </div>
      </QueryClientProvider>,
    ),
    client,
  };
}

const highlightRanges = vi.hoisted(() => new Map<string, Set<Range>>());

class TestHighlight {
  ranges = new Set<Range>();
  add(range: Range) {
    this.ranges.add(range);
  }
  clear() {
    this.ranges.clear();
  }
}

function installHighlightApi() {
  highlightRanges.clear();
  class RegistryHighlight extends TestHighlight {}
  vi.stubGlobal("Highlight", RegistryHighlight);
  vi.stubGlobal("CSS", {
    highlights: {
      set: (name: string, value: RegistryHighlight) => highlightRanges.set(name, value.ranges),
      delete: (name: string) => highlightRanges.delete(name),
    },
  });
}

function currentHighlightText(): string[] {
  return Array.from(highlightRanges.get("omnigent-audio-current-word") ?? [], (range) =>
    range.toString(),
  );
}

function readyAudioRow(responseId: string) {
  return {
    response_id: responseId,
    status: "ready",
    duration_seconds: 2,
    sample_rate: 24_000,
    error_code: null,
    updated_at: 1,
  };
}

function timings(text: string[]) {
  return {
    schema_version: 1,
    engine: "kokoro",
    audio_sha256: "a".repeat(64),
    narration_sha256: "b".repeat(64),
    duration_seconds: 2,
    position_unit: "unicode-code-point",
    units: text.map((word, index) => ({
      text: word,
      narration_start: index,
      narration_end: index + word.length,
      start_seconds: index,
      end_seconds: index + 1,
    })),
  };
}

afterEach(() => {
  hostConfig.fetcher = undefined;
  vi.useRealTimers();
  authenticatedFetch.mockReset();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  highlightRanges.clear();
});

describe("GeneratedResponseAudioPlayer", () => {
  it("uses the CSS Highlight registry when available", () => {
    installHighlightApi();
    expect((globalThis as typeof globalThis & { CSS?: unknown }).CSS).toBeDefined();
    expect((globalThis as typeof globalThis & { Highlight?: unknown }).Highlight).toBeDefined();
    expect((globalThis.CSS as typeof CSS & { highlights?: unknown }).highlights).toBeDefined();
    const text = document.createTextNode("Hello");
    const range = document.createRange();
    range.setStart(text, 0);
    range.setEnd(text, 5);
    expect(setReadAlongHighlight("probe", range)).toBe(true);
    expect(currentHighlightText()).toEqual(["Hello"]);
    clearReadAlongHighlight("probe");
  });

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
    authenticatedFetch.mockImplementation((url: string) =>
      Promise.resolve(
        String(url).endsWith("/timings")
          ? { ok: false, json: async () => ({}) }
          : response({ data: [readyAudioRow("response-1")] }),
      ),
    );
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
    expect(authenticatedFetch).toHaveBeenCalledTimes(2);
    expect(screen.queryByText("Loading audio…")).not.toBeInTheDocument();
  });

  it("preserves the authenticated blob transport for embedded hosts", async () => {
    hostConfig.fetcher = () => undefined;
    authenticatedFetch.mockImplementation((url: string) =>
      Promise.resolve(
        String(url).endsWith("/timings")
          ? { ok: false, json: async () => ({}) }
          : String(url).endsWith("/content")
            ? response({}, new Blob(["wav"], { type: "audio/wav" }))
            : response({ data: [readyAudioRow("response-1")] }),
      ),
    );
    vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:embedded-audio");
    vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => undefined);
    renderPlayer();
    expect(await screen.findByLabelText("Listen to this response")).toHaveAttribute(
      "src",
      "blob:embedded-audio",
    );
    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(3));
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

  it("advances on playback and seeking, retains the paused word, then clears at end", async () => {
    installHighlightApi();
    authenticatedFetch.mockImplementation((url: string) =>
      Promise.resolve(
        String(url).endsWith("/timings")
          ? response(timings(["Hello", "world"]))
          : response({ data: [readyAudioRow("response-1")] }),
      ),
    );
    const view = renderPlayer("response-1", "Hello world.");
    const player = (await screen.findByLabelText("Listen to this response")) as HTMLAudioElement;
    await waitFor(() =>
      expect(
        view.client.getQueryData(["generated-response-audio-timings", "session-1", "response-1"]),
      ).toBeDefined(),
    );
    Object.defineProperty(player, "paused", { configurable: true, value: false });
    Object.defineProperty(player, "currentTime", {
      configurable: true,
      writable: true,
      value: 0.2,
    });
    await waitFor(() => {
      fireEvent.play(player);
      expect(currentHighlightText()).toEqual(["Hello"]);
    });

    player.currentTime = 1.2;
    fireEvent.timeUpdate(player);
    expect(currentHighlightText()).toEqual(["world"]);
    fireEvent.pause(player);
    expect(currentHighlightText()).toEqual(["world"]);

    player.currentTime = 0.4;
    fireEvent.seeking(player);
    expect(currentHighlightText()).toEqual(["Hello"]);
    player.currentTime = 1.4;
    fireEvent.seeked(player);
    expect(currentHighlightText()).toEqual(["world"]);
    fireEvent.ended(player);
    expect(currentHighlightText()).toEqual([]);
  });

  it("keeps audio usable when the optional timing sidecar is missing", async () => {
    installHighlightApi();
    authenticatedFetch.mockImplementation((url: string) =>
      Promise.resolve(
        String(url).endsWith("/timings")
          ? { ok: false, json: async () => ({}) }
          : response({ data: [readyAudioRow("response-1")] }),
      ),
    );
    renderPlayer();
    const player = await screen.findByLabelText("Listen to this response");
    expect(player).toHaveAttribute("controls");
    expect(screen.queryByText("Audio unavailable")).not.toBeInTheDocument();
    expect(currentHighlightText()).toEqual([]);
  });

  it("keeps two response players isolated and removes only the unmounted owner", async () => {
    installHighlightApi();
    authenticatedFetch.mockImplementation((url: string) => {
      const value = String(url);
      if (value.endsWith("/timings")) {
        return Promise.resolve(
          response(
            value.includes("response-2")
              ? timings(["Second", "answer"])
              : timings(["First", "answer"]),
          ),
        );
      }
      return Promise.resolve(
        response({ data: [readyAudioRow("response-1"), readyAudioRow("response-2")] }),
      );
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
    const view = render(
      <QueryClientProvider client={client}>
        <div data-response-id="response-1">
          <div data-testid="assistant-text-section">First answer.</div>
          <GeneratedResponseAudioPlayer
            sessionId="session-1"
            responseId="response-1"
            pollForNewAudio={false}
          />
        </div>
        <div data-response-id="response-2">
          <div data-testid="assistant-text-section">Second answer.</div>
          <GeneratedResponseAudioPlayer
            sessionId="session-1"
            responseId="response-2"
            pollForNewAudio={false}
          />
        </div>
      </QueryClientProvider>,
    );
    const players = (await screen.findAllByLabelText(
      "Listen to this response",
    )) as HTMLAudioElement[];
    await waitFor(() => {
      expect(
        client.getQueryData(["generated-response-audio-timings", "session-1", "response-1"]),
      ).toBeDefined();
      expect(
        client.getQueryData(["generated-response-audio-timings", "session-1", "response-2"]),
      ).toBeDefined();
    });
    Object.defineProperty(players[0], "paused", { configurable: true, value: false });
    Object.defineProperty(players[0], "currentTime", {
      configurable: true,
      writable: true,
      value: 0.2,
    });
    await waitFor(() => {
      fireEvent.play(players[0]!);
      expect(currentHighlightText()).toEqual(["First"]);
    });

    Object.defineProperty(players[1], "paused", { configurable: true, value: false });
    Object.defineProperty(players[1], "currentTime", {
      configurable: true,
      writable: true,
      value: 0.2,
    });
    await waitFor(() => {
      fireEvent.play(players[1]!);
      expect(currentHighlightText()).toEqual(["Second"]);
    });
    fireEvent.ended(players[0]!);
    expect(currentHighlightText()).toEqual(["Second"]);

    view.unmount();
    expect(currentHighlightText()).toEqual([]);
  });
});
