import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { AdvisorPromptPreview } from "./AdvisorPromptPreview";

it("shows the server template and refreshes a preview without retaining stale inputs", async () => {
  const onPreview = vi.fn().mockResolvedValue("Exact server prompt for message one");
  const { rerender } = render(
    <AdvisorPromptPreview
      template="Server template"
      identity="one"
      onPreview={onPreview}
      disabled={false}
    />,
  );
  fireEvent.click(screen.getByText("Prompt sent to the recommender"));
  expect(screen.getByLabelText("Recommender prompt")).toHaveTextContent("Server template");
  expect(onPreview).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Preview current request" }));
  await waitFor(() =>
    expect(screen.getByLabelText("Recommender prompt")).toHaveTextContent(
      "Exact server prompt for message one",
    ),
  );
  rerender(
    <AdvisorPromptPreview
      template="Server template"
      identity="two"
      onPreview={onPreview}
      disabled={false}
    />,
  );
  expect(screen.getByLabelText("Recommender prompt")).toHaveTextContent("Server template");
});
