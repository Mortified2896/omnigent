import { describe, expect, it } from "vitest";
import { sceneAt, validateScenes, type Scene } from "./model";

const scenes: Scene[] = [
  { start: 0, title: "Opening", summary: "", image_query: "", image_id: null },
  { start: 2.13, title: "Release", summary: "", image_query: "", image_id: null },
];
describe("report scene times", () => {
  it("uses the media clock across exact scene boundaries and backward seeking", () => {
    expect(sceneAt(scenes, 2.12)).toBe(0);
    expect(sceneAt(scenes, 2.13)).toBe(1);
    expect(sceneAt(scenes, 6)).toBe(1);
    expect(sceneAt(scenes, 0.5)).toBe(0);
  });
  it("accepts fractional times and rejects unordered, missing, and out-of-range scenes", () => {
    expect(() => validateScenes(scenes, 8.13)).not.toThrow();
    expect(() => validateScenes([scenes[1]], 8.13)).toThrow("first scene");
    for (const start of [0, -1, 9, NaN])
      expect(() => validateScenes([scenes[0], { ...scenes[1], start }], 8.13)).toThrow(
        "Scene times",
      );
  });
});
