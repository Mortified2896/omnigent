import { createRef } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { assert, afterEach, beforeEach, expect, it, vi } from "vitest";
import {
  NewChatAdvisorSection as AdvisorSection,
  type AdvisorSubmitHandle,
} from "./NewChatAdvisorSection";

let composerSubmitRef = createRef<AdvisorSubmitHandle>();
function NewChatAdvisorSection(props: Parameters<typeof AdvisorSection>[0]) {
  return (
    <AdvisorSection
      submitRef={composerSubmitRef}
      settingsPanelTarget={settingsPanelTarget}
      {...props}
    />
  );
}
function send() {
  act(() => {
    composerSubmitRef.current!.submit();
  });
}
beforeEach(() => {
  composerSubmitRef = createRef<AdvisorSubmitHandle>();
});

const api = vi.hoisted(() => vi.fn());
vi.mock("@/lib/identity", () => ({
  authenticatedFetch: api,
  getCurrentUserId: () => "local",
  getCurrentAuthorId: () => "local",
}));

const OPTION_A = {
  candidate_id: "legacy-aaa",
  model_id: "gpt-6.1-sol",
  display_name: "GPT-6.1 Sol",
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
  model_id: "gpt-6.1-sol",
  display_name: "GPT-6.1 Sol",
  reasoning_effort: "medium",
  model_ids: ["gpt-6.1-sol", "codex/gpt-6.1-sol"],
  access_lanes: ["codex-direct", "omniroute"],
  default_access_lanes: ["codex-direct"],
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
let advisorModelTarget: HTMLDivElement;
let settingsPanelTarget: HTMLDivElement;

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
  localStorage.clear();
  advisorModelTarget = document.createElement("div");
  document.body.appendChild(advisorModelTarget);
  settingsPanelTarget = document.createElement("div");
  document.body.appendChild(settingsPanelTarget);
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
        const sessionId = body.launch.continue_session_id ?? "conv_new";
        rounds[id] = {
          ...rounds[id],
          state: "dispatch_bound",
          version: (rounds[id]?.version as number) + 1,
          execution: { session_id: sessionId, uncertain: false },
          requested_execution: {
            session_id: sessionId,
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
afterEach(() => {
  cleanup();
  advisorModelTarget.remove();
  settingsPanelTarget.remove();
});

const GLM_PICK = { model: "glm-5.3", accessLane: "glm-direct", effort: "high" };

function mountSection(overrides: Partial<Parameters<typeof NewChatAdvisorSection>[0]> = {}) {
  const view = render(
    <NewChatAdvisorSection
      requireConfirmation
      hostId="host_1"
      task="Write a test suite"
      humanPick={GLM_PICK}
      launchAgentId="ag_1"
      launchWorkspace="/repo"
      advisorModelTarget={advisorModelTarget}
      onLaunched={() => {}}
      {...overrides}
    />,
  );
  const settings = screen.queryByRole("button", { name: "Recommender settings" });
  if (settings) fireEvent.click(settings);
  return view;
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
  expect(screen.getByTestId("model-advisor-advisor-choice")).toHaveTextContent("GLM-5.3");
  expect(screen.queryByText("Your model and reasoning")).toBeNull();
  expect(screen.queryByTestId("model-advisor-human-choice")).toBeNull();
  const advisorPicker = screen.getByTestId("model-advisor-advisor-choice");
  const allowedAnswers = screen.getByText("Allowed answers — shared by you and the advisor");
  expect(
    advisorPicker.compareDocumentPosition(allowedAnswers) & Node.DOCUMENT_POSITION_FOLLOWING,
  ).toBeTruthy();
  expect(await screen.findByText(/Save defaults/)).toBeDefined();
});

it("selects recommender reasoning independently and persists the logical choice", async () => {
  catalog.logical_options = [
    LOGICAL_A,
    LOGICAL_B,
    {
      ...LOGICAL_B,
      choice_id: "choice-glm-medium",
      reasoning_effort: "medium",
    },
  ];
  mountSection();
  await screen.findByRole("combobox", { name: "Recommender model" });
  expect(screen.getByText("Recommender", { exact: true })).toBeDefined();
  fireEvent.click(screen.getByRole("combobox", { name: "Recommender reasoning effort" }));
  fireEvent.click(await screen.findByRole("option", { name: "Medium" }));
  expect(screen.getByTestId("model-advisor-advisor-choice")).toHaveTextContent("GLM-5.3");
  fireEvent.click(screen.getByRole("button", { name: "Save defaults" }));
  await waitFor(() => {
    const saved = api.mock.calls.find(([, init]) => init?.method === "PUT");
    expect(saved).toBeDefined();
    expect(JSON.parse(saved![1].body).preferences.advisor_choice_id).toBe("choice-glm-medium");
  });
});

it("keeps provider and model switches from erasing remembered reasoning", async () => {
  mountSection();
  await screen.findByRole("region", { name: "Model advisor settings" });

  const terra = screen.getByRole("switch", {
    name: "Enable GPT-6.1 Sol answers",
  }) as HTMLInputElement;
  const effort = screen.getByRole("checkbox", { name: "GPT-6.1 Sol: Medium" }) as HTMLInputElement;
  expect(terra.checked).toBe(true);
  expect(effort.checked).toBe(true);

  fireEvent.click(terra);
  expect(
    (screen.getByRole("switch", { name: "Enable GPT-6.1 Sol answers" }) as HTMLInputElement)
      .checked,
  ).toBe(false);
  expect(
    (screen.getByRole("checkbox", { name: "GPT-6.1 Sol: Medium" }) as HTMLInputElement).checked,
  ).toBe(true);
  expect(screen.getByRole("status")).toHaveTextContent("1 active combinations");

  const openai = screen.getByRole("switch", { name: "Enable OpenAI answers" }) as HTMLInputElement;
  fireEvent.click(openai);
  expect(
    (screen.getByRole("checkbox", { name: "GPT-6.1 Sol: Medium" }) as HTMLInputElement).checked,
  ).toBe(true);
  fireEvent.click(screen.getByRole("switch", { name: "Enable OpenAI answers" }));
  expect(
    (screen.getByRole("switch", { name: "Enable GPT-6.1 Sol answers" }) as HTMLInputElement)
      .checked,
  ).toBe(false);

  fireEvent.click(screen.getByRole("switch", { name: "Enable GPT-6.1 Sol answers" }));
  expect(
    (screen.getByRole("checkbox", { name: "GPT-6.1 Sol: Medium" }) as HTMLInputElement).checked,
  ).toBe(true);
});

it("allows an answer-disabled model as the independent advisor choice", async () => {
  mountSection();
  await screen.findByRole("region", { name: "Model advisor settings" });
  fireEvent.click(screen.getByRole("switch", { name: "Enable GPT-6.1 Sol answers" }));
  const advisor = screen.getByTestId("model-advisor-advisor-choice");
  fireEvent.click(advisor);
  fireEvent.click(await screen.findByRole("option", { name: /GPT-6\.1 Sol/ }));
  expect(screen.getByTestId("model-advisor-advisor-choice")).toHaveTextContent("GPT-6.1 Sol");
  expect(screen.getByTestId("model-advisor-advisor-effort")).toHaveTextContent("Medium");
});

it("does not replace a saved advisor choice missing from the live host catalog", async () => {
  prefsDto = {
    ...prefsDto,
    logical_preferences: { ...V2_PREFERENCES, advisor_choice_id: "saved-choice-missing" },
  };
  mountSection();
  await screen.findByText(/saved advisor model is unavailable from this host/i);
  expect(screen.getByTestId("model-advisor-advisor-choice")).toHaveTextContent(
    "Saved advisor model unavailable",
  );
  expect(screen.queryByRole("button", { name: "Get recommendation" })).not.toBeInTheDocument();
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
      advisorModelTarget={advisorModelTarget}
      onLaunched={() => {}}
    />,
  );
  expect((await screen.findAllByText(/GLM-5\.3/)).length).toBeGreaterThan(0);

  resolveOldCatalog(
    Response.json({ ...catalog, catalog_revision: "stale-host1", logical_options: [LOGICAL_A] }),
  );
  resolveOldPreferences(Response.json(prefsDto));
  await waitFor(() => expect(screen.queryByText(/GPT-6\.1 Sol/)).toBeNull());
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
  send();
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
  expect(body.preferences.schema_version).toBe(3);
});

it("does not reserve a round without the composer prompt", async () => {
  mountSection({ task: "   " });
  await screen.findByRole("region", { name: "Model advisor settings" });
  send();
  await waitFor(() => expect(screen.getByText(/Write the task before asking/)).toBeDefined());
  expect(screen.getByText(/Write the task before asking/)).toBeDefined();
  const posted = api.mock.calls.find(([url]) => url === "/v1/model-advisor/rounds");
  expect(posted).toBeUndefined();
});

it("disables Get recommendation when the composer pick is outside the pool", async () => {
  mountSection({ humanPick: { model: "unknown-model", accessLane: null, effort: "" } });
  await screen.findByRole("region", { name: "Model advisor settings" });
  expect(screen.queryByRole("button", { name: "Get recommendation" })).not.toBeInTheDocument();
  expect(screen.getByText(/Choose an allowed model/)).toBeDefined();
  send();
  const posted = api.mock.calls.find(([url]) => url === "/v1/model-advisor/rounds");
  expect(posted).toBeUndefined();
});

it("requires the exact lane, model, and reasoning effort", async () => {
  mountSection({
    humanPick: { model: OPTION_A.model_id, accessLane: OPTION_A.lane_id, effort: "high" },
  });
  await screen.findByRole("region", { name: "Model advisor settings" });
  expect(screen.queryByRole("button", { name: "Get recommendation" })).not.toBeInTheDocument();
  expect(screen.getByText(/Choose an allowed model/)).toBeDefined();
});

it("maps the composer Default effort to the host catalog's lane-specific default choice", async () => {
  mountSection({
    humanPick: { model: "codex/gpt-6.1-sol", accessLane: "codex-direct", effort: "" },
  });
  await screen.findByRole("region", { name: "Model advisor settings" });
  send();
  await waitFor(() => expect(screen.getByLabelText("Review model assignment")).toBeDefined());
  const posted = api.mock.calls.find(
    ([url, options]) => url === "/v1/model-advisor/rounds" && options?.method === "POST",
  );
  assert(posted !== undefined);
  expect(JSON.parse((posted[1] as RequestInit).body as string).human_choice_id).toBe(
    LOGICAL_A.choice_id,
  );
});

it("uses the current composer model and reasoning choice when creating each round", async () => {
  const view = mountSection({
    humanPick: { model: "glm-5.3", accessLane: "glm-direct", effort: "high" },
  });
  await screen.findByRole("region", { name: "Model advisor settings" });
  send();
  await waitFor(() => expect(screen.getByLabelText("Review model assignment")).toBeDefined());

  let posted = api.mock.calls.find(
    ([url, options]) => url === "/v1/model-advisor/rounds" && options?.method === "POST",
  );
  assert(posted !== undefined);
  expect(JSON.parse((posted[1] as RequestInit).body as string).human_choice_id).toBe(
    LOGICAL_B.choice_id,
  );

  view.rerender(
    <NewChatAdvisorSection
      hostId="host_1"
      task="Write a test suite"
      humanPick={{ model: "gpt-6.1-sol", accessLane: "codex-direct", effort: "medium" }}
      launchAgentId="ag_1"
      launchWorkspace="/repo"
      advisorModelTarget={advisorModelTarget}
      onLaunched={() => {}}
    />,
  );
  await waitFor(() =>
    expect(screen.queryByLabelText("Review model assignment")).not.toBeInTheDocument(),
  );
  send();
  await waitFor(() =>
    expect(
      api.mock.calls.filter(
        ([url, options]) => url === "/v1/model-advisor/rounds" && options?.method === "POST",
      ),
    ).toHaveLength(2),
  );
  posted = api.mock.calls.filter(
    ([url, options]) => url === "/v1/model-advisor/rounds" && options?.method === "POST",
  )[1];
  expect(JSON.parse((posted[1] as RequestInit).body as string).human_choice_id).toBe(
    LOGICAL_A.choice_id,
  );
});

it("confirms the logical assignment and reports the bound session", async () => {
  const onLaunched = vi.fn();
  mountSection({ onLaunched });
  await screen.findByRole("region", { name: "Model advisor settings" });
  send();
  await screen.findByLabelText("Review model assignment");
  fireEvent.click(screen.getByRole("button", { name: "Run selected model" }));
  await waitFor(() => expect(onLaunched).toHaveBeenCalledWith("conv_new"));
  expect(screen.getByText(/Requested: glm\/glm-5\.3/)).toBeDefined();
  expect(screen.getByText(/Actual: unknown\/unverified/)).toBeDefined();
});

it("confirms an in-chat round against the same session", async () => {
  const onLaunched = vi.fn();
  mountSection({ continueSessionId: "conv_existing", onLaunched });
  await screen.findByRole("region", { name: "Model advisor settings" });
  send();
  await screen.findByLabelText("Review model assignment");
  fireEvent.click(screen.getByRole("button", { name: "Run selected model" }));
  await waitFor(() => expect(onLaunched).toHaveBeenCalledWith("conv_existing"));
  const confirm = api.mock.calls.find(
    ([url]) => typeof url === "string" && url.endsWith("/confirm"),
  );
  assert(confirm !== undefined);
  const body = JSON.parse((confirm[1] as RequestInit).body as string);
  expect(body.launch.continue_session_id).toBe("conv_existing");
  expect(body.launch.agent_id).toBe("ag_1");
  expect(body.launch.workspace).toBe("/repo");
});

it("marks an explicit logical override and still launches", async () => {
  mountSection();
  await screen.findByRole("region", { name: "Model advisor settings" });
  send();
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

it("normal Send is owned by Advisor and cannot bypass a pending review", async () => {
  const submitRef = createRef<AdvisorSubmitHandle>();
  mountSection({ submitRef });
  await screen.findByRole("region", { name: "Model advisor settings" });
  let handled = false;
  act(() => {
    handled = submitRef.current!.submit();
  });
  expect(handled).toBe(true);
  await screen.findByText("Hard task, use the stronger logical choice.");
  act(() => {
    expect(submitRef.current!.submit()).toBe(true);
  });
  expect(
    api.mock.calls.filter(
      ([url, init]) => String(url).endsWith("/rounds") && init?.method === "POST",
    ),
  ).toHaveLength(1);
});

it("normal Send falls through only when Advisor is explicitly off", async () => {
  const submitRef = createRef<AdvisorSubmitHandle>();
  mountSection({ submitRef });
  await screen.findByRole("region", { name: "Model advisor settings" });
  fireEvent.click(screen.getByRole("switch", { name: "Compare my choice with the advisor" }));
  act(() => {
    expect(submitRef.current!.submit()).toBe(false);
  });
  expect(api.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(0);
});

it("does not silently send while saved Advisor settings are loading", () => {
  api.mockImplementation(() => new Promise(() => {}));
  const submitRef = createRef<AdvisorSubmitHandle>();
  mountSection({ submitRef });
  act(() => {
    expect(submitRef.current!.submit()).toBe(true);
  });
  expect(screen.getByText(/Wait for advisor settings/)).toBeInTheDocument();
});

it("an enabled follow-up uses its composer choice even when saved defaults are off", async () => {
  prefsDto = { ...prefsDto, logical_preferences: { ...V2_PREFERENCES, enabled: false } };
  mountSection({ continueSessionId: "existing" });
  const toggle = await screen.findByRole("switch", { name: "Compare my choice with the advisor" });
  expect(toggle).toBeChecked();
  expect(toggle).toBeDisabled();
  expect(screen.queryByRole("button", { name: "Get recommendation" })).not.toBeInTheDocument();
  await screen.findByLabelText("Review model assignment");
});

it("resolves an explicit model and effort independently of a stale composer connection", async () => {
  mountSection({
    humanPick: {
      model: "gpt-6.1-sol",
      effort: "medium",
      accessLane: "unavailable-previous-connection",
    },
  });
  await screen.findByRole("region", { name: "Model advisor settings" });
  expect(screen.queryByText(/Choose an allowed model/)).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Get recommendation" })).not.toBeInTheDocument();
  send();
  await screen.findByLabelText("Review model assignment");
  const posted = api.mock.calls.find(
    ([url, init]) => url === "/v1/model-advisor/rounds" && init?.method === "POST",
  );
  expect(JSON.parse(posted![1].body).human_choice_id).toBe(LOGICAL_A.choice_id);
});

it("keeps the Advisor row and selectors present when toggled off", async () => {
  render(
    <NewChatAdvisorSection
      hostId="host_1"
      task="Task"
      humanPick={GLM_PICK}
      launchAgentId="ag_1"
      launchWorkspace="/repo"
      onLaunched={() => {}}
    />,
  );
  const model = await screen.findByRole("combobox", { name: "Recommender model" });
  const effort = screen.getByRole("combobox", { name: "Recommender reasoning effort" });
  const toggle = screen.getByRole("switch", { name: "Enable Advisor" });
  expect(toggle).toBeChecked();
  fireEvent.click(toggle);
  expect(toggle).not.toBeChecked();
  expect(model).toBeInTheDocument();
  expect(effort).toBeInTheDocument();
  fireEvent.click(toggle);
  expect(toggle).toBeChecked();
  expect(model).toBeInTheDocument();
});

it("saves approval flags and pauses an advisor-selected guarded model for approval or override", async () => {
  mountSection({
    requireConfirmation: false,
    humanPick: { model: "gpt-6.1-sol", accessLane: "codex-direct", effort: "medium" },
  });
  const approval = await screen.findByRole("checkbox", {
    name: "Ask before running GLM-5.3 at High",
  });
  fireEvent.click(approval);
  fireEvent.click(screen.getByRole("button", { name: "Save defaults" }));
  await waitFor(() => expect(api.mock.calls.some(([, init]) => init?.method === "PUT")).toBe(true));
  const saved = api.mock.calls.find(([, init]) => init?.method === "PUT")!;
  expect(JSON.parse(saved[1].body).preferences.providers.glm.approval_choice_ids).toEqual([
    LOGICAL_B.choice_id,
  ]);
  send();
  await screen.findByRole("heading", { name: "Approval required before running" });
  expect(api.mock.calls.some(([url]) => url.endsWith("/confirm"))).toBe(false);
  fireEvent.click(screen.getByRole("checkbox", { name: "Override this assignment" }));
  fireEvent.change(screen.getByLabelText("Run instead"), {
    target: { value: LOGICAL_A.choice_id },
  });
  fireEvent.change(screen.getByLabelText("Override reason"), {
    target: { value: "Use the cheaper model" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Run selected model" }));
  await waitFor(() => expect(api.mock.calls.some(([url]) => url.endsWith("/confirm"))).toBe(true));
  const confirm = api.mock.calls.find(([url]) => url.endsWith("/confirm"))!;
  expect(JSON.parse(confirm[1].body).override_candidate_id).toBe(LOGICAL_A.choice_id);
});

it("retains the original human and recommender choices when continuing a newly launched chat", async () => {
  catalog.logical_options = [
    LOGICAL_A,
    LOGICAL_B,
    { ...LOGICAL_B, choice_id: "choice-glm-medium", reasoning_effort: "medium" },
  ];
  const original = { model: "gpt-6.1-sol", accessLane: "codex-direct", effort: "medium" };
  const launched = vi.fn();
  const view = mountSection({ humanPick: original, onLaunched: launched });
  await screen.findByRole("region", { name: "Model advisor settings" });
  fireEvent.click(screen.getByRole("combobox", { name: "Recommender reasoning effort" }));
  fireEvent.click(await screen.findByRole("option", { name: "Medium" }));
  send();
  await screen.findByRole("button", { name: "Run selected model" });
  fireEvent.click(screen.getByRole("button", { name: "Run selected model" }));
  await waitFor(() => expect(launched).toHaveBeenCalledWith("conv_new"));
  view.unmount();
  render(
    <NewChatAdvisorSection
      hostId="host_1"
      task=""
      humanPick={GLM_PICK}
      launchAgentId="ag_1"
      launchWorkspace="/repo"
      continueSessionId="conv_new"
      autoSubmit={false}
      enabledOverride
      onLaunched={() => {}}
    />,
  );
  expect(screen.queryByRole("combobox", { name: "Your model" })).toBeNull();
  expect(screen.queryByRole("combobox", { name: "Your reasoning effort" })).toBeNull();
  expect(await screen.findByRole("combobox", { name: "Recommender model" })).toHaveTextContent(
    "GLM-5.3",
  );
  expect(screen.getByRole("combobox", { name: "Recommender reasoning effort" })).toHaveTextContent(
    "Medium",
  );
  expect(
    api.mock.calls.filter(([url, init]) => url.endsWith("/rounds") && init?.method === "POST"),
  ).toHaveLength(1);
});

it("normal Send automatically runs the assignment once without revealing it", async () => {
  const onLaunched = vi.fn();
  const view = mountSection({ requireConfirmation: false, onLaunched });
  await screen.findByRole("region", { name: "Model advisor settings" });
  send();
  send();
  await waitFor(() => expect(onLaunched).toHaveBeenCalledWith("conv_new"));
  expect(screen.queryByLabelText("Review model assignment")).not.toBeInTheDocument();
  expect(screen.queryByText(/Hard task, use/)).not.toBeInTheDocument();
  expect(screen.queryByText(/Requested:/)).not.toBeInTheDocument();
  const confirms = api.mock.calls.filter(([url]) => String(url).endsWith("/confirm"));
  expect(confirms).toHaveLength(1);
  const body = JSON.parse(confirms[0][1].body);
  expect(body.override_candidate_id).toBeNull();
  expect(body.reason).toBeNull();
  view.unmount();
});

it("automatically runs follow-ups against their existing session", async () => {
  const onLaunched = vi.fn();
  mountSection({ requireConfirmation: false, continueSessionId: "existing", onLaunched });
  await waitFor(() => expect(onLaunched).toHaveBeenCalledWith("existing"));
  expect(screen.queryByRole("button", { name: "Run selected model" })).not.toBeInTheDocument();
  expect(api.mock.calls.filter(([url]) => String(url).endsWith("/confirm"))).toHaveLength(1);
});

it("does not automatically repeat a failed launch and allows an explicit retry", async () => {
  const original = api.getMockImplementation()!;
  let attempts = 0;
  api.mockImplementation(async (url, init) => {
    if (String(url).endsWith("/confirm") && ++attempts === 1) {
      return Response.json({ detail: "Launch unavailable" }, { status: 503 });
    }
    return original(url, init);
  });
  const onLaunched = vi.fn();
  mountSection({ requireConfirmation: false, onLaunched });
  await screen.findByRole("region", { name: "Model advisor settings" });
  send();
  await screen.findByText("Launch unavailable");
  expect(attempts).toBe(1);
  expect(onLaunched).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Retry launch" }));
  await waitFor(() => expect(onLaunched).toHaveBeenCalledWith("conv_new"));
  expect(attempts).toBe(2);
});

it("seeds declared defaults so enabling a never-configured advisor is valid immediately", async () => {
  prefsDto = {
    object: "model_advisor.preferences",
    version: 0,
    etag: null,
    state: "unsaved",
    preferences: null,
  };
  mountSection();
  await screen.findByRole("switch", { name: "Enable Advisor" });
  expect(screen.getByRole("switch", { name: "Enable Advisor" })).toBeChecked();
  // No validation error becomes the normal post-toggle state.
  await waitFor(() => {
    expect(screen.getByTestId("model-advisor-advisor-choice")).toHaveTextContent("GPT-6.1 Sol");
  });
  expect(screen.getByTestId("model-advisor-advisor-effort")).toHaveTextContent("Medium");
  expect(screen.queryByText(/Select at least one active model and reasoning level/)).toBeNull();
  expect(screen.queryByText(/Choose an available advisor model/)).toBeNull();
  // The seeded pool keeps both providers' defaults plus the composer pick.
  expect(screen.getByRole("checkbox", { name: "GPT-6.1 Sol: Medium" })).toBeChecked();
  expect(screen.getByRole("checkbox", { name: "GLM-5.3: High" })).toBeChecked();
});

it("reconciles the advisor effort to the declared default when the new model lacks it", async () => {
  mountSection();
  await screen.findByRole("combobox", { name: "Recommender model" });
  // Saved choice: GLM-5.3 at high. GPT-6.1 Sol offers medium (its declared
  // default) but not high, so the switch must land on Medium — never a
  // "Default" placeholder and never the old arbitrary first row.
  fireEvent.click(screen.getByRole("combobox", { name: "Recommender model" }));
  fireEvent.click(await screen.findByRole("option", { name: /GPT-6\.1 Sol/ }));
  expect(screen.getByTestId("model-advisor-advisor-choice")).toHaveTextContent("GPT-6.1 Sol");
  expect(screen.getByTestId("model-advisor-advisor-effort")).toHaveTextContent("Medium");
});

it("keeps the advisor effort across a model switch when it is still supported", async () => {
  catalog.logical_options = [
    LOGICAL_A,
    { ...LOGICAL_A, choice_id: "choice-openai-high", reasoning_effort: "high" },
    LOGICAL_B,
  ];
  mountSection();
  await screen.findByRole("combobox", { name: "Recommender model" });
  fireEvent.click(screen.getByRole("combobox", { name: "Recommender model" }));
  fireEvent.click(await screen.findByRole("option", { name: /GPT-6\.1 Sol/ }));
  expect(screen.getByTestId("model-advisor-advisor-choice")).toHaveTextContent("GPT-6.1 Sol");
  expect(screen.getByTestId("model-advisor-advisor-effort")).toHaveTextContent("High");
});

it("renders each advisor model's routes with the shared lane vocabulary", async () => {
  mountSection();
  await screen.findByRole("combobox", { name: "Recommender model" });
  fireEvent.click(screen.getByRole("combobox", { name: "Recommender model" }));
  const option = await screen.findByRole("option", { name: /GPT-6\.1 Sol/ });
  expect(option).toHaveTextContent("Codex Subscription — Direct · OmniRoute");
});

it("starts a visible elapsed timer immediately while the Advisor request is pending", async () => {
  mountSection();
  await screen.findByRole("region", { name: "Model advisor settings" });
  const previous = api.getMockImplementation()!;
  api.mockImplementation((url, init) =>
    url === "/v1/model-advisor/rounds" && init?.method === "POST"
      ? new Promise(() => {})
      : previous(url, init),
  );
  send();
  expect(screen.getByRole("status", { name: "Advisor progress" })).toHaveTextContent("0s elapsed");
  expect(screen.getByRole("status", { name: "Advisor progress" })).toHaveTextContent(
    "Advisor choosing a model",
  );
  const clock = vi.spyOn(Date, "now").mockReturnValue(Date.now() + 3000);
  await waitFor(
    () =>
      expect(screen.getByRole("status", { name: "Advisor progress" })).toHaveTextContent(
        "3s elapsed",
      ),
    { timeout: 2000 },
  );
  clock.mockRestore();
});

it("keeps the configured Advisor reasoning visible and disabled while off", async () => {
  mountSection({ enabledOverride: false });
  const model = await screen.findByTestId("model-advisor-advisor-choice");
  const effort = screen.getByTestId("model-advisor-advisor-effort");
  expect(model).toHaveTextContent("GLM-5.3");
  expect(model).toBeDisabled();
  expect(effort).toHaveTextContent("High");
  expect(effort).toBeDisabled();
});

it("defaults a fresh Advisor to on with Luna Max and allows turning it off", async () => {
  prefsDto = {
    object: "model_advisor.preferences",
    version: 0,
    etag: null,
    state: "unsaved",
    preferences: null,
  };
  catalog.logical_options = [
    LOGICAL_A,
    LOGICAL_B,
    {
      ...LOGICAL_A,
      choice_id: "luna-max",
      model_id: "gpt-6-luna",
      display_name: "GPT-6 Luna",
      model_ids: ["gpt-6-luna"],
      reasoning_effort: "max",
    },
  ];
  mountSection();
  await waitFor(() =>
    expect(screen.getByTestId("model-advisor-advisor-choice")).toHaveTextContent("GPT-6 Luna"),
  );
  expect(screen.getByTestId("model-advisor-advisor-effort")).toHaveTextContent("Max");
  expect(screen.getByTestId("model-advisor-advisor-effort")).toBeEnabled();
  expect(screen.getByRole("switch", { name: "Enable Advisor" })).toBeChecked();
  fireEvent.click(screen.getByRole("switch", { name: "Enable Advisor" }));
  expect(screen.getByTestId("model-advisor-advisor-effort")).toBeDisabled();
});


it("preserves a saved Off choice instead of applying the fresh default", async () => {
  const saved = prefsDto.preferences as Record<string, unknown>;
  prefsDto.preferences = { ...saved, enabled: false };
  prefsDto.logical_preferences = { ...saved, enabled: false };
  mountSection();
  await screen.findByTestId("model-advisor-advisor-choice");
  expect(screen.getByRole("switch", { name: "Enable Advisor" })).not.toBeChecked();
  expect(screen.getByTestId("model-advisor-advisor-effort")).toBeDisabled();
});
