import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { FilePathAwareMessageResponse } from "./ChatMarkdown";

vi.mock("@/components/WorkspaceImage", () => ({
  WorkspaceImage: ({ path, alt }: { path: string; alt: string }) => (
    <img src="blob:session-image" alt={alt} data-path={path} />
  ),
}));
afterEach(cleanup);

describe("chat image rendering", () => {
  it.each([
    [
      "![Overview](/Users/Jo/Documents/Alibi-Review-2026-10-13/00-overview.png)",
      "/Users/Jo/Documents/Alibi-Review-2026-10-13/00-overview.png",
    ],
    ["![Overview](screenshots/overview.png)", "screenshots/overview.png"],
    [
      "![Overview](file:///Users/Jo/Documents/my%20screen.png)",
      "/Users/Jo/Documents/my screen.png",
    ],
  ])("hands a local image to the session-authorized renderer", async (markdown, path) => {
    render(<FilePathAwareMessageResponse>{markdown}</FilePathAwareMessageResponse>);
    expect(await screen.findByRole("img", { name: "Overview" })).toHaveAttribute("data-path", path);
    expect(screen.queryByText(/Image blocked/)).toBeNull();
  });
  it.each([
    "https://attacker.example/leak.png",
    "//attacker.example/leak.png",
    "../private.png",
    "/tmp/%2e%2e/private.png",
  ])("keeps unsafe images blocked: %s", async (src) => {
    render(<FilePathAwareMessageResponse>{`![Leak](${src})`}</FilePathAwareMessageResponse>);
    expect(await screen.findByText("[Image blocked: Leak]")).toBeInTheDocument();
    expect(screen.queryByRole("img", { name: "Leak" })).toBeNull();
  });
  it("does not trust an authored marker attribute", async () => {
    render(
      <FilePathAwareMessageResponse>
        {'<span data-omnigent-image="/tmp/private.png" data-omnigent-image-alt="Spoof">Text</span>'}
      </FilePathAwareMessageResponse>,
    );
    expect(await screen.findByText("Text")).toBeInTheDocument();
    expect(screen.queryByRole("img", { name: "Spoof" })).toBeNull();
  });
});
