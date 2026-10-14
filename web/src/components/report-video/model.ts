import { authenticatedFetch } from "@/lib/identity";
import type { ReadAlongUnit } from "@/components/chat/readAlongHighlight";

export interface Scene {
  start: number;
  title: string;
  summary: string;
  image_query: string;
  image_id: string | null;
}
export interface VideoImage {
  id: string;
  title: string;
  source: string;
  credit: string;
  license: string;
}
export interface Project {
  schema_version: 1;
  audio_sha256: string;
  duration: number;
  scenes: Scene[];
  units: ReadAlongUnit[];
  timing: "native" | "estimated";
  images: Record<string, VideoImage>;
  saved: { video_sha256: string; byte_size: number } | null;
  ffmpeg_available: boolean;
}
export interface Recording {
  response_id: string;
  status: string;
  duration_seconds: number | null;
  updated_at: number;
}
export const audioPath = (session: string, response?: string) =>
  `/v1/sessions/${encodeURIComponent(session)}/generated-audio${response ? `/${encodeURIComponent(response)}` : ""}`;

export async function videoRequest(path: string, init?: RequestInit): Promise<Response> {
  const result = await authenticatedFetch(path, init);
  if (!result.ok) {
    const error = await result.json().catch(() => null);
    throw Error(error?.detail ?? error?.message ?? `Request failed (HTTP ${result.status})`);
  }
  return result;
}
export function sceneAt(scenes: Scene[], time: number): number {
  let index = 0;
  // Native media clocks round to microseconds when seeking. Treat that rounding
  // as the boundary so a paused story preview does not show the previous card.
  while (index + 1 < scenes.length && scenes[index + 1].start <= time + 0.000001) index++;
  return index;
}
export function validateScenes(scenes: Scene[], duration: number): void {
  if (!scenes.length || scenes.length > 32 || scenes[0].start !== 0) {
    throw Error("The first scene must start at 0 seconds.");
  }
  scenes.forEach((scene, index) => {
    if (
      !Number.isFinite(scene.start) ||
      scene.start < 0 ||
      scene.start >= duration ||
      (index > 0 && scene.start <= scenes[index - 1].start)
    ) {
      throw Error("Scene times must increase and stay within the recording.");
    }
    if (!scene.title.trim()) throw Error("Every scene needs a title.");
  });
}
export function clock(seconds: number): string {
  return `${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, "0")}`;
}
