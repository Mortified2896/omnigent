import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { assert, afterEach, beforeEach, expect, it, vi } from "vitest";
import { NewChatAdvisorSection } from "./NewChatAdvisorSection";

const api = vi.hoisted(() => vi.fn());
vi.mock("@/lib/identity", () => ({ authenticatedFetch: api, getCurrentUserId: () => "local" }));

const OPTION_A = {
  candidate_id: "choice-aaa",
  model_id: "gpt-5.3-codex",
  display_name: "gpt-5.3-codex",
  lane_id: "codex-direct" as const,
  reasoning_effort: "medium",
  access_class: "chatgpt_plan",
  is_default_effort: true,
};
const OPTION_B = {
  candidate_id: "choice-bbb",
  model_id: "glm-5.3",
  display_name: "GLM-5.3",
  lane_id: "glm-direct" as const,
  reasoning_effort: "high",
  access_class: "glm_plan",
  is_default_effort: false,
};

let catalog: { object: string; catalog_revision: string; options: unknown[] };
let prefsDto: Record<string, unknown>;
let savedPrefs: Record<string, unknown> | null;
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
  };
  savedPrefs = {
    object: "model_advisor.preferences",
    version: 2,
    etag: '"advisor-2-y"',
    state: "saved",
    preferences: {
      schema_version: 1,
      enabled: true,
      allowed_candidate_ids: ["choice-aaa", "choice-bbb"],
      advisor_candidate_id: "choice-bbb",
      human_probability_percent: 50,
    },
  };
  prefsDto = savedPrefs;
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
      savedPrefs = {
        ...prefsDto,
        version: 3,
        etag: '"advisor-3-y"',
        preferences: body.preferences,
      };
      prefsDto = savedPrefs;
      return Response.json(savedPrefs);
    }
    if (url.endsWith("/model-advisor/rounds") && options?.method === "POST") {
      const body = JSON.parse(options.body as string);
      const id = "adviseround-1";
      rounds[id] = roundPayload(id, {
        state: "awaiting_confirmation",
        review: {
          round_fingerprint: "fp1",
          human_candidate_id: body.human_candidate_id,
          advisor_candidate_id: "choice-bbb",
          rationale: "Hard task, use the stronger lane.",
          assigned_candidate_id: "choice-bbb",
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
          observed_execution: {
            session_id: "conv_new",
            model: "glm-5.3",
            reasoning_effort: "high",
            access_lane: "glm-direct",
            comparison_group: "manual_override",
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
      onHumanCandidateChosen={() => {}}
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

it("hydrates saved settings and shows enabled panel", async () => {
  mountSection();
  expect(await screen.findByLabelText("Model advisor")).toBeDefined();
  const checkbox = screen.getByRole("checkbox", {
    name: /Compare my choice with the advisor/,
  }) as HTMLInputElement;
  expect(checkbox.checked).toBe(true);
  expect(await screen.findByText(/Save defaults/)).toBeDefined();
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

it("reserves a round on Get recommendation and shows the review", async () => {
  mountSection();
  await screen.findByLabelText("Model advisor");
  fireEvent.click(screen.getByRole("button", { name: "Get recommendation" }));
  await waitFor(() => expect(screen.getByLabelText("Review model assignment")).toBeDefined());
  expect(screen.getByText(/Advisor recommendation:/)).toBeDefined();
  expect(screen.getByText(/Hard task, use the stronger lane\./)).toBeDefined();
  const posted = api.mock.calls.find(
    ([url, options]) => url === "/v1/model-advisor/rounds" && options?.method === "POST",
  );
  assert(posted !== undefined);
  const body = JSON.parse((posted[1] as RequestInit).body as string);
  expect(body.human_candidate_id).toBe("choice-bbb");
});

it("does not reserve a round without the composer prompt", async () => {
  mountSection({ task: "   " });
  await screen.findByLabelText("Model advisor");
  fireEvent.click(screen.getByRole("button", { name: "Get recommendation" }));
  await waitFor(() => expect(screen.getByRole("alert")).toBeDefined());
  expect(screen.getByText(/Write the task before asking/)).toBeDefined();
  const posted = api.mock.calls.find(([url]) => url === "/v1/model-advisor/rounds");
  expect(posted).toBeUndefined();
});

it("disables Get recommendation when the composer pick is outside the pool", async () => {
  mountSection({ humanPick: { model: "unknown-model", accessLane: null, effort: "" } });
  await screen.findByLabelText("Model advisor");
  const button = screen.getByRole("button", { name: "Get recommendation" }) as HTMLButtonElement;
  expect(button.disabled).toBe(true);
  fireEvent.click(button);
  const posted = api.mock.calls.find(([url]) => url === "/v1/model-advisor/rounds");
  expect(posted).toBeUndefined();
});

it("confirms the assignment and reports the bound session", async () => {
  const onLaunched = vi.fn();
  mountSection({ onLaunched });
  await screen.findByLabelText("Model advisor");
  fireEvent.click(screen.getByRole("button", { name: "Get recommendation" }));
  await screen.findByLabelText("Review model assignment");
  fireEvent.click(screen.getByRole("button", { name: "Run selected model" }));
  await waitFor(() => expect(onLaunched).toHaveBeenCalledWith("conv_new"));
  expect(screen.getByText(/Actually ran glm-5\.3/)).toBeDefined();
});

it("marks an explicit override and still launches", async () => {
  mountSection();
  await screen.findByLabelText("Model advisor");
  fireEvent.click(screen.getByRole("button", { name: "Get recommendation" }));
  await screen.findByLabelText("Review model assignment");
  fireEvent.click(screen.getByRole("checkbox", { name: /Override this assignment/ }));
  const select = screen.getByLabelText("Run instead");
  fireEvent.change(select, { target: { value: "choice-aaa" } });
  fireEvent.change(screen.getByLabelText("Override reason"), {
    target: { value: "I prefer codex here" },
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
  expect(body.override_candidate_id).toBe("choice-aaa");
  expect(body.reason).toBe("I prefer codex here");
});

it("surfaces a save conflict without overwriting the draft", async () => {
  mountSection();
  await screen.findByLabelText("Model advisor");
  const balance = screen.getByLabelText(/Decision balance/);
  fireEvent.change(balance, { target: { value: "80" } });
  failNext = 409;
  fireEvent.click(screen.getByRole("button", { name: "Save defaults" }));
  await waitFor(() => expect(screen.getByRole("alert")).toBeDefined());
  expect(screen.getByText(/changed elsewhere/i)).toBeDefined();
});
