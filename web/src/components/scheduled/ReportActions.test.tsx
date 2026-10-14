import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ReportActions, ReportActionsContext } from "./ReportActions";

afterEach(cleanup);
const prepare = vi.fn((_request: string) => true);
const context = { sessionId: "report", prepare };
beforeEach(() => {
  prepare.mockReset();
  prepare.mockReturnValue(true);
});
const actions = [
  { title: "OmniRoute update", target: "Mac OmniRoute", why: "A useful reliability fix." },
  { title: "Omnigent update", target: "Mac Omnigent", why: "A relevant recovery fix." },
];

it("requires a deliberate choice, preserves selections and prepares rather than executes", () => {
  render(
    <ReportActionsContext.Provider value={context}>
      <ReportActions actions={actions} responseId="response" />
    </ReportActionsContext.Provider>,
  );
  const button = screen.getByRole("button", { name: "Add choices to chat" });
  expect(button).toBeDisabled();
  expect(prepare).not.toHaveBeenCalled();
  fireEvent.click(
    within(screen.getByRole("group", { name: "OmniRoute update" })).getByRole("radio", {
      name: /Try it safely/,
    }),
  );
  fireEvent.click(
    within(screen.getByRole("group", { name: "Omnigent update" })).getByRole("radio", {
      name: /Not now/,
    }),
  );
  expect(prepare).not.toHaveBeenCalled();
  fireEvent.click(button);
  expect(prepare).toHaveBeenCalledTimes(1);
  expect(prepare.mock.calls[0]![0]).toContain("Try it safely:");
  expect(prepare.mock.calls[0]![0]).toContain("Not now:");
  expect(screen.getByRole("status")).toHaveTextContent("Edit them or press Send");
});

it("reports when a draft could not be prepared instead of claiming it ran", () => {
  prepare.mockReturnValue(false);
  render(
    <ReportActionsContext.Provider value={context}>
      <ReportActions actions={actions} responseId="response" />
    </ReportActionsContext.Provider>,
  );
  fireEvent.click(screen.getAllByRole("radio", { name: /Review first/ })[0]!);
  fireEvent.click(screen.getByRole("button", { name: "Add choices to chat" }));
  expect(screen.getByRole("status")).toHaveTextContent("Could not add choices");
});

it("does not offer execution controls outside a scheduled-report surface", () => {
  render(<ReportActions actions={actions} responseId="response" />);
  expect(screen.queryByRole("region")).not.toBeInTheDocument();
  expect(screen.queryByRole("button")).not.toBeInTheDocument();
});
