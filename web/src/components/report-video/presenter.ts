// Reused from Finance-R-Simulations/apps/article_lab_web/experiments/animated-explainer/draw.ts.
// Deterministic vector presenter; no generated pixels or video-model calls.
const INK = "#171817",
  PAPER = "#f5f3ee";
const ease = (x: number) => 1 - Math.pow(1 - Math.max(0, Math.min(1, x)), 3);
function ellipse(
  c: CanvasRenderingContext2D,
  x: number,
  y: number,
  rx: number,
  ry: number,
  fill: string,
) {
  c.beginPath();
  c.ellipse(x, y, rx, ry, 0, 0, Math.PI * 2);
  c.fillStyle = fill;
  c.fill();
}
interface Pose {
  x: number;
  y: number;
  scale?: number;
  mode?: "idle" | "walk" | "point" | "shrug";
  expression?: "neutral" | "think" | "happy" | "curious";
  dark?: boolean;
  entrance?: number;
  gestureFrom?: "idle" | "walk" | "point" | "shrug";
  gestureMix?: number;
  moving?: boolean;
}
/** Reusable, deterministic vector presenter. No sprite sheet or generated pixels. */
export function presenter(c: CanvasRenderingContext2D, t: number, pose: Pose, speaking = false) {
  c.save();
  const entrance = pose.entrance === undefined ? 1 : ease((t - pose.entrance) / 1.6);
  const walking = entrance < 1 || pose.mode === "walk" || pose.moving;
  const stride = Math.sin(t * 10) * (walking ? 1 : 0);
  c.translate(pose.x - 160 * (1 - entrance), pose.y + Math.sin(t * 1.8) * 1.5);
  c.scale(pose.scale ?? 1, pose.scale ?? 1);
  // One character keeps the same wardrobe and face across lighting changes.
  // A restrained keyline keeps the dark suit readable against the dark stage.
  const ink = INK,
    face = PAPER;
  ellipse(c, 0, 190, 45, 6, pose.dark ? "#ffffff18" : "#00000012");
  c.strokeStyle = ink;
  c.lineWidth = 12;
  c.lineCap = "round";
  for (const sign of [-1, 1]) {
    const footX = sign * 14 + sign * stride * 14;
    c.beginPath();
    c.moveTo(sign * 12, 134);
    c.lineTo(sign * 16 + sign * stride * 8, 158);
    c.lineTo(footX, 181);
    if (pose.dark) {
      c.strokeStyle = PAPER;
      c.lineWidth = 15;
      c.stroke();
      c.strokeStyle = INK;
      c.lineWidth = 12;
    }
    c.stroke();
    c.lineWidth = 7;
    c.beginPath();
    c.moveTo(footX - 3, 186);
    c.lineTo(footX + 10, 186);
    if (pose.dark) {
      c.strokeStyle = PAPER;
      c.lineWidth = 10;
      c.stroke();
      c.strokeStyle = INK;
      c.lineWidth = 7;
    }
    c.stroke();
    c.lineWidth = 12;
  }
  c.beginPath();
  c.moveTo(-28, 72);
  c.quadraticCurveTo(-39, 88, -28, 144);
  c.lineTo(28, 144);
  c.quadraticCurveTo(39, 88, 28, 72);
  c.quadraticCurveTo(0, 59, -28, 72);
  c.fillStyle = ink;
  c.fill();
  if (pose.dark) {
    c.strokeStyle = PAPER;
    c.lineWidth = 1.8;
    c.stroke();
  }
  c.fillStyle = face;
  c.beginPath();
  c.moveTo(-10, 68);
  c.lineTo(0, 90);
  c.lineTo(10, 68);
  c.closePath();
  c.fill();
  const mode = pose.mode ?? "idle";
  for (const sign of [-1, 1]) {
    const arm = (m: Pose["mode"]) => {
      const point = m === "point" && sign === -1,
        shrug = m === "shrug";
      return {
        elbowX: point ? -74 : sign * (43 + (walking ? stride * 6 : 0)),
        elbowY: point ? 85 : shrug ? 84 : 114,
        handX: point
          ? -120
          : sign * (shrug ? 62 : 43 + (walking ? -stride * 14 : Math.sin(t * 2) * 2)),
        handY: point ? 70 + Math.sin(t * 2) * 2 : shrug ? 70 : 148,
      };
    };
    const target = arm(mode),
      previous = arm(pose.gestureFrom ?? mode),
      mix = pose.gestureMix ?? 1;
    const lerp = (a: number, b: number) => a + (b - a) * mix;
    const elbowX = lerp(previous.elbowX, target.elbowX),
      elbowY = lerp(previous.elbowY, target.elbowY),
      handX = lerp(previous.handX, target.handX),
      handY = lerp(previous.handY, target.handY);
    const point = mode === "point" && sign === -1 && mix > 0.7;
    c.strokeStyle = ink;
    c.lineWidth = 11;
    c.beginPath();
    c.moveTo(sign * 28, 80);
    c.lineTo(elbowX, elbowY);
    c.lineTo(handX, handY);
    if (pose.dark) {
      c.strokeStyle = PAPER;
      c.lineWidth = 14;
      c.stroke();
      c.strokeStyle = INK;
      c.lineWidth = 11;
    }
    c.stroke();
    ellipse(c, handX, handY, 8, 8, face);
    c.strokeStyle = ink;
    c.lineWidth = 2;
    c.beginPath();
    c.arc(handX, handY, 8, 0, Math.PI * 2);
    c.stroke();
    if (point) {
      c.beginPath();
      c.moveTo(handX - 5, handY - 5);
      c.lineTo(handX - 22, handY - 8);
      c.stroke();
    }
  }
  c.save();
  c.translate(0, 28);
  c.rotate(Math.sin(t * 1.3) * 0.025 + (pose.expression === "think" ? -0.08 : 0));
  ellipse(c, 0, 0, 33, 36, face);
  c.strokeStyle = ink;
  c.lineWidth = 2.6;
  c.beginPath();
  c.ellipse(0, 0, 33, 36, 0, 0, Math.PI * 2);
  c.stroke();
  // Asymmetric ink hair, glasses, expressive brows and a narration-gated mouth.
  c.fillStyle = ink;
  c.beginPath();
  c.moveTo(-32, -6);
  c.bezierCurveTo(-35, -41, 28, -43, 33, -10);
  c.quadraticCurveTo(12, -29, -8, -18);
  c.quadraticCurveTo(-19, -6, -32, -6);
  c.fill();
  const phase = t % 4.7;
  if (pose.dark) {
    c.strokeStyle = PAPER;
    c.lineWidth = 1.8;
    c.beginPath();
    c.ellipse(0, 0, 33, 36, 0, 0, Math.PI * 2);
    c.stroke();
  }
  const blink = phase > 4.43 && phase < 4.62 ? 0.15 : 1;
  for (const sign of [-1, 1]) {
    c.strokeStyle = ink;
    c.lineWidth = 1.8;
    c.beginPath();
    c.roundRect(sign < 0 ? -26 : 3, -6, 23, 17, 5);
    c.stroke();
    ellipse(c, sign * 14 - 2, 2, 2.1, 3.2 * blink, ink);
    c.beginPath();
    const raise = pose.expression === "curious" && sign === 1 ? -5 : 0;
    c.moveTo(sign < 0 ? -23 : 6, -12 + raise);
    c.lineTo(sign < 0 ? -7 : 23, -14 + raise);
    c.stroke();
  }
  c.beginPath();
  c.moveTo(-3, 0);
  c.lineTo(3, 0);
  c.stroke();
  if (speaking) ellipse(c, 0, 20, 5, 2 + Math.abs(Math.sin(t * 13)) * 4, ink);
  else {
    c.beginPath();
    c.moveTo(-6, 19);
    c.quadraticCurveTo(0, pose.expression === "happy" ? 27 : 22, 6, 19);
    c.stroke();
  }
  c.restore();
  c.restore();
}
