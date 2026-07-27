// Targeted tests for the BulkActionBar's count and partial-failure
// behavior. The reported bug:
//
//   164 selected
//   Delete 120
//   Some actions failed. Retry or dismiss.
//
// The fix locks in:
//   - selection chip text reflects `selectedIds.size` (164), not a
//     filtered count;
//   - delete button label shows the exact attempted count (120) and
//     appends "M unavailable" when some selected ids aren't deletable;
//   - the confirm dialog names the attempted count AND the unavailable
//     split;
//   - on partial failure, successful ids leave the selection while
//     failed ids remain selected so Retry can target them.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";

const mocks = vi.hoisted(() => ({
  bulkDelete: {
    mutate: vi.fn(),
    isPending: false,
    isError: false,
    isSuccess: false,
    reset: vi.fn(),
    variables: undefined as string[] | undefined,
  },
}));

vi.mock("@/hooks/useConversations", () => ({
  useConversations: vi.fn(),
  useConnectedConversations: () => [],
  useStopAndDeleteConversation: () => ({
    mutate: vi.fn(),
    reset: vi.fn(),
    isPending: false,
    isError: false,
  }),
  usePinnedConversationBackfill: () => [],
  useRenameConversation: () => ({ mutate: vi.fn() }),
  useArchiveConversation: () => ({ mutate: vi.fn() }),
  useBulkArchiveConversations: () => ({
    mutate: vi.fn(),
    isPending: false,
    isError: false,
  }),
  useBulkDeleteConversations: () => mocks.bulkDelete,
  useBulkStopSessions: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useStopSession: () => ({ mutate: vi.fn() }),
  useProjects: () => ({ data: [] }),
  useProjectSessions: () => ({
    data: undefined,
    isLoading: false,
    hasNextPage: false,
    isFetchingNextPage: false,
    fetchNextPage: vi.fn(),
  }),
  useMoveToProject: () => ({ mutate: vi.fn() }),
  useDeleteProject: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  fetchProjectSessionIds: () => Promise.resolve([]),
  PROJECT_LABEL_KEY: "omni_project",
}));

vi.mock("@/components/PermissionsModal", () => ({ PermissionsModal: () => null }));

import { type Conversation, useConversations } from "@/hooks/useConversations";
import { Sidebar } from "./Sidebar";

const useConvMock = vi.mocked(useConversations);

const OWNED: Conversation[] = Array.from({ length: 120 }, (_, i) => ({
  id: `own_${i}`,
  object: "conversation",
  title: `Own ${i}`,
  created_at: 1_700_000_000,
  updated_at: 1_700_000_000,
  labels: {},
  permission_level: null,
  status: "idle",
}));

// One session shared with the viewer (not owner). When selected, the
// bulk-action bar must surface it as "unavailable" without silently
// dropping it.
const SHARED: Conversation = {
  id: "shared_1",
  object: "conversation",
  title: "Shared",
  created_at: 1_700_000_000,
  updated_at: 1_700_000_000,
  labels: {},
  permission_level: 1, // read-only — not owner
  status: "idle",
};

function mockConversations(conversations: Conversation[]) {
  const dataResult = {
    data: {
      pages: [
        {
          data: conversations,
          first_id: conversations[0]?.id ?? null,
          last_id: conversations.at(-1)?.id ?? null,
          has_more: false,
        },
      ],
      pageParams: [undefined],
    },
    isLoading: false,
    isError: false,
    error: null,
    fetchNextPage: vi.fn(),
    hasNextPage: false,
    isFetchingNextPage: false,
  } as unknown as ReturnType<typeof useConversations>;
  useConvMock.mockImplementation(() => dataResult);
}

function renderSidebar() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <TooltipProvider>
        <MemoryRouter initialEntries={["/"]}>
          <Sidebar open={true} onClose={vi.fn()} />
        </MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mocks.bulkDelete.mutate.mockReset();
  mocks.bulkDelete.reset.mockReset();
  mocks.bulkDelete.isPending = false;
  mocks.bulkDelete.isError = false;
  mocks.bulkDelete.isSuccess = false;
  mocks.bulkDelete.variables = undefined;
  mockConversations([...OWNED, SHARED]);
});

afterEach(() => {
  cleanup();
});

function enterSelectionMode() {
  fireEvent.click(screen.getByRole("button", { name: "Select sessions" }));
}

function selectAllLoaded() {
  fireEvent.click(screen.getByRole("button", { name: "Select all" }));
}

describe("BulkActionBar — counts and partial-failure UI", () => {
  it("shows the selected count and exact deletable count when nothing is unavailable", () => {
    // SHARED isn't owner; if selected, it's "unavailable". Don't
    // select it for this scenario.
    mockConversations(OWNED);
    renderSidebar();
    enterSelectionMode();
    selectAllLoaded();
    expect(screen.getByText("120 selected")).toBeInTheDocument();
    const deleteBtn = screen.getByTestId("bulk-delete");
    expect(deleteBtn.textContent).toContain("Delete");
    expect(deleteBtn.textContent).toContain("120");
    expect(screen.queryByTestId("bulk-delete-unavailable")).not.toBeInTheDocument();
  });

  it("shows 'M unavailable' alongside the attempted count when some selections aren't deletable", () => {
    renderSidebar();
    enterSelectionMode();
    selectAllLoaded();
    // 121 selected (120 owned + 1 shared), button reads "Delete 120 · 1 unavailable".
    expect(screen.getByText("121 selected")).toBeInTheDocument();
    const pill = screen.getByTestId("bulk-delete-unavailable");
    expect(pill.textContent).toContain("1 unavailable");
  });

  it("confirm dialog names the attempted count and the unavailable split", () => {
    renderSidebar();
    enterSelectionMode();
    selectAllLoaded();
    fireEvent.click(screen.getByTestId("bulk-delete"));
    const dialog = screen.getByRole("dialog");
    expect(dialog.textContent).toContain("Delete 120 session(s)?");
    const note = screen.getByTestId("confirm-delete-unavailable");
    expect(note.textContent).toContain("1 of the 121 selected session");
    expect(note.textContent).toContain("can't be deleted");
  });

  it("calls bulkDelete.mutate with only the owned (deletable) ids", () => {
    renderSidebar();
    enterSelectionMode();
    selectAllLoaded();
    fireEvent.click(screen.getByTestId("bulk-delete"));
    const dialog = screen.getByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: /Delete 120 session/i }));
    expect(mocks.bulkDelete.mutate).toHaveBeenCalledTimes(1);
    const idsArg = mocks.bulkDelete.mutate.mock.calls[0][0] as string[];
    expect(idsArg).toHaveLength(120);
    expect(idsArg).not.toContain("shared_1");
  });

  it("partial failure: banner lists counts, Retry targets only failed ids, successful ids leave selection", () => {
    renderSidebar();
    enterSelectionMode();
    selectAllLoaded();
    fireEvent.click(screen.getByTestId("bulk-delete"));
    const dialog = screen.getByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: /Delete 120 session/i }));

    const attemptedIds = mocks.bulkDelete.mutate.mock.calls[0][0] as string[];
    const failedId = attemptedIds[5];
    const succeededIds = attemptedIds.filter((id) => id !== failedId);
    const onError = mocks.bulkDelete.mutate.mock.calls[0][1].onError as (e: unknown) => void;
    act(() => {
      onError({
        attempted: attemptedIds,
        succeeded: succeededIds,
        alreadyDeleted: [],
        forbidden: [],
        activeSession: [],
        failed: [{ id: failedId, reason: "Server timeout", retryable: true }],
      });
    });

    const banner = screen.getByTestId("bulk-delete-failure");
    expect(banner.textContent).toContain("119 deleted");
    expect(banner.textContent).toContain("1 could not be deleted");
    expect(banner.textContent).toContain(failedId);

    fireEvent.click(screen.getByTestId("bulk-delete-retry"));
    expect(mocks.bulkDelete.mutate).toHaveBeenCalledTimes(2);
    expect(mocks.bulkDelete.mutate.mock.calls[1][0]).toEqual([failedId]);
  });

  it("Dismiss clears the banner but preserves the failed-id selection", () => {
    renderSidebar();
    enterSelectionMode();
    selectAllLoaded();
    fireEvent.click(screen.getByTestId("bulk-delete"));
    const dialog = screen.getByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: /Delete 120 session/i }));
    const attemptedIds = mocks.bulkDelete.mutate.mock.calls[0][0] as string[];
    const failedId = attemptedIds[3];
    act(() => {
      (mocks.bulkDelete.mutate.mock.calls[0][1].onError as (e: unknown) => void)({
        attempted: attemptedIds,
        succeeded: attemptedIds.filter((id) => id !== failedId),
        alreadyDeleted: [],
        forbidden: [],
        activeSession: [],
        failed: [{ id: failedId, reason: "Network error", retryable: true }],
      });
    });

    fireEvent.click(screen.getByTestId("bulk-delete-dismiss"));
    expect(screen.queryByTestId("bulk-delete-failure")).not.toBeInTheDocument();
    // Successful ids left the selection; only the failed id remains
    // selected (plus the always-unavailable SHARED row from Select all).
    expect(screen.getByText("2 selected")).toBeInTheDocument();
  });

  it("Clear removes the entire selection", () => {
    renderSidebar();
    enterSelectionMode();
    selectAllLoaded();
    expect(screen.getByText("121 selected")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Clear" }));
    expect(screen.getByText("None selected")).toBeInTheDocument();
  });
});