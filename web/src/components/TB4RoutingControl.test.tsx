import { useState } from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { authenticatedFetch } from "@/lib/identity";
import { TB4RoutingControl, type TB4Policy } from "./TB4RoutingControl";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));
const fetchMock = vi.mocked(authenticatedFetch);

function Fixture({ available = true }: { available?: boolean }) {
  const [value, setValue] = useState<TB4Policy | null>(null);
  return <TB4RoutingControl available={available} value={value} onChange={setValue} />;
}
function mount(available = true) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <Fixture available={available} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  fetchMock.mockReset();
  fetchMock.mockImplementation(
    async (url) =>
      new Response(
        JSON.stringify(
          String(url).endsWith("/preview")
            ? {
                eligible_models: 0,
                eligible_routes: 0,
                catalogue_routes: 20,
                eligible: [],
                exclusions: { "Below TB4 floor": 20 },
                benchmark: { retrieved_at: "2026-10-22", source_url: "https://example.com" },
              }
            : { enabled: true },
        ),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
  );
});

describe("TB4 routing controls", () => {
  it("sends the floor and both price limits and explains an empty pool", async () => {
    mount();
    fireEvent.click(await screen.findByRole("checkbox", { name: /Choose by TB4/ }));
    fireEvent.change(screen.getByRole("slider"), { target: { value: "40" } });
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "metered" } });
    fireEvent.change(screen.getByRole("spinbutton", { name: /Maximum input/ }), {
      target: { value: "0.2" },
    });
    fireEvent.change(screen.getByRole("spinbutton", { name: /Maximum output/ }), {
      target: { value: "0.8" },
    });
    await screen.findByText(/No model meets these limits/);
    await waitFor(() => {
      const calls = fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/preview"));
      expect(JSON.parse(String(calls.at(-1)?.[1]?.body))).toMatchObject({
        minimum_score: 40,
        cost_mode: "metered",
        max_input_usd_per_million: 0.2,
        max_output_usd_per_million: 0.8,
      });
    });
    fireEvent.click(screen.getByRole("checkbox"));
    expect(screen.queryByRole("slider")).toBeNull();
  });
  it("hides the policy when the selected harness is unavailable", async () => {
    mount(false);
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(screen.queryByRole("checkbox")).toBeNull();
  });
});
