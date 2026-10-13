import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { ToolCard } from "./ToolCard";
import { BlockRenderer } from "./BlockRenderer";
import type { RenderItem } from "@/lib/renderItems";

vi.mock("@/components/WorkspaceImage", () => ({
  WorkspaceImage: ({ alt }: { alt: string }) => (
    <span role="img" aria-label={alt}>
      Visible image
    </span>
  ),
}));
afterEach(cleanup);
describe("native image outputs", () => {
  it("shows an image without opening the tool details", () => {
    render(
      <TooltipProvider>
        <ToolCard
          name="view_image"
          arguments={{ path: "/tmp/review.png" }}
          output="/tmp/review.png"
          state="output-available"
        />
      </TooltipProvider>,
    );
    expect(screen.getByRole("img", { name: "review.png" })).toBeVisible();
    expect(screen.queryByText("Parameters")).toBeNull();
  });
  it("keeps native images visible after a completed turn folds its work", () => {
    const items = [
      {
        kind: "tool",
        itemId: "read",
        execution: {
          callId: "read",
          name: "shell",
          arguments: { command: "pwd" },
          argsSummary: "pwd",
        },
        output: "ok",
        state: "output-available",
      },
      {
        kind: "tool",
        itemId: "image",
        execution: {
          callId: "image",
          name: "view_image",
          arguments: { path: "/tmp/review.png" },
          argsSummary: "/tmp/review.png",
        },
        output: "/tmp/review.png",
        state: "output-available",
      },
      { kind: "text", itemId: "answer", text: "Here is the review." },
    ] as RenderItem[];
    render(
      <TooltipProvider>
        <BlockRenderer
          items={items}
          sessionStatus="idle"
          turnLifecycle="completed"
          workedForS={5}
        />
      </TooltipProvider>,
    );
    expect(screen.getByRole("img", { name: "review.png" })).toBeVisible();
  });
});
