import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useChatStore } from "@/store/chatStore";
import type { OmniRouteCombo } from "@/lib/types";

// Composer reads workspace files / harness labels via TanStack query hooks.
// These picker tests don't exercise those paths, so stub the hooks to avoid
// needing a QueryClientProvider around every bare render.
vi.mock("@/hooks/useWorkspaceChangedFiles", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/hooks/useWorkspaceChangedFiles")>();
  return {
    ...actual,
    useWorkspaceAllFiles: () => ({ data: undefined }),
    useWorkspaceDirectory: () => ({ data: undefined }),
  };
});
vi.mock("@/hooks/useSession", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/useSession")>()),
  useSession: () => ({ session: { hostId: null }, isLoading: false, error: null }),
}));
vi.mock("@/hooks/useHosts", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/useHosts")>()),
  useHosts: () => ({ data: [] }),
}));
vi.mock("@/hooks/RunnerHealthProvider", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/RunnerHealthProvider")>()),
  useSessionHostOnline: () => undefined,
}));
vi.mock("@/lib/agentLabels", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/agentLabels")>()),
  useBrainHarnessLabels: () => ({
    "claude-sdk": "Claude SDK",
    codex: "Codex",
    cursor: "Cursor",
    pi: "Pi",
    antigravity: "Antigravity",
    copilot: "Copilot",
  }),
}));

import type { ReactElement } from "react";
import { TooltipProvider } from "@/components/ui/tooltip";
import { Composer } from "./ChatPage";

/** Minimal ComposerProps for an interactive (writable, idle) composer. */
function composerProps(overrides: Partial<Parameters<typeof Composer>[0]> = {}) {
  return {
    status: "idle" as const,
    isWorking: false,
    disabled: false,
    onSend: vi.fn(),
    onStop: vi.fn(),
    agents: [{ id: "a1", name: "opencode-native-ui", display_name: "OpenCode" }],
    agentsLoading: false,
    selectedAgentId: "a1",
    onSelectAgent: vi.fn(),
    permissionLevel: null,
    readOnlyReason: null,
    replyQuotes: [],
    onRemoveQuote: vi.fn(),
    onClearAllQuotes: vi.fn(),
    effortLevels: ["low", "medium", "high"] as const,
    showEffort: true,
    showModels: true,
    modelPickerKind: "opencode" as const,
    codexModelOptions: [],
    showCodexPlanMode: false,
    showRouteApprovalControl: false,
    routeApprovalEnabled: false,
    routeApprovalDisabled: false,
    ...overrides,
  };
}

function renderWithTooltips(ui: ReactElement) {
  return render(<TooltipProvider>{ui}</TooltipProvider>);
}

const SAMPLE_COMBOS: OmniRouteCombo[] = [
  {
    id: "auto/best-coding",
    display_name: "OmniRoute Coding Best",
    provider: "omniroute",
    kind: "combo",
    reasoning_efforts: ["medium", "high"],
    max_reasoning_effort: "high",
    default_reasoning_effort: "medium",
    requires_explicit_approval: false,
  },
  {
    id: "auto/coding:fast",
    display_name: "OmniRoute Coding Fast",
    provider: "omniroute",
    kind: "combo",
    reasoning_efforts: ["low", "medium"],
    max_reasoning_effort: "medium",
    default_reasoning_effort: "low",
    requires_explicit_approval: false,
  },
  {
    id: "auto/coding:reliable",
    display_name: "OmniRoute Coding Reliable",
    provider: "omniroute",
    kind: "combo",
    reasoning_efforts: ["medium", "high"],
    max_reasoning_effort: "high",
    default_reasoning_effort: "medium",
    requires_explicit_approval: false,
  },
];

describe("Composer OmniRoute combos picker", () => {
  beforeEach(() => {
    useChatStore.setState({
      omnirouteCombos: [],
      omnirouteCombosSource: null,
      selectedModel: null,
      sessionModelOverride: null,
      llmModel: null,
      selectedEffort: null,
    });
  });

  afterEach(() => {
    cleanup();
    useChatStore.setState({
      omnirouteCombos: [],
      omnirouteCombosSource: null,
    });
    vi.restoreAllMocks();
  });

  it("shows the three curated combos under an OmniRoute group for opencode-native", async () => {
    useChatStore.setState({ omnirouteCombos: SAMPLE_COMBOS });
    renderWithTooltips(<Composer {...composerProps()} />);
    const trigger = screen.getByTestId("agent-picker-trigger");
    // Radix dropdown opens on pointerdown for submenu/select triggers.
    fireEvent.pointerDown(trigger, { button: 0 });
    fireEvent.click(trigger);

    // Each combo gets a picker row under the OmniRoute group.
    const items = await screen.findAllByTestId("omniroute-combo-picker-item");
    expect(items).toHaveLength(3);

    // The display names appear (NOT just the cryptic `auto/coding:fast`).
    expect(screen.getByText("OmniRoute Coding Best")).toBeInTheDocument();
    expect(screen.getByText("OmniRoute Coding Fast")).toBeInTheDocument();
    expect(screen.getByText("OmniRoute Coding Reliable")).toBeInTheDocument();

    // The raw ids are visible (so a user knows what the runner dispatches).
    expect(screen.getByText("auto/best-coding")).toBeInTheDocument();
    expect(screen.getByText("auto/coding:fast")).toBeInTheDocument();
    expect(screen.getByText("auto/coding:reliable")).toBeInTheDocument();
  });

  it("does NOT show the OmniRoute group when the catalog is empty", () => {
    useChatStore.setState({ omnirouteCombos: [] });
    renderWithTooltips(<Composer {...composerProps()} />);
    const trigger = screen.getByTestId("agent-picker-trigger");
    fireEvent.pointerDown(trigger, { button: 0 });
    fireEvent.click(trigger);
    expect(screen.queryAllByTestId("omniroute-combo-picker-item")).toHaveLength(0);
  });

  it("does NOT show the OmniRoute group on harnesses that can't use it (claude-native)", () => {
    useChatStore.setState({ omnirouteCombos: SAMPLE_COMBOS });
    renderWithTooltips(<Composer {...composerProps({ modelPickerKind: "claude" })} />);
    const trigger = screen.getByTestId("agent-picker-trigger");
    fireEvent.pointerDown(trigger, { button: 0 });
    fireEvent.click(trigger);
    expect(screen.queryAllByTestId("omniroute-combo-picker-item")).toHaveLength(0);
  });

  it("surfaces the curated display name in the picker trigger when a combo is selected", () => {
    useChatStore.setState({
      omnirouteCombos: SAMPLE_COMBOS,
      selectedModel: "auto/coding:fast",
      sessionModelOverride: "auto/coding:fast",
    });
    renderWithTooltips(<Composer {...composerProps()} />);
    const trigger = screen.getByTestId("agent-picker-trigger");
    // Display name, not the raw id — users shouldn't see `auto/coding:fast`
    // when a curated label exists.
    expect(trigger).toHaveTextContent("OmniRoute Coding Fast");
  });
});
