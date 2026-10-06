import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { CodexSubscriptionUsage } from "./CodexSubscriptionUsage";

const fetchUsage = vi.hoisted(() => vi.fn());
vi.mock("@/lib/identity", () => ({
  authenticatedFetch: fetchUsage,
  getCurrentUserId: () => "owner",
}));

afterEach(() => {
  cleanup();
  fetchUsage.mockReset();
});

function renderUsage() {
  return render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <TooltipProvider>
        <CodexSubscriptionUsage hostId="host-1" />
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

it("reads the selected host's real subscription allowance", async () => {
  fetchUsage.mockResolvedValue(Response.json({ remaining_percent: 65, windows: [] }));
  renderUsage();
  expect(await screen.findByText("65% left")).toBeInTheDocument();
  expect(fetchUsage).toHaveBeenCalledWith("/v1/hosts/host-1/codex-rate-limits");
});

it("shows unknown usage when the host cannot report it", async () => {
  fetchUsage.mockResolvedValue(new Response(null, { status: 502 }));
  renderUsage();
  expect(await screen.findByText("Usage unavailable")).toBeInTheDocument();
});

it("does not query an account when no host is selected", () => {
  expect(render(<CodexSubscriptionUsage hostId={null} />).container).toBeEmptyDOMElement();
  expect(fetchUsage).not.toHaveBeenCalled();
});
