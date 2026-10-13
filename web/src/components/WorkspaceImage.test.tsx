import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { WorkspaceImage } from "./WorkspaceImage";
import { useFileContent } from "@/hooks/useFileContent";

vi.mock("@/hooks/useFileContent", () => ({
  useFileContent: vi.fn(),
  fileContentToBlob: vi.fn(() => new Blob(["image bytes"], { type: "image/png" })),
}));
vi.mock("@/shell/FileViewerContext", () => ({
  useFileViewerConversationId: () => "conv_review",
  useWorkspacePaths: () => ({ root: "/workspace", home: "/Users/Jo" }),
}));
vi.mock("./ImageLightbox", () => ({
  ZoomableImage: (props: React.ComponentProps<"img">) => <img {...props} />,
}));
const useContent = vi.mocked(useFileContent);
const createUrl = vi.fn(() => "blob:authorized-image");
const revokeUrl = vi.fn();
beforeEach(() => {
  createUrl.mockClear();
  revokeUrl.mockClear();
  useContent.mockReset();
  vi.stubGlobal(
    "URL",
    Object.assign(URL, { createObjectURL: createUrl, revokeObjectURL: revokeUrl }),
  );
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});
function content(options: Record<string, unknown> = {}) {
  useContent.mockReturnValue({
    data: {
      encoding: "base64",
      content: "aW1hZ2U=",
      content_type: "image/png",
      bytes: 5,
      truncated: false,
      ...options,
    },
    isError: false,
  } as ReturnType<typeof useFileContent>);
}
describe("session-authorized workspace images", () => {
  it("reads from the conversation host and releases the object URL", async () => {
    content();
    const { unmount } = render(
      <WorkspaceImage path="/Users/Jo/Documents/review.png" alt="Review" />,
    );
    expect(await screen.findByRole("img", { name: "Review" })).toHaveAttribute(
      "src",
      "blob:authorized-image",
    );
    expect(useContent).toHaveBeenCalledWith("conv_review", "/Users/Jo/Documents/review.png");
    unmount();
    expect(revokeUrl).toHaveBeenCalledWith("blob:authorized-image");
  });
  it("collapses a workspace absolute path to its existing relative API form", async () => {
    content();
    render(<WorkspaceImage path="/workspace/screens/review.png" alt="Review" />);
    await screen.findByRole("img", { name: "Review" });
    expect(useContent).toHaveBeenCalledWith("conv_review", "screens/review.png");
  });
  it.each([
    { truncated: true },
    { content_type: "text/plain" },
    { content_type: "image/svg+xml" },
    { bytes: 11 * 1024 * 1024 },
  ])("does not expose unsupported or incomplete payloads", async (options) => {
    content(options);
    render(<WorkspaceImage path="/workspace/review.png" alt="Review" />);
    await waitFor(() => expect(screen.getByText("Image unavailable: Review")).toBeInTheDocument());
    expect(createUrl).not.toHaveBeenCalled();
  });
  it("does not request external URLs", () => {
    render(<WorkspaceImage path="https://attacker.example/private.png" alt="Review" />);
    expect(useContent).not.toHaveBeenCalled();
  });
});
