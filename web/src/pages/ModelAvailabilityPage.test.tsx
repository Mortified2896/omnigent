import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { authenticatedFetch } from "@/lib/identity";
import { ModelAvailabilityPage } from "./ModelAvailabilityPage";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));
const fetchMock = vi.mocked(authenticatedFetch);
const base = {
  provider: "provider",
  effort: "high",
  source_url: null,
  cost_class: "free",
  context_tokens: 128000,
  account_ready: true,
  enabled_accounts: 1,
  input_usd_per_million: null,
  output_usd_per_million: null,
  estimated_task_usd: 0,
  last_test: null,
};
let running = false;
const data = () => ({
  rows: [
    { ...base, name: "Model A", route: "provider/model-a", score: 50, status: "available" },
    { ...base, name: "Model B", route: "provider/model-b", score: 20, status: "failed" },
    { ...base, name: "Unknown score", route: "provider/unknown", score: null, status: "untested" },
  ],
  providers: [
    {
      provider: "provider",
      enabled_accounts: 1,
      disabled_accounts: 0,
      account_ready: true,
      limits: [],
      quota: [],
      policy_limits: [],
      usage: [],
    },
  ],
  catalogue_at: 100,
  account_evidence_at: 100,
  benchmark: { source_url: "https://example.com", retrieved_at: "2026-10-22" },
  run: running
    ? {
        id: "run",
        status: "running",
        total: 3,
        completed: 1,
        available: 1,
        current: ["provider/model-b"],
        skipped: {},
        estimated_probe_usd: 0,
      }
    : null,
});

beforeEach(() => {
  running = false;
  fetchMock.mockReset();
  fetchMock.mockImplementation(async (url) => {
    const path = String(url);
    if (path.endsWith("/test")) running = true;
    if (path.endsWith("/cancel")) running = false;
    return new Response(
      JSON.stringify(
        path.endsWith("/plan")
          ? { targets: 3, skipped: { "Cost outside scope or unknown": 2 }, estimated_probe_usd: 0 }
          : data(),
      ),
      { status: 200 },
    );
  });
});
function mount() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <MemoryRouter>
      <QueryClientProvider client={client}>
        <ModelAvailabilityPage />
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

describe("Model availability", () => {
  it("shows usable routes and keeps detailed scan controls in OmniRoute", async () => {
    mount();
    await screen.findByText("Model A");
    expect(screen.getAllByTestId("availability-row")).toHaveLength(1);
    expect(screen.queryByText("Model B")).toBeNull();
    expect(screen.getByRole("link", { name: /Tests, providers and quotas/ })).toHaveAttribute(
      "href",
      "https://sg-apps.taile0361b.ts.net:9443/availability",
    );
    expect(screen.getByText("Harness: no observed client turn")).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("Show unverified and failed routes"));
    expect(screen.getAllByTestId("availability-row")).toHaveLength(3);
    fireEvent.change(screen.getByLabelText("Minimum TB4 score"), { target: { value: "30" } });
    expect(screen.getAllByTestId("availability-row")).toHaveLength(1);
  });
  it("starts the shared included scan and shows its progress", async () => {
    mount();
    const button = await screen.findByRole("button", { name: "Refresh availability" });
    await waitFor(() => expect(button).toBeEnabled());
    fireEvent.click(button);
    await screen.findByText(/OmniRoute is testing/);
    const call = fetchMock.mock.calls.find(([url]) => String(url).endsWith("/test"));
    expect(JSON.parse(String(call?.[1]?.body))).toEqual({
      cost_mode: "included",
      minimum_score: 0,
      include_unscored: true,
      retest: true,
    });
    expect(button).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Stop testing" })).toBeNull();
  });
  it("surfaces gateway failures instead of an empty successful list", async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ error: { message: "Gateway unavailable" } }), { status: 409 }),
    );
    mount();
    expect(await screen.findByRole("alert")).toHaveTextContent("Gateway unavailable");
    expect(screen.queryByText("Verified with tools")).toBeNull();
  });
});
