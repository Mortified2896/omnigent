import {
  BufferTarget,
  CanvasSource,
  Mp4OutputFormat,
  Output,
  Quality,
  canEncodeVideo,
} from "mediabunny";

/** Encode visuals locally. The server adds the exact saved recording with FFmpeg. */
export async function exportVideo(
  draw: (ctx: CanvasRenderingContext2D, time: number) => void,
  duration: number,
  progress: (percent: number) => void,
  signal: AbortSignal,
): Promise<Blob> {
  if (!(await canEncodeVideo("avc", { width: 1280, height: 720, frameRate: 30 }))) {
    throw Error("This browser cannot encode H.264 video. Try current Chrome or Safari.");
  }
  const canvas = document.createElement("canvas");
  canvas.width = 1280;
  canvas.height = 720;
  const context = canvas.getContext("2d");
  if (!context) throw Error("Video preview is unavailable in this browser.");
  const target = new BufferTarget();
  const output = new Output({ format: new Mp4OutputFormat({ fastStart: "in-memory" }), target });
  const video = new CanvasSource(canvas, {
    codec: "avc",
    quality: new Quality({ bitrate: 450_000 }),
  });
  output.addVideoTrack(video, { frameRate: 30 });
  const frames = Math.ceil(duration * 30);
  try {
    await output.start();
    for (let frame = 0; frame < frames; frame++) {
      if (signal.aborted) throw Error("Video creation cancelled. Your report audio is preserved.");
      const time = frame / 30;
      draw(context, time);
      // Frames share one mutable Canvas and must be submitted in timestamp order.
      // eslint-disable-next-line no-await-in-loop
      await video.add(time, Math.min(1 / 30, duration - time));
      if (frame % 30 === 0) {
        progress(Math.floor((100 * frame) / frames));
        // Yield between batches so progress and cancellation remain responsive.
        // eslint-disable-next-line no-await-in-loop
        await new Promise<void>((resolve) => {
          setTimeout(resolve, 0);
        });
      }
    }
    video.close();
    await output.finalize();
    if (!target.buffer) throw Error("The browser produced no video.");
    if (target.buffer.byteLength > 128 * 1024 * 1024)
      throw Error("Video exceeds 128 MB. Try a shorter report.");
    progress(100);
    return new Blob([target.buffer], { type: "video/mp4" });
  } catch (error) {
    if (output.state !== "finalized" && output.state !== "canceled") await output.cancel();
    throw error;
  }
}
