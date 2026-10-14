import { useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import {
  PauseIcon,
  PlayIcon,
  RotateCcwIcon,
  RotateCwIcon,
  Volume2Icon,
  VolumeXIcon,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { GeneratedAudioDockContext } from "./GeneratedAudioDock";
import { useQuery } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";
import { getOmnigentHostConfig } from "@/lib/host";
import {
  activeReadAlongUnitIndex,
  clearReadAlongHighlight,
  estimateReadAlongUnits,
  followReadAlongRange,
  mapReadAlongRanges,
  setReadAlongHighlight,
  type ReadAlongUnit,
} from "./readAlongHighlight";

interface GeneratedAudioWire {
  response_id: string;
  status: "pending" | "processing" | "ready" | "failed";
  duration_seconds: number | null;
  sample_rate: number | null;
  error_code: string | null;
  updated_at: number;
  generation?: {
    model: string;
    narration_characters: number;
    completed_chunks: number;
    total_chunks: number | null;
    elapsed_seconds: number;
    remaining_seconds_low: number | null;
    remaining_seconds_high: number | null;
    taking_longer: boolean;
    estimate_basis: string;
  } | null;
}

interface GeneratedAudioListWire {
  data: GeneratedAudioWire[];
}

interface GeneratedAudioTimingsWire {
  schema_version: 1;
  engine: string;
  audio_sha256: string;
  narration_sha256: string;
  duration_seconds: number;
  position_unit: "unicode-code-point";
  units: ReadAlongUnit[];
}

async function readGeneratedAudioList(sessionId: string): Promise<GeneratedAudioWire[]> {
  const response = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(sessionId)}/generated-audio`,
  );
  if (!response.ok) throw new Error("Could not load generated audio");
  return ((await response.json()) as GeneratedAudioListWire).data;
}

async function readGeneratedAudio(sessionId: string, responseId: string): Promise<Blob> {
  const response = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(sessionId)}/generated-audio/${encodeURIComponent(responseId)}/content?format=mp3`,
  );
  if (!response.ok) throw new Error(`Audio request failed (HTTP ${response.status})`);
  return response.blob();
}

async function readGeneratedAudioTimings(
  sessionId: string,
  responseId: string,
): Promise<GeneratedAudioTimingsWire> {
  const response = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(sessionId)}/generated-audio/${encodeURIComponent(responseId)}/timings`,
  );
  if (!response.ok) throw new Error("Generated audio timings are unavailable");
  return (await response.json()) as GeneratedAudioTimingsWire;
}

/** Compact, response-keyed audio controls; no row means no transcript change. */
export function GeneratedResponseAudioPlayer({
  sessionId,
  responseId,
  pollForNewAudio,
}: {
  sessionId: string;
  responseId: string;
  pollForNewAudio: boolean;
}) {
  const listQuery = useQuery({
    queryKey: ["generated-response-audio", sessionId],
    queryFn: () => readGeneratedAudioList(sessionId),
    staleTime: 0,
    refetchInterval: (query) => {
      const row = query.state.data?.find((item) => item.response_id === responseId);
      if (row?.status === "pending" || row?.status === "processing") return 3_000;
      // A scheduled response can acquire audio after a long generation delay.
      // Keep checking the latest response while mounted; React Query pauses in background.
      if (pollForNewAudio && !row) return 30_000;
      return false;
    },
  });
  const entry = listQuery.data?.find((item) => item.response_id === responseId);
  const timingsQuery = useQuery({
    queryKey: ["generated-response-audio-timings", sessionId, responseId],
    queryFn: () => readGeneratedAudioTimings(sessionId, responseId),
    enabled: entry?.status === "ready",
    staleTime: Infinity,
    retry: false,
  });
  // Standalone media requests carry same-origin cookies / trusted proxy auth.
  // Let the browser request byte ranges instead of waiting for a complete WAV.
  // Embedded hosts still need their custom authenticated transport.
  const needsBlob = Boolean(getOmnigentHostConfig().fetcher);
  const [forceBlobFallback, setForceBlobFallback] = useState(false);
  const useBlobTransport = needsBlob || forceBlobFallback;
  const contentPath = `/v1/sessions/${encodeURIComponent(sessionId)}/generated-audio/${encodeURIComponent(responseId)}/content?format=mp3`;
  const [mediaFailed, setMediaFailed] = useState(false);
  const [playbackBlocked, setPlaybackBlocked] = useState(false);
  const blobFallbackPlayback = useRef<{ position: number; resume: boolean } | null>(null);
  const contentQuery = useQuery({
    queryKey: ["generated-response-audio-content", sessionId, responseId, "mp3"],
    queryFn: () => readGeneratedAudio(sessionId, responseId),
    enabled: entry?.status === "ready" && useBlobTransport,
    staleTime: Infinity,
    retry: false,
  });
  const [audioUrl, setAudioUrl] = useState<string | null>(null);
  const [audioElement, setAudioElement] = useState<HTMLAudioElement | null>(null);
  const dock = useContext(GeneratedAudioDockContext);
  const playerKey = `${sessionId}:${responseId}`;
  const selectDock = dock?.select;
  const docked = Boolean(
    dock?.element && (dock.activeKey ? dock.activeKey === playerKey : pollForNewAudio),
  );
  const renderControls = (node: ReactNode) =>
    docked && dock?.element ? createPortal(node, dock.element) : node;
  const [position, setPosition] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [followText, setFollowText] = useState(true);
  const [playbackRate, setPlaybackRate] = useState(1);
  const [muted, setMuted] = useState(false);
  const [mediaDuration, setMediaDuration] = useState<number | null>(null);
  const duration = mediaDuration ?? entry?.duration_seconds ?? 0;
  const clock = (seconds: number) => {
    const value = Math.max(0, Math.floor(Number.isFinite(seconds) ? seconds : 0));
    return `${Math.floor(value / 60)}:${String(value % 60).padStart(2, "0")}`;
  };
  const seek = (seconds: number) => {
    if (!audioElement) return;
    audioElement.currentTime = Math.min(duration, Math.max(0, seconds));
    setPosition(audioElement.currentTime);
  };
  const togglePlayback = async () => {
    if (!audioElement) return;
    selectDock?.(playerKey);
    if (!audioElement.paused) audioElement.pause();
    else {
      setPlaybackBlocked(false);
      try {
        await audioElement.play();
      } catch (error) {
        // A pause or source switch rejects an outstanding play request. It is
        // not evidence that the recording failed to load.
        const name = error && typeof error === "object" && "name" in error ? error.name : "";
        if (name === "AbortError") return;
        if (name === "NotAllowedError") setPlaybackBlocked(true);
        else setMediaFailed(true);
      }
    }
  };
  const retryPlayback = () => {
    if (!audioElement) return;
    blobFallbackPlayback.current = { position: audioElement.currentTime, resume: false };
    setMediaFailed(false);
    setPlaybackBlocked(false);
    // Reload the existing recording; this never asks the speech worker to
    // regenerate it. Keep time/rate/mute through the metadata handler.
    audioElement.load();
  };
  useEffect(
    () => () => selectDock?.((active) => (active === playerKey ? null : active)),
    [playerKey, selectDock],
  );
  useEffect(() => {
    if (dock?.activeKey && dock.activeKey !== playerKey && audioElement && !audioElement.paused) {
      audioElement.pause();
    }
  }, [dock?.activeKey, playerKey, audioElement]);
  useEffect(
    () => () => {
      if (audioElement && !audioElement.paused) audioElement.pause();
    },
    [audioElement],
  );

  useEffect(() => {
    if (!contentQuery.data) {
      setAudioUrl(null);
      return;
    }
    const objectUrl = URL.createObjectURL(contentQuery.data);
    setAudioUrl(objectUrl);
    return () => URL.revokeObjectURL(objectUrl);
  }, [contentQuery.data]);

  useEffect(() => {
    const audio = audioElement;
    const bubble = audio?.closest<HTMLElement>("[data-response-id]");
    const timings = timingsQuery.data;
    if (!audio || !bubble || bubble.dataset.responseId !== responseId) {
      return undefined;
    }
    const getSections = () =>
      bubble.querySelectorAll<HTMLElement>('[data-testid="assistant-text-section"]');
    const getUnits = (sections = getSections()) =>
      timings?.units?.length
        ? timings.units
        : estimateReadAlongUnits(sections, entry?.duration_seconds ?? audio.duration);
    let units = getUnits();
    let rangeByUnit = new Map<number, Range>();
    const owner = `${sessionId}:${responseId}`;
    let lastUnitIndex = -2;
    clearReadAlongHighlight(owner);

    const updateHighlight = (force = false) => {
      const unitIndex = activeReadAlongUnitIndex(units, audio.currentTime);
      if (!force && unitIndex === lastUnitIndex) return;
      lastUnitIndex = unitIndex;
      const range = rangeByUnit.get(unitIndex) ?? null;
      setReadAlongHighlight(owner, range);
      if (followText && !audio.paused && range) followReadAlongRange(range);
    };
    const handlePlay = () => updateHighlight(true);
    const handleTimeUpdate = () => {
      if (!audio.paused) updateHighlight();
    };
    const handleSeeking = () => updateHighlight(true);
    const handleEnded = () => {
      lastUnitIndex = -2;
      clearReadAlongHighlight(owner);
    };
    // Markdown loads lazily, and collapsed work sections can mount later.
    // Rebuild only when text DOM changes, never for each playback tick.
    const rebuildRanges = () => {
      const sections = getSections();
      units = getUnits(sections);
      rangeByUnit = new Map(
        mapReadAlongRanges(sections, units).map(({ unitIndex, range }) => [unitIndex, range]),
      );
      if (lastUnitIndex !== -2) updateHighlight(true);
    };
    const handleMetadata = () => {
      rebuildRanges();
      updateHighlight(true);
    };
    rebuildRanges();
    let rebuildFrame: number | null = null;
    const observer = new MutationObserver((records) => {
      if (
        records.every((record) =>
          (record.target instanceof Element ? record.target : record.target.parentElement)?.closest(
            "[data-audio-player-controls]",
          ),
        )
      )
        return;
      if (rebuildFrame !== null) return;
      rebuildFrame = requestAnimationFrame(() => {
        rebuildFrame = null;
        rebuildRanges();
      });
    });
    observer.observe(bubble, { childList: true, characterData: true, subtree: true });

    audio.addEventListener("play", handlePlay);
    audio.addEventListener("loadedmetadata", handleMetadata);
    audio.addEventListener("timeupdate", handleTimeUpdate);
    audio.addEventListener("seeking", handleSeeking);
    audio.addEventListener("seeked", handleSeeking);
    audio.addEventListener("ended", handleEnded);
    if (!audio.paused) updateHighlight(true);

    return () => {
      observer.disconnect();
      if (rebuildFrame !== null) cancelAnimationFrame(rebuildFrame);
      audio.removeEventListener("play", handlePlay);
      audio.removeEventListener("loadedmetadata", handleMetadata);
      audio.removeEventListener("timeupdate", handleTimeUpdate);
      audio.removeEventListener("seeking", handleSeeking);
      audio.removeEventListener("seeked", handleSeeking);
      audio.removeEventListener("ended", handleEnded);
      clearReadAlongHighlight(owner);
    };
  }, [audioElement, entry?.duration_seconds, followText, responseId, sessionId, timingsQuery.data]);

  if (listQuery.isError) return null;
  if (!entry) return null;
  if (entry.status === "pending" || entry.status === "processing") {
    const estimate = entry.generation;
    const minutes = (seconds: number) => Math.max(1, Math.ceil(seconds / 60));
    const remaining =
      estimate?.remaining_seconds_high != null && estimate.remaining_seconds_low != null
        ? `about ${minutes(estimate.remaining_seconds_low)}–${minutes(estimate.remaining_seconds_high)} min remaining`
        : null;
    return renderControls(
      <div
        className="rounded-xl border border-border bg-card px-3 py-2 text-xs text-muted-foreground"
        role="status"
      >
        <p className="font-medium text-foreground">
          {entry.status === "pending" ? "Audio queued…" : "Preparing audio…"}
        </p>
        {estimate && (
          <>
            <p>
              {estimate.model === "eleven_v4" ? "ElevenLabs V4" : estimate.model} ·{" "}
              {estimate.narration_characters.toLocaleString()} characters
              {entry.status === "processing" && ` · ${clock(estimate.elapsed_seconds)} elapsed`}
            </p>
            <p>
              {estimate.taking_longer
                ? "Taking longer than estimated. Still waiting for the speech service."
                : remaining}
              {estimate.total_chunks != null &&
                ` · ${estimate.completed_chunks}/${estimate.total_chunks} sections complete`}
            </p>
            <p className="mt-1 text-[11px]">
              Approximate timing based on {estimate.estimate_basis}. Provider load can change it.
              {entry.status === "pending" && " Starts when the audio worker is free."}
            </p>
          </>
        )}
      </div>,
    );
  }
  if (entry.status === "failed") {
    return renderControls(
      <span className="text-[11px] text-muted-foreground" role="status">
        {entry.error_code === "audio_expired" || entry.error_code === "audio_expiring"
          ? "Recording removed after the retention period. The written report is still available."
          : "Audio unavailable"}
      </span>,
    );
  }
  if (useBlobTransport && contentQuery.isError) {
    return renderControls(
      <div className="mb-4 flex flex-col gap-2 text-sm text-muted-foreground" role="status">
        <span>Audio download was interrupted.</span>
        <div className="flex gap-3">
          <button type="button" className="underline" onClick={() => void contentQuery.refetch()}>
            Retry audio
          </button>
          <a className="underline" href={contentPath}>
            Open the recording
          </a>
        </div>
      </div>,
    );
  }
  if (useBlobTransport && !audioUrl) {
    return renderControls(
      <span className="text-[11px] text-muted-foreground" role="status">
        Loading audio…
      </span>,
    );
  }
  const controls = (
    <div
      role="region"
      aria-label="Response audio player"
      data-audio-player-controls
      className="flex w-full flex-wrap items-center gap-2 rounded-xl border border-border bg-card px-2 py-2 shadow-sm sm:flex-nowrap"
    >
      <div className="flex shrink-0 items-center gap-0.5">
        <Button
          variant="ghost"
          size="icon-sm"
          className="size-9 sm:size-8"
          aria-label="Back 15 seconds"
          title="Back 15 seconds"
          onClick={() => seek((audioElement?.currentTime ?? position) - 15)}
          disabled={!audioElement}
        >
          <span className="relative">
            <RotateCcwIcon className="size-5" />
            <span className="absolute inset-0 flex items-center justify-center text-[8px] font-semibold">
              15
            </span>
          </span>
        </Button>
        <Button
          size="icon-sm"
          className="size-9 sm:size-8"
          aria-label={playing ? "Pause audio" : "Play audio"}
          title={playing ? "Pause" : "Play"}
          onClick={() => void togglePlayback()}
          disabled={!audioElement}
        >
          {playing ? <PauseIcon className="size-4" /> : <PlayIcon className="size-4" />}
        </Button>
        <Button
          variant="ghost"
          size="icon-sm"
          className="size-9 sm:size-8"
          aria-label="Forward 15 seconds"
          title="Forward 15 seconds"
          onClick={() => seek((audioElement?.currentTime ?? position) + 15)}
          disabled={!audioElement}
        >
          <span className="relative">
            <RotateCwIcon className="size-5" />
            <span className="absolute inset-0 flex items-center justify-center text-[8px] font-semibold">
              15
            </span>
          </span>
        </Button>
      </div>
      <div className="order-first flex w-full min-w-0 flex-col gap-1 sm:order-none sm:w-auto sm:flex-1">
        <div className="flex items-center justify-between gap-2 text-xs">
          <span className="truncate font-medium">Listen along</span>
          <span className="whitespace-nowrap text-muted-foreground tabular-nums">
            {clock(position)} / {clock(duration)}
          </span>
        </div>
        <input
          type="range"
          aria-label="Audio position"
          aria-valuetext={`${clock(position)} of ${clock(duration)}`}
          min={0}
          max={duration || 1}
          step={1}
          value={Math.min(position, duration)}
          onChange={(event) => seek(Number(event.target.value))}
          disabled={!duration || !audioElement}
          className="h-5 w-full cursor-pointer accent-primary sm:h-4"
        />
      </div>
      <Button
        variant={followText ? "secondary" : "ghost"}
        size="sm"
        className="h-9 shrink-0 px-2 text-xs sm:h-8"
        aria-label="Follow text"
        aria-pressed={followText}
        title={followText ? "Turn off automatic text scrolling" : "Follow the spoken text"}
        onClick={() => setFollowText((value) => !value)}
      >
        Follow text
      </Button>
      <select
        aria-label="Playback speed"
        value={playbackRate}
        onChange={(event) => {
          const value = Number(event.target.value);
          setPlaybackRate(value);
          if (audioElement) audioElement.playbackRate = value;
        }}
        className="ml-auto h-9 shrink-0 rounded-md border border-input bg-background px-1 text-xs font-medium sm:ml-0 sm:h-8"
      >
        {[0.75, 1, 1.25, 1.5, 1.75, 2].map((speed) => (
          <option key={speed} value={speed}>
            {speed}×
          </option>
        ))}
      </select>
      <Button
        variant="ghost"
        size="icon-sm"
        className="size-9 sm:size-8"
        aria-label={muted ? "Unmute audio" : "Mute audio"}
        title={muted ? "Unmute" : "Mute"}
        onClick={() => {
          if (audioElement) audioElement.muted = !muted;
          setMuted(!muted);
        }}
      >
        {muted ? <VolumeXIcon className="size-4" /> : <Volume2Icon className="size-4" />}
      </Button>
    </div>
  );
  return (
    <>
      {renderControls(
        <>
          {controls}
          {mediaFailed && (
            <span role="status" className="text-sm text-muted-foreground">
              Playback was interrupted.{" "}
              <button type="button" className="underline" onClick={retryPlayback}>
                Retry playback
              </button>
              {" or "}
              <a className="underline" href={contentPath}>
                open the recording
              </a>
              .
            </span>
          )}
          {playbackBlocked && (
            <span role="status" className="text-sm text-muted-foreground">
              Press Play again to allow audio in this browser.
            </span>
          )}
        </>,
      )}
      <audio
        ref={setAudioElement}
        className="hidden"
        preload="metadata"
        src={useBlobTransport ? (audioUrl ?? undefined) : contentPath}
        onError={(event) => {
          if (!useBlobTransport) {
            const audio = event.currentTarget;
            blobFallbackPlayback.current = {
              position: audio.currentTime,
              resume: !audio.paused && !audio.ended,
            };
            setMediaFailed(false);
            setForceBlobFallback(true);
            return;
          }
          setMediaFailed(true);
        }}
        onLoadedMetadata={() => {
          const pending = blobFallbackPlayback.current;
          const audio = audioElement;
          if (!audio) return;
          audio.playbackRate = playbackRate;
          audio.muted = muted;
          if (Number.isFinite(audio.duration) && audio.duration > 0)
            setMediaDuration(audio.duration);
          setPosition(audio.currentTime);
          if (!pending) return;
          blobFallbackPlayback.current = null;
          if (
            Number.isFinite(pending.position) &&
            pending.position > 0 &&
            (!Number.isFinite(audio.duration) || pending.position < audio.duration)
          ) {
            audio.currentTime = pending.position;
            setPosition(audio.currentTime);
          }
          if (pending.resume) void audio.play().catch(() => undefined);
        }}
        onCanPlay={() => setMediaFailed(false)}
        onDurationChange={(event) => {
          const value = event.currentTarget.duration;
          if (Number.isFinite(value) && value > 0) setMediaDuration(value);
        }}
        onTimeUpdate={(event) => setPosition(event.currentTarget.currentTime)}
        onPlay={() => {
          setPlaying(true);
          setMediaFailed(false);
          setPlaybackBlocked(false);
        }}
        onPause={() => setPlaying(false)}
        onEnded={() => setPlaying(false)}
        aria-label="Listen to this response"
      />
    </>
  );
}
