import { activeReadAlongUnitIndex } from "@/components/chat/readAlongHighlight";
import { presenter } from "./presenter";
import { clock, sceneAt, type Project, type Scene } from "./model";

/** The same deterministic Canvas presenter used by the Finance experiment. */
export function renderer(project: Project, scenes: Scene[], images: Map<string, HTMLImageElement>) {
  return (c: CanvasRenderingContext2D, time: number, speaking = true) => {
    const index = sceneAt(scenes, time),
      scene = scenes[index];
    c.fillStyle = "#f5f3ee";
    c.fillRect(0, 0, 1280, 720);
    c.fillStyle = "#171817";
    c.fillRect(0, 0, 1280, 75);
    c.fillStyle = "#f5f3ee";
    c.font = "600 25px system-ui, sans-serif";
    c.fillText("OMNIGENT / DAILY BRIEFING", 40, 48);
    c.textAlign = "right";
    c.fillText(`${clock(time)} / ${clock(project.duration)}`, 1240, 48);
    c.textAlign = "left";
    c.fillStyle = "#e8e5da";
    c.beginPath();
    c.roundRect(30, 102, 292, 550, 18);
    c.fill();
    const word = project.units[activeReadAlongUnitIndex(project.units, time)];
    const talking =
      speaking && (project.timing === "estimated" || Boolean(word && time <= word.end_seconds));
    presenter(
      c,
      time,
      {
        x: 177,
        y: 354,
        scale: 1.38,
        mode: time - scene.start < 2 ? "point" : "idle",
        expression: "curious",
      },
      talking,
    );
    c.font = "500 17px system-ui, sans-serif";
    c.fillStyle = "#66695e";
    c.fillText("Your daily briefing", 87, 625);
    const wrap = (
      text: string,
      x: number,
      y: number,
      width: number,
      size: number,
      count: number,
      font = "system-ui, sans-serif",
    ) => {
      c.font = `${size}px ${font}`;
      const words = text.split(/\s+/).filter(Boolean);
      let line = "",
        row = 0;
      for (let i = 0; i < words.length; i++) {
        const next = line ? `${line} ${words[i]}` : words[i];
        if (line && c.measureText(next).width > width) {
          c.fillText(row === count - 1 ? `${line.slice(0, -1)}…` : line, x, y + row * size * 1.35);
          if (++row >= count) return row;
          line = words[i];
        } else line = next;
      }
      if (line) c.fillText(line, x, y + row++ * size * 1.35);
      return row;
    };
    c.fillStyle = "#74796a";
    c.font = "600 17px system-ui, sans-serif";
    c.fillText(`STORY ${index + 1} OF ${scenes.length}`, 365, 127);
    c.fillStyle = "#171817";
    wrap(scene.title, 365, 173, 860, 34, 2, "Georgia, serif");
    const image = scene.image_id ? images.get(scene.image_id) : undefined;
    if (image) {
      const scale = Math.min(385 / image.naturalWidth, 305 / image.naturalHeight);
      const w = image.naturalWidth * scale,
        h = image.naturalHeight * scale;
      c.drawImage(image, 365 + (385 - w) / 2, 265 + (305 - h) / 2, w, h);
      wrap(scene.summary, 785, 282, 435, 23, 10);
      const credit = project.images[scene.image_id!];
      if (credit) {
        c.fillStyle = "#565b50";
        wrap(
          `Illustrative image: ${credit.title.replace(/^File:/, "")} · ${credit.credit} · ${credit.license}`,
          365,
          593,
          855,
          13,
          3,
        );
        // Preserve the full source URL in the exported image credit.
        c.font = "12px system-ui, sans-serif";
        const url = credit.source;
        for (let offset = 0, row = 0; offset < url.length && row < 3; row++) {
          let end = offset + 1;
          while (end < url.length && c.measureText(url.slice(offset, end + 1)).width < 855) end++;
          c.fillText(url.slice(offset, end), 365, 643 + row * 14);
          offset = end;
        }
      }
    } else {
      wrap(scene.summary, 365, 286, 825, 28, 9);
      c.fillStyle = "#74796a";
      c.font = "16px system-ui, sans-serif";
      c.fillText("Original report narration · Illustrated briefing", 365, 632);
    }
    c.fillStyle = "#c7cabe";
    c.fillRect(0, 708, 1280, 12);
    c.fillStyle = "#d9ab4b";
    c.fillRect(0, 708, 1280 * Math.min(1, time / project.duration), 12);
  };
}
