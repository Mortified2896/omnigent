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
  it("shows the persisted spending scope when reopening a running batch", async () => {
    running = true;
    fetchMock.mockImplementation(
      async () =>
        new Response(
          JSON.stringify({
            ...data(),
            run: {
              ...data().run,
              policy: {
                cost_mode: "metered",
                minimum_score: 40,
                include_unscored: false,
                retest: true,
                max_input_usd_per_million: 2,
                max_output_usd_per_million: 7,
                estimated_budget_usd: 0.5,
              },
            },
          }),
          { status: 200 },
        ),
    );
    mount();
    await screen.findByRole("progressbar");
    expect(screen.getByLabelText("Test spending")).toHaveValue("metered");
    expect(screen.getByLabelText("Test spending")).toBeDisabled();
    expect(screen.getByLabelText("Estimated batch budget ($)")).toHaveValue(0.5);
  });
  it("shows ranked unknown and failed routes, and filters available routes", async () => {
    mount();
    await screen.findByText("Model A");
    expect(screen.getAllByTestId("availability-row").map((r) => r.textContent)).toEqual([
      expect.stringContaining("Model A"),
      expect.stringContaining("Model B"),
      expect.stringContaining("Unknown score"),
    ]);
    fireEvent.change(screen.getByLabelText("Filter availability"), {
      target: { value: "available" },
    });
    expect(screen.getAllByTestId("availability-row")).toHaveLength(1);
    expect(screen.queryByText("Model B")).toBeNull();
    expect(screen.getByText("Remaining quota: not reported")).toBeInTheDocument();
  });
  it("starts a scoped batch, shows progress, and stops it", async () => {
    mount();
    const button = await screen.findByRole("button", { name: /Test all models in scope/ });
    await waitFor(() => expect(button).toBeEnabled());
    fireEvent.change(screen.getByLabelText("Test spending"), { target: { value: "free" } });
    await waitFor(() => expect(button).toBeEnabled());
    fireEvent.click(button);
    await screen.findByRole("progressbar");
    const call = fetchMock.mock.calls.find(([url]) => String(url).endsWith("/test"));
    expect(JSON.parse(String(call?.[1]?.body))).toMatchObject({
      cost_mode: "free",
      include_unscored: true,
      minimum_score: 0,
    });
    expect(button).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Stop testing" }));
    await waitFor(() => expect(screen.queryByRole("progressbar")).toBeNull());
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
