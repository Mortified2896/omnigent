import { describe, expect, it } from "vitest";
import { nativeToolImagePath, workspaceImagePath } from "./workspaceImages";

describe("workspace image paths", () => {
  it.each([
    "/Users/Jo/Documents/Review/shot.png",
    "screenshots/mobile.webp",
    "~/Review/my%20screen.jpg",
  ])("accepts a local raster path: %s", (value) => {
    expect(workspaceImagePath(value)).toBe(value.replace("%20", " "));
  });
  it.each([
    "https://example.com/leak.png",
    "//example.com/leak.png",
    "data:image/png;base64,AAAA",
    "file:///tmp/shot.png",
    "javascript:alert(1)",
    "../private.png",
    "/tmp/../private.png",
    "screenshots/%2e%2e/private.png",
    "screenshots/%2Fprivate.png",
    "screenshots/%5cprivate.png",
    "screenshots/foo.png?token=secret",
    "screenshots/foo.png#x",
    "/tmp/password.txt",
    "/tmp/active.svg",
    "/tmp//shot.png",
    "/tmp/%00.png",
  ])("rejects unsafe or non-raster sources: %s", (value) => {
    expect(workspaceImagePath(value)).toBeNull();
  });
});

describe("native image metadata", () => {
  it("recovers the exact persisted imageView shape", () => {
    expect(nativeToolImagePath("view_image", { path: "/tmp/shot.png" }, "/tmp/shot.png")).toBe(
      "/tmp/shot.png",
    );
  });
  it("recovers a completed imageGeneration save path", () => {
    expect(
      nativeToolImagePath("generate_image", {}, "status: completed\nsaved to /tmp/generated.png"),
    ).toBe("/tmp/generated.png");
  });
  it("does not invent previews for arbitrary prose, pending tools, or failed generation", () => {
    expect(
      nativeToolImagePath("shell", {}, "status: completed\nsaved to /tmp/shot.png"),
    ).toBeNull();
    expect(nativeToolImagePath("view_image", { path: "/tmp/shot.png" }, null)).toBeNull();
    expect(
      nativeToolImagePath("generate_image", {}, "status: failed\nsaved to /tmp/shot.png"),
    ).toBeNull();
  });
});
