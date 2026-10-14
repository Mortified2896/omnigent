import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { GeneratedResponseAudioPlayer } from "./GeneratedResponseAudioPlayer";
import { GeneratedAudioDock, GeneratedAudioDockProvider } from "./GeneratedAudioDock";
import { clearReadAlongHighlight, setReadAlongHighlight } from "./readAlongHighlight";
import * as readAlong from "./readAlongHighlight";

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

beforeEach(() => {
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => undefined);
});

afterEach(() => {
  cleanup();
  hostConfig.fetcher = undefined;
  vi.useRealTimers();
  authenticatedFetch.mockReset();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  highlightRanges.clear();
});

describe("GeneratedResponseAudioPlayer", () => {
  it("shows model, report length, progress and a remaining-time range while generating", async () => {
    const generation = {
      model: "eleven_v4",
      narration_characters: 8000,
      completed_chunks: 1,
      total_chunks: 4,
      elapsed_seconds: 80,
      remaining_seconds_low: 90,
      remaining_seconds_high: 240,
      taking_longer: false,
      estimate_basis: "this generation",
    };
    authenticatedFetch.mockResolvedValue(
      response({ data: [{ ...readyAudioRow("response-1"), status: "processing", generation }] }),
    );
    const view = renderPlayer();
    expect(await screen.findByText(/ElevenLabs V4/)).toHaveTextContent("8,000 characters");
    expect(screen.getByText(/min remaining/)).toHaveTextContent(
      "about 2–4 min remaining · 1/4 sections complete",
    );
    authenticatedFetch.mockResolvedValue(
      response({
        data: [
          {
            ...readyAudioRow("response-1"),
            status: "processing",
            generation: {
              ...generation,
              taking_longer: true,
              remaining_seconds_low: null,
              remaining_seconds_high: null,
            },
          },
        ],
      }),
    );
    await act(async () => {
      await view.client.refetchQueries({ queryKey: ["generated-response-audio", "session-1"] });
    });
    expect(await screen.findByText(/Taking longer than estimated/)).toBeInTheDocument();
    expect(screen.queryByText(/min remaining/)).not.toBeInTheDocument();
  });
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

  it("shows accessible playback controls for the matching response", async () => {
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
    expect(player).not.toHaveAttribute("controls");
    expect(screen.getByRole("button", { name: "Play audio" })).toBeInTheDocument();
    expect(screen.getByRole("slider", { name: "Audio position" })).toBeInTheDocument();
    expect(player).toHaveAttribute(
      "src",
      "/v1/sessions/session-1/generated-audio/response-1/content?format=mp3",
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
          : String(url).endsWith("/content?format=mp3")
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

  it("retries an interrupted fallback and reattaches word highlighting", async () => {
    installHighlightApi();
    let failures = 1;
    authenticatedFetch.mockImplementation((url: string) => {
      if (url.endsWith("/timings")) return Promise.resolve(response(timings(["Hello", "world"])));
      if (url.includes("/content?")) {
        if (failures-- > 0) return Promise.reject(new TypeError("Load failed"));
        return Promise.resolve(response({}, new Blob(["mp3"], { type: "audio/mpeg" })));
      }
      return Promise.resolve(response({ data: [readyAudioRow("response-1")] }));
    });
    vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:retry-audio");
    vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => undefined);
    const view = renderPlayer();
    const original = await screen.findByLabelText("Listen to this response");
    fireEvent.error(original);
    fireEvent.click(await screen.findByText("Retry audio"));
    const restored = (await screen.findByLabelText("Listen to this response")) as HTMLAudioElement;
    expect(restored).toHaveAttribute("src", "blob:retry-audio");
    restored.currentTime = 1.2;
    fireEvent.seeked(restored);
    expect(currentHighlightText()).toEqual(["world"]);
    view.unmount();
    expect(currentHighlightText()).toEqual([]);
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:retry-audio");
  });

  it("maps Markdown that mounts after timings and media", async () => {
    installHighlightApi();
    authenticatedFetch.mockImplementation((url: string) =>
      Promise.resolve(
        url.endsWith("/timings")
          ? response(timings(["Hello", "world"]))
          : response({ data: [readyAudioRow("response-1")] }),
      ),
    );
    renderPlayer("response-1", "");
    const audio = (await screen.findByLabelText("Listen to this response")) as HTMLAudioElement;
    audio.currentTime = 1.2;
    fireEvent.seeked(audio);
    expect(currentHighlightText()).toEqual([]);
    screen.getByTestId("assistant-text-section").textContent = "Hello world.";
    await waitFor(() => expect(currentHighlightText()).toEqual(["world"]));
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

  it("estimates word highlighting when the optional timing sidecar is missing", async () => {
    installHighlightApi();
    authenticatedFetch.mockImplementation((url: string) =>
      Promise.resolve(
        String(url).endsWith("/timings")
          ? { ok: false, json: async () => ({}) }
          : response({ data: [readyAudioRow("response-1")] }),
      ),
    );
    renderPlayer();
    const player = (await screen.findByLabelText("Listen to this response")) as HTMLAudioElement;
    expect(player).not.toHaveAttribute("controls");
    expect(screen.queryByText("Audio unavailable")).not.toBeInTheDocument();
    player.currentTime = 1.5;
    fireEvent.seeking(player);
    expect(currentHighlightText()).toEqual(["world"]);
  });

  it("follows playback by default, can opt out, and never scrolls paused seeking", async () => {
    const follow = vi.spyOn(readAlong, "followReadAlongRange").mockImplementation(() => undefined);
    authenticatedFetch.mockImplementation((url: string) =>
      Promise.resolve(
        String(url).endsWith("/timings")
          ? response(timings(["Hello", "world"]))
          : response({ data: [readyAudioRow("response-1")] }),
      ),
    );
    const view = renderPlayer();
    const audio = (await screen.findByLabelText("Listen to this response")) as HTMLAudioElement;
    await waitFor(() =>
      expect(
        view.client.getQueryData(["generated-response-audio-timings", "session-1", "response-1"]),
      ).toBeDefined(),
    );
    const toggle = screen.getByRole("button", { name: "Follow text" });
    expect(toggle).toHaveAttribute("aria-pressed", "true");
    audio.currentTime = 0.2;
    fireEvent.seeking(audio);
    expect(follow).not.toHaveBeenCalled();
    Object.defineProperty(audio, "paused", { configurable: true, value: false });
    fireEvent.play(audio);
    expect(follow).toHaveBeenCalled();
    follow.mockClear();
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-pressed", "false");
    audio.currentTime = 1.2;
    fireEvent.timeUpdate(audio);
    expect(follow).not.toHaveBeenCalled();
    fireEvent.click(toggle);
    expect(follow).toHaveBeenCalled();
    follow.mockClear();
    Object.defineProperty(audio, "paused", { configurable: true, value: true });
    fireEvent.pause(audio);
    audio.currentTime = 0.2;
    fireEvent.seeking(audio);
    expect(follow).not.toHaveBeenCalled();
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

describe("compact audio transport", () => {
  function mockReady() {
    authenticatedFetch.mockImplementation((url: string) =>
      Promise.resolve(
        url.endsWith("/timings")
          ? { ok: false, json: async () => ({}) }
          : response({ data: [readyAudioRow("response-1")] }),
      ),
    );
  }

  it("does not present canceled playback or browser permission as a failed recording", async () => {
    mockReady();
    renderPlayer();
    const audio = (await screen.findByLabelText("Listen to this response")) as HTMLAudioElement;
    const play = vi
      .spyOn(audio, "play")
      .mockRejectedValueOnce(new DOMException("Playback canceled", "AbortError"));
    fireEvent.click(screen.getByRole("button", { name: "Play audio" }));
    await waitFor(() => expect(play).toHaveBeenCalledOnce());
    expect(screen.queryByText(/Playback was interrupted/)).not.toBeInTheDocument();
    play.mockRejectedValueOnce(new DOMException("User gesture required", "NotAllowedError"));
    fireEvent.click(screen.getByRole("button", { name: "Play audio" }));
    expect(await screen.findByText(/Press Play again/)).toBeInTheDocument();
    expect(screen.queryByText(/Playback was interrupted/)).not.toBeInTheDocument();
    fireEvent.play(audio);
    expect(screen.queryByText(/Press Play again/)).not.toBeInTheDocument();
  });

  it("retries playback in place without resynthesizing and restores position, speed and mute", async () => {
    mockReady();
    renderPlayer();
    const audio = (await screen.findByLabelText("Listen to this response")) as HTMLAudioElement;
    vi.spyOn(audio, "play").mockRejectedValue(new Error("Load failed"));
    const load = vi.spyOn(audio, "load").mockImplementation(() => {
      audio.currentTime = 0;
    });
    audio.currentTime = 42;
    fireEvent.change(screen.getByRole("combobox", { name: "Playback speed" }), {
      target: { value: "1.5" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Mute audio" }));
    fireEvent.click(screen.getByRole("button", { name: "Play audio" }));
    const retry = await screen.findByRole("button", { name: "Retry playback" });
    const requests = authenticatedFetch.mock.calls.length;
    fireEvent.click(retry);
    expect(load).toHaveBeenCalledOnce();
    fireEvent.loadedMetadata(audio);
    expect(audio.currentTime).toBe(42);
    expect(audio.playbackRate).toBe(1.5);
    expect(audio.muted).toBe(true);
    expect(authenticatedFetch).toHaveBeenCalledTimes(requests);
    expect(screen.queryByText(/Playback was interrupted/)).not.toBeInTheDocument();
    fireEvent.play(audio);
    expect(screen.getByRole("button", { name: "Pause audio" })).toBeInTheDocument();
  });

  it("seeks, clamps skip controls, changes speed, and synchronizes play/pause and mute", async () => {
    mockReady();
    renderPlayer();
    const audio = (await screen.findByLabelText("Listen to this response")) as HTMLAudioElement;
    Object.defineProperty(audio, "duration", { configurable: true, value: 120 });
    fireEvent.loadedMetadata(audio);
    let paused = true;
    Object.defineProperty(audio, "paused", { configurable: true, get: () => paused });
    vi.spyOn(audio, "play").mockImplementation(async () => {
      paused = false;
      fireEvent.play(audio);
    });
    vi.spyOn(audio, "pause").mockImplementation(() => {
      paused = true;
      fireEvent.pause(audio);
    });
    fireEvent.click(screen.getByRole("button", { name: "Forward 15 seconds" }));
    expect(audio.currentTime).toBe(15);
    fireEvent.click(screen.getByRole("button", { name: "Back 15 seconds" }));
    expect(audio.currentTime).toBe(0);
    fireEvent.change(screen.getByRole("slider", { name: "Audio position" }), {
      target: { value: "90" },
    });
    expect(audio.currentTime).toBe(90);
    expect(screen.getByText("1:30 / 2:00")).toBeInTheDocument();
    fireEvent.change(screen.getByRole("combobox", { name: "Playback speed" }), {
      target: { value: "1.5" },
    });
    expect(audio.playbackRate).toBe(1.5);
    fireEvent.click(screen.getByRole("button", { name: "Mute audio" }));
    expect(audio.muted).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Play audio" }));
    fireEvent.click(await screen.findByRole("button", { name: "Pause audio" }));
    expect(audio.pause).toHaveBeenCalled();
    audio.currentTime = 119;
    fireEvent.timeUpdate(audio);
    fireEvent.click(screen.getByRole("button", { name: "Forward 15 seconds" }));
    expect(audio.currentTime).toBe(120);
  });

  it("docks controls outside the report while retaining the audio in its response for highlighting", async () => {
    mockReady();
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <GeneratedAudioDockProvider>
          <div data-testid="report" data-response-id="response-1">
            <div data-testid="assistant-text-section">Hello world.</div>
            <GeneratedResponseAudioPlayer
              sessionId="session-1"
              responseId="response-1"
              pollForNewAudio
            />
          </div>
          <GeneratedAudioDock />
        </GeneratedAudioDockProvider>
      </QueryClientProvider>,
    );
    const audio = await screen.findByLabelText("Listen to this response");
    const dock = screen.getByTestId("generated-audio-dock");
    expect(within(dock).getByRole("button", { name: "Play audio" })).toBeInTheDocument();
    expect(screen.getByTestId("report")).toContainElement(audio);
    expect(
      within(screen.getByTestId("report")).queryByRole("button", { name: "Play audio" }),
    ).not.toBeInTheDocument();
  });

  it("selects an older recording without remounting media and pauses the previous recording", async () => {
    authenticatedFetch.mockImplementation((url: string) =>
      Promise.resolve(
        url.endsWith("/timings")
          ? { ok: false, json: async () => ({}) }
          : response({ data: [readyAudioRow("older"), readyAudioRow("latest")] }),
      ),
    );
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <GeneratedAudioDockProvider>
          {["older", "latest"].map((id) => (
            <div key={id} data-testid={id} data-response-id={id}>
              <GeneratedResponseAudioPlayer
                sessionId="session-1"
                responseId={id}
                pollForNewAudio={id === "latest"}
              />
            </div>
          ))}
          <GeneratedAudioDock />
        </GeneratedAudioDockProvider>
      </QueryClientProvider>,
    );
    await screen.findAllByLabelText("Listen to this response");
    const older = within(screen.getByTestId("older"));
    const olderAudio = older.getByLabelText("Listen to this response") as HTMLAudioElement;
    const latestAudio = within(screen.getByTestId("latest")).getByLabelText(
      "Listen to this response",
    ) as HTMLAudioElement;
    Object.defineProperty(latestAudio, "paused", { configurable: true, value: false });
    const pauseLatest = vi.spyOn(latestAudio, "pause");
    const playOlder = vi.spyOn(olderAudio, "play").mockImplementation(async () => {
      fireEvent.play(olderAudio);
    });
    fireEvent.click(older.getByRole("button", { name: "Play audio" }));
    expect(playOlder).toHaveBeenCalledOnce();
    await waitFor(() => expect(pauseLatest).toHaveBeenCalledOnce());
    expect(
      within(screen.getByTestId("generated-audio-dock")).getByRole("button", {
        name: "Pause audio",
      }),
    ).toBeInTheDocument();
    expect(older.queryByRole("button", { name: "Pause audio" })).not.toBeInTheDocument();
    expect(older.getByLabelText("Listen to this response")).toBe(olderAudio);
    expect(
      within(screen.getByTestId("latest")).getByRole("button", { name: "Play audio" }),
    ).toBeInTheDocument();
  });
});
