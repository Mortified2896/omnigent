import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { PromptTagSuggestions } from "./PromptTagSuggestions";

afterEach(cleanup);

it("reviews individual suggestions then applies the accepted tags", async () => {
  const apply = vi.fn().mockResolvedValue(undefined);
  render(<PromptTagSuggestions original={[]} proposed={["UI", "Testing"]} onApply={apply} />);
  fireEvent.click(screen.getByRole("button", { name: "Review add tag Testing" }));
  fireEvent.click(screen.getByRole("button", { name: "Reject add tag Testing" }));
  expect(apply).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Accept remaining & apply" }));
  await waitFor(() => expect(apply).toHaveBeenCalledWith(["UI"]));
  expect(screen.queryByLabelText("Proposed feedback rating")).not.toBeInTheDocument();
});
it("rejects the entire proposal while preserving existing labels", async () => {
  const apply = vi.fn().mockResolvedValue(undefined);
  render(<PromptTagSuggestions original={["Research"]} proposed={["UI"]} onApply={apply} />);
  fireEvent.click(screen.getByRole("button", { name: "Reject suggested prompt tags" }));
  await waitFor(() => expect(apply).toHaveBeenCalledWith(["Research"]));
});
it("lets the user edit tags and retains the proposal after a save error", async () => {
  const apply = vi.fn().mockRejectedValue(new Error("Reload to review newer tags"));
  render(<PromptTagSuggestions original={[]} proposed={["UI"]} onApply={apply} />);
  fireEvent.click(screen.getByRole("button", { name: "Edit" }));
  fireEvent.click(screen.getByRole("button", { name: "Remove task tag UI" }));
  fireEvent.click(screen.getByRole("button", { name: "Task tags" }));
  fireEvent.click(screen.getByRole("button", { name: "Research" }));
  fireEvent.click(screen.getByRole("button", { name: "Task tags" }));
  fireEvent.click(screen.getByRole("button", { name: "Accept tags" }));
  await waitFor(() => expect(apply).toHaveBeenCalledWith(["Research"]));
  expect(await screen.findByText("Reload to review newer tags")).toBeInTheDocument();
});
