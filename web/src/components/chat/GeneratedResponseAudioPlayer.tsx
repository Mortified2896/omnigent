import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";
import { getOmnigentHostConfig } from "@/lib/host";
import {
  activeReadAlongUnitIndex,
  clearReadAlongHighlight,
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
    if (!audio || !bubble || bubble.dataset.responseId !== responseId || !timings?.units?.length) {
      return undefined;
    }
    let rangeByUnit = new Map<number, Range>();
    const owner = `${sessionId}:${responseId}`;
    let lastUnitIndex = -2;
    clearReadAlongHighlight(owner);

    const updateHighlight = (force = false) => {
      const unitIndex = activeReadAlongUnitIndex(timings.units, audio.currentTime);
      if (!force && unitIndex === lastUnitIndex) return;
      lastUnitIndex = unitIndex;
      setReadAlongHighlight(owner, rangeByUnit.get(unitIndex) ?? null);
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
      const sections = bubble.querySelectorAll<HTMLElement>(
        '[data-testid="assistant-text-section"]',
      );
      rangeByUnit = new Map(
        mapReadAlongRanges(sections, timings.units).map(({ unitIndex, range }) => [
          unitIndex,
          range,
        ]),
      );
      if (lastUnitIndex !== -2) updateHighlight(true);
    };
    rebuildRanges();
    let rebuildFrame: number | null = null;
    const observer = new MutationObserver(() => {
      if (rebuildFrame !== null) return;
      rebuildFrame = requestAnimationFrame(() => {
        rebuildFrame = null;
        rebuildRanges();
      });
    });
    observer.observe(bubble, { childList: true, characterData: true, subtree: true });

    audio.addEventListener("play", handlePlay);
    audio.addEventListener("timeupdate", handleTimeUpdate);
    audio.addEventListener("seeking", handleSeeking);
    audio.addEventListener("seeked", handleSeeking);
    audio.addEventListener("ended", handleEnded);
    if (!audio.paused) updateHighlight(true);

    return () => {
      observer.disconnect();
      if (rebuildFrame !== null) cancelAnimationFrame(rebuildFrame);
      audio.removeEventListener("play", handlePlay);
      audio.removeEventListener("timeupdate", handleTimeUpdate);
      audio.removeEventListener("seeking", handleSeeking);
      audio.removeEventListener("seeked", handleSeeking);
      audio.removeEventListener("ended", handleEnded);
      clearReadAlongHighlight(owner);
    };
  }, [audioElement, responseId, sessionId, timingsQuery.data]);

  if (listQuery.isError) return null;
  if (!entry) return null;
  if (entry.status === "pending" || entry.status === "processing") {
    return (
      <span className="text-[11px] text-muted-foreground" role="status">
        Preparing audio…
      </span>
    );
  }
  if (entry.status === "failed") {
    return (
      <span className="text-[11px] text-muted-foreground" role="status">
        Audio unavailable
      </span>
    );
  }
  if (useBlobTransport && contentQuery.isError) {
    return (
      <div className="mb-4 flex flex-col gap-2 text-sm text-muted-foreground" role="status">
        <span>Audio download was interrupted.</span>
        <div className="flex gap-3">
          <button className="underline" onClick={() => void contentQuery.refetch()}>
            Retry audio
          </button>
          <a className="underline" href={contentPath}>
            Open the recording
          </a>
        </div>
      </div>
    );
  }
  if (useBlobTransport && !audioUrl) {
    return (
      <span className="text-[11px] text-muted-foreground" role="status">
        Loading audio…
      </span>
    );
  }
  return (
    <div className="mb-4 flex w-full flex-col gap-2 rounded-lg border border-border bg-muted/40 p-3">
      <span className="text-sm font-medium">Listen to this response</span>
      {mediaFailed && (
        <span role="status" className="text-sm text-muted-foreground">
          Audio could not load. You can{" "}
          <a className="underline" href={contentPath}>
            open the recording
          </a>
          .
        </span>
      )}
      <audio
        ref={setAudioElement}
        className="h-9 w-full max-w-[360px]"
        controls
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
          if (!pending || !audio) return;
          blobFallbackPlayback.current = null;
          if (
            Number.isFinite(pending.position) &&
            pending.position > 0 &&
            (!Number.isFinite(audio.duration) || pending.position < audio.duration)
          ) {
            audio.currentTime = pending.position;
          }
          if (pending.resume) void audio.play().catch(() => undefined);
        }}
        onCanPlay={() => setMediaFailed(false)}
        aria-label="Listen to this response"
      />
    </div>
  );
}
