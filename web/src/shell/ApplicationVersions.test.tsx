import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { copyText } from "@/lib/clipboard";
import { showToast } from "@/components/ui/toast";
import { ApplicationVersions } from "./ApplicationVersions";

vi.mock("@/lib/clipboard", () => ({ copyText: vi.fn() }));
vi.mock("@/components/ui/toast", () => ({ showToast: vi.fn() }));
afterEach(cleanup);
beforeEach(() => vi.resetAllMocks());

it("shows and copies the official base and the private revision together", async () => {
  render(<ApplicationVersions versions={{ official: "0.18.0", private: "custom01" }} />);
  const button = screen.getByRole("button", { name: "Copy application versions" });
  expect(button).toHaveTextContent("Official base v0.18.0");
  expect(button).toHaveTextContent("Private custom01");
  fireEvent.click(button);
  expect(copyText).toHaveBeenCalledWith("Omnigent\nOfficial base v0.18.0\nPrivate custom01");
  await waitFor(() => expect(showToast).toHaveBeenCalledWith("Versions copied"));
});

it("updates both labels when the connected server changes", () => {
  const view = render(
    <ApplicationVersions versions={{ official: "0.17.0", private: "oldbuild" }} />,
  );
  view.rerender(<ApplicationVersions versions={{ official: "0.18.0", private: "newbuild" }} />);
  expect(screen.queryByText("Official base v0.17.0")).not.toBeInTheDocument();
  expect(screen.getByText("Official base v0.18.0")).toBeInTheDocument();
  expect(screen.getByText("Private newbuild")).toBeInTheDocument();
});

it("reports a clipboard failure without claiming success", async () => {
  vi.mocked(copyText).mockRejectedValue(new Error("Clipboard unavailable"));
  render(<ApplicationVersions versions={{ official: "0.18.0", private: "custom01" }} />);
  fireEvent.click(screen.getByRole("button", { name: "Copy application versions" }));
  await waitFor(() =>
    expect(showToast).toHaveBeenCalledWith("Couldn’t copy versions. Please try again."),
  );
  expect(showToast).not.toHaveBeenCalledWith("Versions copied");
});
