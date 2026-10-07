import { cleanup, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, expect, it, vi } from "vitest";
import { ResponseRouteBadge } from "./ResponseRouteBadge";

const api = vi.hoisted(() => vi.fn());
vi.mock("@/lib/identity", () => ({ authenticatedFetch: api, getCurrentUserId: () => "owner" }));
afterEach(cleanup);

it.each([
  ["omniroute", "OmniRoute"],
  ["direct", "Direct"],
  ["direct_fallback", "Direct fallback"],
])("shows the recorded %s route without exposing model identity", async (route, label) => {
  api.mockResolvedValue({ ok: true, json: async () => ({ route }) });
  render(
    <QueryClientProvider client={new QueryClient()}>
      <ResponseRouteBadge sessionId="chat" responseId="answer" running={false} />
    </QueryClientProvider>,
  );
  expect(await screen.findByTestId("response-route")).toHaveTextContent(label);
  expect(api).toHaveBeenCalledWith("/v1/sessions/chat/response-route/answer");
  expect(screen.queryByText(/GPT/)).not.toBeInTheDocument();
});
