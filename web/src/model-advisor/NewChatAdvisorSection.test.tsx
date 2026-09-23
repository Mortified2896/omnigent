import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { assert, afterEach, beforeEach, expect, it, vi } from "vitest";
import { NewChatAdvisorSection } from "./NewChatAdvisorSection";

const api = vi.hoisted(() => vi.fn());
vi.mock("@/lib/identity", () => ({ authenticatedFetch: api, getCurrentUserId: () => "local" }));

const OPTION_A = {
  candidate_id: "legacy-aaa",
  model_id: "gpt-5.5",
  display_name: "GPT-5.5",
  lane_id: "codex-direct" as const,
  reasoning_effort: "medium",
  access_class: "chatgpt_plan",
  is_default_effort: true,
};
const OPTION_B = {
  candidate_id: "legacy-bbb",
  model_id: "glm-5.3",
  display_name: "GLM-5.3",
  lane_id: "glm-direct" as const,
  reasoning_effort: "high",
  access_class: "glm_plan",
  is_default_effort: true,
};

const LOGICAL_A = {
  choice_id: "choice-openai-medium",
  provider: "openai" as const,
  model_id: "gpt-5.5",
  display_name: "GPT-5.5",
  reasoning_effort: "medium",
  model_ids: ["gpt-5.5", "codex/gpt-5.5"],
  access_lanes: ["codex-direct", "omniroute"],
  available: true,
};
const LOGICAL_B = {
  choice_id: "choice-glm-high",
  provider: "glm" as const,
  model_id: "glm-5.3",
  display_name: "GLM-5.3",
  reasoning_effort: "high",
  model_ids: ["glm-5.3", "glm/glm-5.3"],
  access_lanes: ["glm-direct", "omniroute"],
  available: true,
};

const V2_PREFERENCES = {
  schema_version: 2,
  enabled: true,
  providers: {
    openai: {
      enabled: true,
      collapsed: false,
      selected_choice_ids: [LOGICAL_A.choice_id],
      transport_preference: "omniroute_preferred",
    },
    glm: {
      enabled: true,
      collapsed: false,
      selected_choice_ids: [LOGICAL_B.choice_id],
      transport_preference: "omniroute_preferred",
    },
  },
  advisor_choice_id: LOGICAL_B.choice_id,
  human_probability_percent: 50,
  unresolved_legacy_ids: [],
  route_review_required: [],
};

let catalog: Record<string, unknown>;
let prefsDto: Record<string, unknown>;
let roundState: string;
let rounds: Record<string, Record<string, unknown>>;
let failNext: number | null;

function roundPayload(id: string, overrides: Record<string, unknown> = {}) {
  return {
    object: "model_advisor.round",
    round_id: id,
    state: roundState,
    version: 3,
    etag: '"advisor-3-x"',
    failure_reason: null,
    execution: { session_id: null, uncertain: false },
    ...overrides,
  };
}

beforeEach(() => {
  api.mockReset();
  catalog = {
    object: "model_advisor.catalog",
    catalog_revision: "rev1",
    options: [OPTION_A, OPTION_B],
    logical_options: [LOGICAL_A, LOGICAL_B],
  };
  prefsDto = {
    object: "model_advisor.preferences",
    version: 2,
    etag: '"advisor-2-y"',
    state: "saved",
    preferences: V2_PREFERENCES,
    logical_preferences: V2_PREFERENCES,
  };
  roundState = "advisor_pending";
  rounds = {};
  failNext = null;
  api.mockImplementation(async (url: string, options?: RequestInit) => {
    if (failNext !== null) {
      const status = failNext;
      failNext = null;
      return Response.json({ detail: "boom" }, { status });
    }
    if (url.includes("/model-advisor/catalog")) return Response.json(catalog);
    if (
      url.includes("/model-advisor/preferences") &&
      (!options?.method || options.method === "GET")
    ) {
      return Response.json(prefsDto);
    }
    if (url.endsWith("/model-advisor/preferences") && options?.method === "PUT") {
      const body = JSON.parse(options.body as string);
      if (body.expected_version !== 2) {
        return Response.json({ detail: "Preferences changed in another tab" }, { status: 409 });
      }
      prefsDto = {
        ...prefsDto,
        version: 3,
        etag: '"advisor-3-y"',
        preferences: body.preferences,
        logical_preferences: body.preferences,
      };
      return Response.json(prefsDto);
    }
    if (url.endsWith("/model-advisor/rounds") && options?.method === "POST") {
      const body = JSON.parse(options.body as string);
      const id = "adviseround-1";
      rounds[id] = roundPayload(id, {
        state: "awaiting_confirmation",
        review: {
          schema_version: 2,
          round_fingerprint: "fp1",
          human_choice_id: body.human_choice_id,
          advisor_choice_id: LOGICAL_B.choice_id,
          human_candidate_id: body.human_choice_id,
          advisor_candidate_id: LOGICAL_B.choice_id,
          rationale: "Hard task, use the stronger logical choice.",
          assigned_choice_id: LOGICAL_B.choice_id,
          assigned_candidate_id: LOGICAL_B.choice_id,
          assigned_arm: "advisor",
          human_probability_percent: 50,
          overridden: false,
          override_reason: null,
          comparison_group: "randomized_unblinded",
        },
      });
      roundState = "awaiting_confirmation";
      return Response.json(rounds[id]);
    }
    const roundMatch = url.match(/\/model-advisor\/rounds\/([^/?]+)/);
    if (roundMatch) {
      const id = decodeURIComponent(roundMatch[1]);
      if (url.endsWith("/confirm")) {
        const body = JSON.parse(options?.body as string);
        if (body.expected_version !== rounds[id]?.version) {
          return Response.json({ detail: "Review changed" }, { status: 409 });
        }
        rounds[id] = {
          ...rounds[id],
          state: "dispatch_bound",
          version: (rounds[id]?.version as number) + 1,
          execution: { session_id: "conv_new", uncertain: false },
          requested_execution: {
            session_id: "conv_new",
            model: "glm/glm-5.3",
            reasoning_effort: "high",
            access_lane: "omniroute",
            comparison_group: "manual_override",
          },
          actual_execution: {
            status: "unknown",
            reason: "fixture has no provider telemetry",
          },
        };
        return Response.json(rounds[id]);
      }
      if (url.endsWith("/cancel")) {
        rounds[id] = {
          ...rounds[id],
          state: "cancelled",
          version: (rounds[id]?.version as number) + 1,
        };
        return Response.json(rounds[id]);
      }
      return Response.json(rounds[id] ?? roundPayload(id));
    }
    throw new Error(`Unexpected API call: ${url}`);
  });
});
afterEach(cleanup);

const GLM_PICK = { model: "glm-5.3", accessLane: "glm-direct", effort: "high" };

function mountSection(overrides: Partial<Parameters<typeof NewChatAdvisorSection>[0]> = {}) {
  return render(
    <NewChatAdvisorSection
      hostId="host_1"
      task="Write a test suite"
      humanPick={GLM_PICK}
      launchAgentId="ag_1"
      launchWorkspace="/repo"
      onLaunched={() => {}}
      {...overrides}
    />,
  );
}

it("renders nothing without a host", () => {
  const { container } = mountSection({ hostId: null, humanPick: null });
  expect(container).toBeEmptyDOMElement();
  expect(api).not.toHaveBeenCalled();
});

it("hydrates saved provider-grouped settings and shows enabled panel", async () => {
  mountSection();
  expect(await screen.findByRole("region", { name: "Model advisor settings" })).toBeDefined();
  const checkbox = screen.getByRole("switch", {
    name: /Compare my choice with the advisor/,
  }) as HTMLInputElement;
  expect(checkbox.checked).toBe(true);
  expect(screen.getByLabelText("Choose the advisor independently")).toHaveValue(
    LOGICAL_B.choice_id,
  );
  expect(await screen.findByText(/Save defaults/)).toBeDefined();
});

it("ignores hydration responses from a host that is no longer selected", async () => {
  let resolveOldCatalog!: (response: Response) => void;
  let resolveOldPreferences!: (response: Response) => void;
  const oldCatalog = new Promise<Response>((resolve) => {
    resolveOldCatalog = resolve;
  });
  const oldPreferences = new Promise<Response>((resolve) => {
    resolveOldPreferences = resolve;
  });
  api.mockImplementation(async (url: string) => {
    const host = new URL(url, "http://test.local").searchParams.get("host_id");
    if (host === "host_1") return url.includes("/catalog") ? oldCatalog : oldPreferences;
    if (url.includes("/catalog")) {
      return Response.json({
        ...catalog,
        catalog_revision: "host2",
        options: [OPTION_B],
        logical_options: [LOGICAL_B],
      });
    }
    if (url.includes("/preferences")) return Response.json(prefsDto);
    throw new Error(`Unexpected API call: ${url}`);
  });

  const view = mountSection();
  view.rerender(
    <NewChatAdvisorSection
      hostId="host_2"
      task="Write a test suite"
      humanPick={GLM_PICK}
      launchAgentId="ag_1"
      launchWorkspace="/repo"
      onLaunched={() => {}}
    />,
  );
  expect((await screen.findAllByText(/GLM-5\.3/)).length).toBeGreaterThan(0);

  resolveOldCatalog(
    Response.json({ ...catalog, catalog_revision: "stale-host1", logical_options: [LOGICAL_A] }),
  );
  resolveOldPreferences(Response.json(prefsDto));
  await waitFor(() => expect(screen.queryByText(/GPT-5\.5/)).toBeNull());
});

it("surfaces a catalog failure as a visible reason", async () => {
  api.mockImplementation(async (url: string) => {
    if (url.includes("/model-advisor/catalog"))
      return Response.json({ detail: "host offline" }, { status: 502 });
    if (url.includes("/model-advisor/preferences")) return Response.json(prefsDto);
    throw new Error(`Unexpected API call: ${url}`);
  });
  mountSection();
  expect(await screen.findByTestId("model-advisor-catalog-error")).toBeDefined();
});

it("reserves a logical round on Get recommendation and shows the review", async () => {
  mountSection();
  await screen.findByRole("region", { name: "Model advisor settings" });
  fireEvent.click(screen.getByRole("button", { name: "Get recommendation" }));
  await waitFor(() => expect(screen.getByLabelText("Review model assignment")).toBeDefined());
  expect(screen.getByText(/Advisor recommendation:/)).toBeDefined();
  expect(screen.getByText(/Hard task, use the stronger logical choice\./)).toBeDefined();
  const posted = api.mock.calls.find(
    ([url, options]) => url === "/v1/model-advisor/rounds" && options?.method === "POST",
  );
  assert(posted !== undefined);
  const body = JSON.parse((posted[1] as RequestInit).body as string);
  expect(body.human_choice_id).toBe(LOGICAL_B.choice_id);
  expect(body.human_candidate_id).toBeUndefined();
  expect(body.submission_key).toEqual(expect.any(String));
  expect(body.preferences.schema_version).toBe(2);
});

it("does not reserve a round without the composer prompt", async () => {
  mountSection({ task: "   " });
  await screen.findByRole("region", { name: "Model advisor settings" });
  fireEvent.click(screen.getByRole("button", { name: "Get recommendation" }));
  await waitFor(() => expect(screen.getByRole("status")).toBeDefined());
  expect(screen.getByText(/Write the task before asking/)).toBeDefined();
  const posted = api.mock.calls.find(([url]) => url === "/v1/model-advisor/rounds");
  expect(posted).toBeUndefined();
});

it("disables Get recommendation when the composer pick is outside the pool", async () => {
  mountSection({ humanPick: { model: "unknown-model", accessLane: null, effort: "" } });
  await screen.findByRole("region", { name: "Model advisor settings" });
  const button = screen.getByRole("button", { name: "Get recommendation" }) as HTMLButtonElement;
  expect(button.disabled).toBe(true);
  expect(screen.getByText(/Choose an allowed model/)).toBeDefined();
  fireEvent.click(button);
  const posted = api.mock.calls.find(([url]) => url === "/v1/model-advisor/rounds");
  expect(posted).toBeUndefined();
});

it("requires the exact lane, model, and reasoning effort", async () => {
  mountSection({
    humanPick: { model: OPTION_A.model_id, accessLane: OPTION_A.lane_id, effort: "high" },
  });
  await screen.findByRole("region", { name: "Model advisor settings" });
  const button = screen.getByRole("button", { name: "Get recommendation" }) as HTMLButtonElement;
  expect(button.disabled).toBe(true);
  expect(screen.getByText(/Choose an allowed model/)).toBeDefined();
});

it("confirms the logical assignment and reports the bound session", async () => {
  const onLaunched = vi.fn();
  mountSection({ onLaunched });
  await screen.findByRole("region", { name: "Model advisor settings" });
  fireEvent.click(screen.getByRole("button", { name: "Get recommendation" }));
  await screen.findByLabelText("Review model assignment");
  fireEvent.click(screen.getByRole("button", { name: "Run selected model" }));
  await waitFor(() => expect(onLaunched).toHaveBeenCalledWith("conv_new"));
  expect(screen.getByText(/Requested: glm\/glm-5\.3/)).toBeDefined();
  expect(screen.getByText(/Actual: unknown\/unverified/)).toBeDefined();
});

it("marks an explicit logical override and still launches", async () => {
  mountSection();
  await screen.findByRole("region", { name: "Model advisor settings" });
  fireEvent.click(screen.getByRole("button", { name: "Get recommendation" }));
  await screen.findByLabelText("Review model assignment");
  fireEvent.click(screen.getByRole("checkbox", { name: /Override this assignment/ }));
  const select = screen.getByLabelText("Run instead");
  fireEvent.change(select, { target: { value: LOGICAL_A.choice_id } });
  fireEvent.change(screen.getByLabelText("Override reason"), {
    target: { value: "I prefer OpenAI here" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Run selected model" }));
  await waitFor(() => {
    const confirm = api.mock.calls.find(
      ([url]) => typeof url === "string" && url.endsWith("/confirm"),
    );
    expect(confirm).toBeDefined();
  });
  const confirm = api.mock.calls.find(
    ([url]) => typeof url === "string" && url.endsWith("/confirm"),
  );
  assert(confirm !== undefined);
  const body = JSON.parse((confirm[1] as RequestInit).body as string);
  expect(body.override_candidate_id).toBe(LOGICAL_A.choice_id);
  expect(body.reason).toBe("I prefer OpenAI here");
});

it("surfaces a save conflict without overwriting the draft", async () => {
  mountSection();
  await screen.findByRole("region", { name: "Model advisor settings" });
  const balance = screen.getByLabelText(/Decision balance/);
  fireEvent.change(balance, { target: { value: "80" } });
  failNext = 409;
  fireEvent.click(screen.getByRole("button", { name: "Save defaults" }));
  await waitFor(() => expect(screen.getByRole("alert")).toBeDefined());
  expect(screen.getByText(/changed elsewhere/i)).toBeDefined();
});
