import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { O3SessionReview } from "./O3SessionReview";

const query = vi.hoisted(() => vi.fn());
const serverInfo = vi.hoisted(() => ({
  current: {
    o3_routing_review_enabled: true,
    features: { model_advisor: true },
  } as Record<string, unknown>,
}));

vi.mock("@tanstack/react-query", () => ({
  useQuery: query,
}));

vi.mock("@/lib/CapabilitiesContext", () => ({
  useServerInfo: () => serverInfo.current,
}));

beforeEach(() => {
  query.mockReset();
  query.mockReturnValue({
    isError: true,
    data: undefined,
    refetch: vi.fn(),
  });
  serverInfo.current = {
    o3_routing_review_enabled: true,
    features: { model_advisor: true },
  };
});

afterEach(cleanup);

it("does not resurrect retired O3 review when Model Advisor is enabled", () => {
  render(<O3SessionReview sessionId="conv_new" />);

  expect(query).toHaveBeenCalledWith(
    expect.objectContaining({
      enabled: false,
    }),
  );
  expect(screen.queryByRole("button", { name: "Retry loading O3 review" })).toBeNull();
});

it("keeps the legacy review available only on an O3-only deployment", () => {
  serverInfo.current = {
    o3_routing_review_enabled: true,
    features: {},
  };

  render(<O3SessionReview sessionId="conv_legacy" />);

  expect(query).toHaveBeenCalledWith(
    expect.objectContaining({
      enabled: true,
    }),
  );
  expect(screen.getByRole("button", { name: "Retry loading O3 review" })).toBeInTheDocument();
});
