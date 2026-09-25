import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";

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

async function readGeneratedAudioList(sessionId: string): Promise<GeneratedAudioWire[]> {
  const response = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(sessionId)}/generated-audio`,
  );
  if (!response.ok) throw new Error("Could not load generated audio");
  return ((await response.json()) as GeneratedAudioListWire).data;
}

async function readGeneratedAudio(sessionId: string, responseId: string): Promise<Blob> {
  const response = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(sessionId)}/generated-audio/${encodeURIComponent(responseId)}/content`,
  );
  if (!response.ok) throw new Error("Could not load generated audio");
  return response.blob();
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
  const contentQuery = useQuery({
    queryKey: ["generated-response-audio-content", sessionId, responseId],
    queryFn: () => readGeneratedAudio(sessionId, responseId),
    enabled: entry?.status === "ready",
    staleTime: Infinity,
  });
  const [audioUrl, setAudioUrl] = useState<string | null>(null);

  useEffect(() => {
    if (!contentQuery.data) {
      setAudioUrl(null);
      return;
    }
    const objectUrl = URL.createObjectURL(contentQuery.data);
    setAudioUrl(objectUrl);
    return () => URL.revokeObjectURL(objectUrl);
  }, [contentQuery.data]);

  if (listQuery.isError) return null;
  if (!entry) return null;
  if (entry.status === "pending" || entry.status === "processing") {
    return (
      <span className="text-[11px] text-muted-foreground" role="status">
        Preparing audio…
      </span>
    );
  }
  if (entry.status === "failed" || contentQuery.isError) {
    return (
      <span className="text-[11px] text-muted-foreground" role="status">
        Audio unavailable
      </span>
    );
  }
  if (!audioUrl) {
    return (
      <span className="text-[11px] text-muted-foreground" role="status">
        Loading audio…
      </span>
    );
  }
  return (
    <div className="mb-4 flex w-full flex-col gap-2 rounded-lg border border-border bg-muted/40 p-3">
      <span className="text-sm font-medium">Listen to this response</span>
      <audio
        className="h-9 w-full max-w-[360px]"
        controls
        preload="metadata"
        src={audioUrl}
        aria-label="Listen to this response"
      />
    </div>
  );
}
