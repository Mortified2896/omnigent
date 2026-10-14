import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  audioPath,
  clock,
  validateScenes,
  videoRequest,
  type Project,
  type Recording,
  type Scene,
  type VideoImage,
} from "./model";
import { renderer } from "./draw";

export default function ReportVideoDialog({
  sessionId,
  onClose,
}: {
  sessionId: string;
  onClose: () => void;
}) {
  const [response, setResponse] = useState("");
  const [busy, setBusy] = useState(false);
  const recordings = useQuery({
    queryKey: ["generated-response-audio", sessionId],
    queryFn: async () =>
      ((await (await videoRequest(audioPath(sessionId))).json()) as { data: Recording[] }).data,
    retry: false,
  });
  const ready =
    recordings.data
      ?.filter(
        (row) => row.status === "ready" && row.duration_seconds && row.duration_seconds <= 1800,
      )
      .sort((a, b) => b.updated_at - a.updated_at) ?? [];
  const selected = ready.some((row) => row.response_id === response)
    ? response
    : ready[0]?.response_id;
  useEffect(() => {
    // Pause report playback without changing its position or speed.
    document.querySelectorAll<HTMLAudioElement>("audio").forEach((audio) => audio.pause());
  }, []);
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open && !busy) onClose();
      }}
    >
      <DialogContent
        className="flex max-h-[92dvh] min-w-0 flex-col overflow-x-hidden overflow-y-auto sm:max-w-5xl"
        onInteractOutside={(event) => event.preventDefault()}
      >
        <DialogHeader>
          <DialogTitle>Video report</DialogTitle>
          <DialogDescription>
            The Finance presenter, illustrated story cards, and your saved narration. Create an MP4
            on this Mac when you want one.
          </DialogDescription>
        </DialogHeader>
        {recordings.isPending && <p role="status">Loading saved recordings…</p>}
        {recordings.isError && (
          <p role="alert">
            Could not load recordings.{" "}
            <Button variant="outline" onClick={() => void recordings.refetch()}>
              Retry
            </Button>
          </p>
        )}
        {!recordings.isPending && !recordings.isError && !ready.length && (
          <p>Generate report audio first. Video supports saved recordings up to 30 minutes.</p>
        )}
        {ready.length > 1 && (
          <label className="flex flex-col gap-1 text-sm">
            Recording
            <select
              className="w-full min-w-0 rounded-md border bg-background p-2"
              value={selected}
              disabled={busy}
              onChange={(event) => setResponse(event.target.value)}
            >
              {ready.map((row) => (
                <option key={row.response_id} value={row.response_id}>
                  {new Date(row.updated_at * 1000).toLocaleString()} ·{" "}
                  {clock(row.duration_seconds!)}
                </option>
              ))}
            </select>
          </label>
        )}
        {selected && (
          <Editor key={selected} sessionId={sessionId} responseId={selected} onBusy={setBusy} />
        )}
      </DialogContent>
    </Dialog>
  );
}

function Editor({
  sessionId,
  responseId,
  onBusy,
}: {
  sessionId: string;
  responseId: string;
  onBusy: (busy: boolean) => void;
}) {
  const base = `${audioPath(sessionId, responseId)}/video`;
  const client = useQueryClient();
  const query = useQuery({
    queryKey: ["report-video", sessionId, responseId],
    queryFn: async () => (await (await videoRequest(base)).json()) as Project,
    retry: false,
  });
  const audioQuery = useQuery({
    queryKey: ["generated-response-audio-content", sessionId, responseId, "mp3"],
    queryFn: async () =>
      (await videoRequest(`${audioPath(sessionId, responseId)}/content?format=mp3`)).blob(),
    staleTime: Infinity,
    retry: false,
  });
  const [scenes, setScenes] = useState<Scene[]>([]);
  const [assets, setAssets] = useState<Record<string, VideoImage>>({});
  const [index, setIndex] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState("");
  const [progress, setProgress] = useState<number | null>(null);
  const [searching, setSearching] = useState(false);
  const [searchIds, setSearchIds] = useState<string[]>([]);
  const [urls, setUrls] = useState<Record<string, string>>({});
  const [audioUrl, setAudioUrl] = useState("");
  const [videoUrl, setVideoUrl] = useState("");
  const [watching, setWatching] = useState(false);
  const audio = useRef<HTMLAudioElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const imageElements = useRef(new Map<string, HTMLImageElement>());
  const controller = useRef<AbortController | null>(null);
  const busy = progress !== null;
  useEffect(() => {
    onBusy(busy);
  }, [busy, onBusy]);
  useEffect(() => {
    if (query.data) {
      setScenes(query.data.scenes);
      setAssets(query.data.images);
    }
  }, [query.data]);
  useEffect(() => {
    if (!audioQuery.data) return;
    const url = URL.createObjectURL(audioQuery.data);
    setAudioUrl(url);
    return () => URL.revokeObjectURL(url);
  }, [audioQuery.data]);
  useEffect(
    () => () => {
      controller.current?.abort();
    },
    [],
  );
  useEffect(
    () => () => {
      if (videoUrl) URL.revokeObjectURL(videoUrl);
    },
    [videoUrl],
  );
  useEffect(() => {
    let disposed = false;
    const created: string[] = [];
    const elements = imageElements.current;
    // Use authenticated local Blobs, so Canvas export stays readable in embedded hosts.
    void Promise.all(
      Object.values(assets).map(async (asset) => {
        const blob = await (await videoRequest(`${base}/images/${asset.id}`)).blob();
        if (disposed) return;
        const url = URL.createObjectURL(blob);
        created.push(url);
        const image = new Image();
        image.src = url;
        await image.decode();
        if (disposed) return;
        elements.set(asset.id, image);
        setUrls((previous) => ({ ...previous, [asset.id]: url }));
      }),
    ).catch(() => {
      if (!disposed) setError("Could not load an image. Search again or use a graphic card.");
    });
    return () => {
      disposed = true;
      created.forEach((url) => URL.revokeObjectURL(url));
      elements.clear();
      setUrls({});
    };
  }, [assets, base]);
  const draw = useMemo(
    () =>
      query.data && scenes.length
        ? renderer({ ...query.data, images: assets }, scenes, imageElements.current)
        : null,
    [query.data, scenes, assets],
  );
  useEffect(() => {
    if (!draw) return;
    let frame = 0;
    const paint = () => {
      const context = canvas.current?.getContext("2d");
      if (context) draw(context, audio.current?.currentTime ?? 0, !audio.current?.paused);
    };
    const tick = () => {
      paint();
      if (!audio.current?.paused) frame = requestAnimationFrame(tick);
    };
    const start = () => {
      cancelAnimationFrame(frame);
      tick();
    };
    const element = audio.current;
    element?.addEventListener("play", start);
    element?.addEventListener("seeked", paint);
    element?.addEventListener("pause", paint);
    paint();
    if (element && !element.paused) start();
    return () => {
      cancelAnimationFrame(frame);
      element?.removeEventListener("play", start);
      element?.removeEventListener("seeked", paint);
      element?.removeEventListener("pause", paint);
    };
  }, [draw, urls, audioUrl]);
  const scene = scenes[index];
  function edit(change: Partial<Scene>) {
    setScenes((previous) =>
      previous.map((item, i) => (i === index ? { ...item, ...change } : item)),
    );
    setError(null);
  }
  async function search() {
    if (!scene) return;
    setSearching(true);
    setError(null);
    try {
      const result = (await (
        await videoRequest(`${base}/images`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ query: scene.image_query }),
        })
      ).json()) as { images: VideoImage[] };
      setAssets((previous) => ({
        ...previous,
        ...Object.fromEntries(result.images.map((image) => [image.id, image])),
      }));
      setSearchIds(result.images.map((image) => image.id));
      setStatus(
        result.images.length
          ? "Choose an image below. Photos are illustrative; check the subject and source."
          : "No reusable images found. Try another search or use the graphic card.",
      );
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Image search failed.");
    } finally {
      setSearching(false);
    }
  }
  async function watch() {
    setWatching(true);
    setError(null);
    try {
      const blob = await (await videoRequest(`${base}/content`)).blob();
      setVideoUrl(URL.createObjectURL(blob));
      audio.current?.pause();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Video could not be loaded.");
    } finally {
      setWatching(false);
    }
  }
  async function create() {
    if (!query.data || !draw) return;
    setError(null);
    try {
      validateScenes(scenes, query.data.duration);
      if (scenes.some((item) => item.image_id && !imageElements.current.has(item.image_id)))
        throw Error("Wait for selected images to load before creating the video.");
      audio.current?.pause();
      setProgress(0);
      setStatus("Rendering video on this device…");
      const abort = new AbortController();
      controller.current = abort;
      const { exportVideo } = await import("./export");
      const visuals = await exportVideo(draw, query.data.duration, setProgress, abort.signal);
      if (abort.signal.aborted) return;
      setStatus("Adding the original narration and saving the MP4 on the Mac…");
      const manifest = {
        schema_version: 1,
        audio_sha256: query.data.audio_sha256,
        duration: query.data.duration,
        scenes,
      };
      await videoRequest(base, {
        method: "POST",
        headers: { "Content-Type": "application/octet-stream" },
        body: new Blob([JSON.stringify(manifest), "\n", visuals]),
        signal: abort.signal,
      });
      await client.invalidateQueries({ queryKey: ["report-video", sessionId, responseId] });
      setVideoUrl("");
      setStatus("Video saved. Watch or download it below.");
    } catch (cause) {
      setError(
        controller.current?.signal.aborted
          ? "Video creation cancelled. Your report audio is preserved."
          : cause instanceof Error
            ? cause.message
            : "Could not create the video.",
      );
    } finally {
      setProgress(null);
      controller.current = null;
    }
  }
  if (query.isPending) return <p role="status">Preparing story cards…</p>;
  if (query.isError)
    return (
      <p role="alert">
        {query.error.message}{" "}
        <Button variant="outline" onClick={() => void query.refetch()}>
          Retry
        </Button>
      </p>
    );
  return (
    <div className="flex min-w-0 flex-col gap-4">
      <canvas
        ref={canvas}
        width={1280}
        height={720}
        aria-label="Report video preview"
        className="aspect-video w-full rounded-lg bg-[#f5f3ee]"
      />
      {audioUrl && (
        <audio
          ref={audio}
          controls
          preload="auto"
          src={audioUrl}
          aria-label="Video narration preview"
          className="w-full"
        />
      )}
      {audioQuery.isError && (
        <p role="alert">
          Audio preview could not load.{" "}
          <Button variant="outline" onClick={() => void audioQuery.refetch()}>
            Retry audio
          </Button>
        </p>
      )}
      <p className="text-xs text-muted-foreground">
        {query.data?.timing === "native"
          ? "Scenes use saved ElevenLabs timings."
          : "Estimated scene timing for this older recording; adjust scene starts if needed."}{" "}
        The presenter uses stylized speaking animation.
      </p>
      {scene && (
        <fieldset disabled={busy} className="flex min-w-0 flex-col gap-3 rounded-lg border p-3">
          <label className="flex flex-col gap-1 text-sm">
            Story card
            <select
              className="w-full min-w-0 rounded-md border bg-background p-2"
              value={index}
              onChange={(event) => {
                const i = Number(event.target.value);
                setIndex(i);
                setSearchIds([]);
                setStatus("");
                if (audio.current) audio.current.currentTime = scenes[i].start;
              }}
            >
              {scenes.map((item, i) => (
                <option key={i === 0 ? "opening" : item.start} value={i}>
                  {clock(item.start)} · {item.title}
                </option>
              ))}
            </select>
          </label>
          <div className="grid gap-3 sm:grid-cols-[7rem_1fr]">
            <label className="flex flex-col gap-1 text-sm">
              Start (seconds)
              <Input
                type="number"
                min={0}
                max={query.data?.duration}
                step="0.01"
                disabled={index === 0}
                value={scene.start}
                onChange={(event) => edit({ start: Number(event.target.value) })}
              />
            </label>
            <label className="flex flex-col gap-1 text-sm">
              Title
              <Input
                maxLength={160}
                value={scene.title}
                onChange={(event) => edit({ title: event.target.value })}
              />
            </label>
          </div>
          <label className="flex flex-col gap-1 text-sm">
            Card text
            <Textarea
              rows={3}
              style={{ fieldSizing: "fixed" }}
              className="min-w-0"
              maxLength={600}
              value={scene.summary}
              onChange={(event) => edit({ summary: event.target.value })}
            />
          </label>
          <label className="flex flex-col gap-1 text-sm">
            Search for an illustrative image
            <div className="flex flex-wrap gap-2">
              <Input
                className="min-w-0 flex-1"
                maxLength={180}
                value={scene.image_query}
                onChange={(event) => edit({ image_query: event.target.value })}
              />
              <Button
                variant="outline"
                disabled={searching || scene.image_query.trim().length < 2}
                onClick={() => void search()}
              >
                {searching ? "Searching…" : "Search images"}
              </Button>
            </div>
          </label>
          <p className="text-xs text-muted-foreground">
            Search terms go to Wikimedia Commons. Choose public domain or Creative Commons photos
            with their source credits.
          </p>
          <Button variant="outline" className="self-start" onClick={() => edit({ image_id: null })}>
            Use graphic card
          </Button>
          {(searchIds.length > 0 || scene.image_id) && (
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              {[...new Set([...searchIds, ...(scene.image_id ? [scene.image_id] : [])])].map(
                (id) =>
                  assets[id] && (
                    <div
                      key={id}
                      className={`rounded-md border p-2 ${scene.image_id === id ? "border-primary ring-1 ring-primary" : ""}`}
                    >
                      {urls[id] && (
                        <img
                          src={urls[id]}
                          alt={assets[id].title.replace(/^File:/, "")}
                          className="h-32 w-full object-contain"
                        />
                      )}
                      <p className="mt-1 break-words text-xs">
                        {assets[id].title.replace(/^File:/, "")}
                      </p>
                      <p className="text-xs text-muted-foreground">
                        {assets[id].credit} · {assets[id].license}
                      </p>
                      <div className="mt-2 flex items-center justify-between gap-2">
                        <Button
                          variant={scene.image_id === id ? "default" : "outline"}
                          size="sm"
                          onClick={() => edit({ image_id: id })}
                        >
                          {scene.image_id === id ? "Selected" : "Use image"}
                        </Button>
                        <a
                          className="text-xs underline"
                          href={assets[id].source}
                          target="_blank"
                          rel="noopener noreferrer"
                        >
                          Source
                        </a>
                      </div>
                    </div>
                  ),
              )}
            </div>
          )}
        </fieldset>
      )}
      {!query.data?.ffmpeg_available && (
        <p role="alert">Local FFmpeg is needed to add narration and save the MP4.</p>
      )}
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}
      {status && (
        <p role="status" className="text-sm">
          {status}
        </p>
      )}
      {busy && (
        <progress
          className="w-full"
          max={100}
          value={progress ?? 0}
          aria-label="Video creation progress"
        />
      )}
      <div className="flex flex-wrap gap-2">
        <Button
          disabled={busy || searching || !query.data?.ffmpeg_available || !scenes.length}
          onClick={() => void create()}
        >
          {busy
            ? `Creating video… ${progress}%`
            : query.data?.saved
              ? "Create updated video"
              : "Create video"}
        </Button>
        {busy && (
          <Button
            variant="outline"
            disabled={progress === 100}
            onClick={() => controller.current?.abort()}
          >
            Cancel creation
          </Button>
        )}
        {query.data?.saved && (
          <Button variant="outline" disabled={watching || busy} onClick={() => void watch()}>
            {watching ? "Loading video…" : "Watch saved video"}
          </Button>
        )}
        {videoUrl && (
          <Button asChild variant="outline">
            <a href={videoUrl} download="omnigent-daily-report.mp4">
              Download MP4
            </a>
          </Button>
        )}
      </div>
      {videoUrl && (
        <video
          controls
          src={videoUrl}
          aria-label="Saved report video"
          className="aspect-video w-full rounded-lg"
        />
      )}
      <p className="text-xs text-muted-foreground">
        720p MP4 · Rendering uses this device; finishing uses the Mac. Reuses the exact saved
        narration. Videos and images share the report audio’s retention period.
      </p>
    </div>
  );
}
