/** Live v2 Model Advisor controller for the new-chat composer. */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  AdvisorConflictError,
  cancelRound,
  confirmRound,
  createProviderRound,
  fetchCatalog,
  fetchPreferences,
  fetchRound,
  saveProviderPreferences,
  toLogicalOptions,
  toSavedProviderPreferences,
  type RoundDto,
} from "@/lib/modelAdvisorApi";
import {
  emptyProviderPreferences,
  effectiveOptions,
  type LogicalOption,
  type ProviderPreferences,
} from "@/model-advisor/providerPreferences";
import {
  ProviderAdvisorReview,
  type ProviderReviewView,
} from "@/model-advisor/ProviderAdvisorReview";
import { ProviderSettingsPanel } from "@/model-advisor/ProviderSettingsPanel";

const POLL_INTERVAL_MS = 1500;
const POLL_TIMEOUT_MS = 180_000;

function newSubmissionKey(): string {
  if (typeof globalThis.crypto?.randomUUID === "function") return globalThis.crypto.randomUUID();
  return `submission-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

export interface HumanModelPick {
  model: string;
  accessLane: string | null;
  effort: string;
}

export interface NewChatAdvisorSectionProps {
  hostId: string | null;
  task: string;
  humanPick: HumanModelPick | null;
  launchAgentId: string | null;
  launchWorkspace: string | null;
  onLaunched: (sessionId: string) => void;
}

interface SavedProviderPreferences {
  version: number;
  etag: string;
  preferences: ProviderPreferences;
}

interface EditorState {
  saved: SavedProviderPreferences | null;
  draft: ProviderPreferences | null;
  dirty: boolean;
  error: string | null;
}

interface RoundFlowState {
  round: RoundDto | null;
  busy: boolean;
  error: string | null;
}

const IDLE_ROUND: RoundFlowState = { round: null, busy: false, error: null };

function samePreferences(a: ProviderPreferences, b: ProviderPreferences): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}

function providerReview(round: RoundDto | null): ProviderReviewView | null {
  if (!round?.review) return null;
  const humanChoiceId = round.review.human_choice_id ?? round.review.human_candidate_id;
  const advisorChoiceId = round.review.advisor_choice_id ?? round.review.advisor_candidate_id;
  const assignedChoiceId = round.review.assigned_choice_id ?? round.review.assigned_candidate_id;
  if (!humanChoiceId || !advisorChoiceId || !assignedChoiceId) return null;
  return {
    round_fingerprint: round.review.round_fingerprint,
    human_choice_id: humanChoiceId,
    advisor_choice_id: advisorChoiceId,
    rationale: round.review.rationale,
    assigned_choice_id: assignedChoiceId,
    assigned_arm: round.review.assigned_arm,
    human_probability_percent: round.review.human_probability_percent,
  };
}

export function NewChatAdvisorSection(props: NewChatAdvisorSectionProps) {
  const { hostId, task, humanPick, launchAgentId, launchWorkspace, onLaunched } = props;
  const scope = hostId ?? "";
  const [editor, setEditor] = useState<EditorState>({
    saved: null,
    draft: null,
    dirty: false,
    error: null,
  });
  const [options, setOptions] = useState<readonly LogicalOption[]>([]);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [round, setRound] = useState<RoundFlowState>(IDLE_ROUND);
  const pollTimer = useRef<ReturnType<typeof setInterval> | null>(null);
  const mounted = useRef(true);
  const scopeToken = useRef({ generation: 0, host: "" });
  const inputGeneration = useRef(0);
  const submissionIdentity = useRef<string | null>(null);
  const submissionKey = useRef<string | null>(null);

  const stopPolling = useCallback(() => {
    if (pollTimer.current !== null) clearInterval(pollTimer.current);
    pollTimer.current = null;
  }, []);

  const isCurrentScope = useCallback(
    (host: string, generation: number) =>
      mounted.current &&
      scopeToken.current.host === host &&
      scopeToken.current.generation === generation,
    [],
  );

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      stopPolling();
    };
  }, [stopPolling]);

  useEffect(() => {
    const generation = scopeToken.current.generation + 1;
    scopeToken.current = { generation, host: hostId ?? "" };
    inputGeneration.current += 1;
    stopPolling();
    submissionIdentity.current = null;
    submissionKey.current = null;
    setEditor({ saved: null, draft: null, dirty: false, error: null });
    setRound(IDLE_ROUND);
    setOptions([]);
    setCatalogError(null);
    if (hostId === null) return;
    let cancelled = false;
    void (async () => {
      try {
        const [catalog, prefs] = await Promise.all([
          fetchCatalog(hostId),
          fetchPreferences(hostId),
        ]);
        if (cancelled || !isCurrentScope(hostId, generation)) return;
        setOptions(toLogicalOptions(catalog));
        const saved = toSavedProviderPreferences(prefs);
        if (saved) {
          setEditor({ saved, draft: saved.preferences, dirty: false, error: null });
        } else {
          const empty = emptyProviderPreferences();
          setEditor({
            saved: { version: prefs.version, etag: prefs.etag ?? "", preferences: empty },
            draft: empty,
            dirty: false,
            error: null,
          });
        }
      } catch (cause) {
        if (!cancelled && isCurrentScope(hostId, generation)) {
          setCatalogError(
            cause instanceof Error ? cause.message : "Couldn't load advisor settings.",
          );
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [hostId, isCurrentScope, stopPolling]);

  const resolveHumanChoice = useCallback((): string | null => {
    if (!humanPick || humanPick.model === "") return null;
    const effort = humanPick.effort || "not_applicable";
    const matches = options.filter(
      (option) =>
        option.available &&
        option.model_ids.includes(humanPick.model) &&
        option.reasoning_effort === effort &&
        (humanPick.accessLane === null || option.access_lanes.includes(humanPick.accessLane)),
    );
    return matches.length === 1 ? matches[0].choice_id : null;
  }, [humanPick, options]);

  const validation = useMemo(() => {
    const draft = editor.draft;
    if (!draft || !draft.enabled) return null;
    if (draft.unresolved_legacy_ids.length || draft.route_review_required.length) {
      return "Review unresolved saved choices and confirm each connection preference.";
    }
    try {
      effectiveOptions(draft, options);
    } catch (cause) {
      return cause instanceof Error ? cause.message : "Select at least one active answer.";
    }
    if (
      !draft.advisor_choice_id ||
      !options.some((option) => option.choice_id === draft.advisor_choice_id && option.available)
    ) {
      return "Choose an available advisor model and reasoning level.";
    }
    const humanChoiceId = resolveHumanChoice();
    if (
      !humanChoiceId ||
      !effectiveOptions(draft, options).some((option) => option.choice_id === humanChoiceId)
    ) {
      return "Choose an allowed model and reasoning level in the composer.";
    }
    return null;
  }, [editor.draft, options, resolveHumanChoice]);

  useEffect(() => {
    const identity = JSON.stringify({
      scope,
      task,
      humanChoiceId: resolveHumanChoice(),
      preferences: editor.draft,
    });
    if (submissionIdentity.current !== null && submissionIdentity.current !== identity) {
      inputGeneration.current += 1;
      stopPolling();
      setRound(IDLE_ROUND);
    }
    if (submissionIdentity.current !== identity) submissionKey.current = newSubmissionKey();
    submissionIdentity.current = identity;
  }, [editor.draft, resolveHumanChoice, scope, stopPolling, task]);

  const pollRound = useCallback(
    (
      host: string,
      roundId: string,
      startedAt: number,
      generation: number,
      inputVersion: number,
    ) => {
      stopPolling();
      const tick = () =>
        void (async () => {
          try {
            const dto = await fetchRound(host, roundId);
            if (!isCurrentScope(host, generation) || inputGeneration.current !== inputVersion)
              return;
            if (dto.state === "advisor_pending" || dto.state === "assigning") {
              if (Date.now() - startedAt > POLL_TIMEOUT_MS) {
                stopPolling();
                setRound({
                  round: dto,
                  busy: false,
                  error: "The advisor is still preparing; check this round later.",
                });
              }
              return;
            }
            stopPolling();
            setRound({ round: dto, busy: false, error: null });
          } catch (cause) {
            if (!isCurrentScope(host, generation) || inputGeneration.current !== inputVersion)
              return;
            stopPolling();
            setRound({
              round: null,
              busy: false,
              error: cause instanceof Error ? cause.message : "Couldn't load the round.",
            });
          }
        })();
      tick();
      pollTimer.current = setInterval(tick, POLL_INTERVAL_MS);
    },
    [isCurrentScope, stopPolling],
  );

  const handleChange = useCallback((next: ProviderPreferences) => {
    setEditor((current) => ({
      ...current,
      draft: next,
      dirty: !current.saved || !samePreferences(current.saved.preferences, next),
      error: null,
    }));
  }, []);

  const handleSave = useCallback(() => {
    if (hostId === null || !editor.draft) return;
    const host = hostId;
    const generation = scopeToken.current.generation;
    const submitted = editor.draft;
    void (async () => {
      try {
        const dto = await saveProviderPreferences(
          host,
          "default",
          submitted,
          editor.saved?.version ?? 0,
        );
        const saved = toSavedProviderPreferences(dto);
        if (saved && isCurrentScope(host, generation))
          setEditor({ saved, draft: saved.preferences, dirty: false, error: null });
      } catch (cause) {
        if (!isCurrentScope(host, generation)) return;
        if (cause instanceof AdvisorConflictError) {
          setEditor((current) => ({
            ...current,
            error: "Saved settings changed elsewhere. Reload the panel and reapply.",
          }));
          try {
            const fresh = toSavedProviderPreferences(await fetchPreferences(host));
            if (fresh && isCurrentScope(host, generation)) {
              setEditor((current) => ({
                ...current,
                saved: fresh,
                dirty: current.draft !== null && !samePreferences(fresh.preferences, current.draft),
                error: "Saved settings changed elsewhere. Reload the panel and reapply.",
              }));
            }
          } catch {
            // Keep the conflict visible.
          }
          return;
        }
        setEditor((current) => ({
          ...current,
          error: cause instanceof Error ? cause.message : "Couldn't save settings.",
        }));
      }
    })();
  }, [editor.draft, editor.saved?.version, hostId, isCurrentScope]);

  const handlePropose = useCallback(() => {
    if (hostId === null || round.busy || !editor.draft || validation !== null) {
      if (validation !== null) setRound({ round: null, busy: false, error: validation });
      return;
    }
    if (task.trim() === "") {
      setRound({
        round: null,
        busy: false,
        error: "Write the task before asking for a recommendation.",
      });
      return;
    }
    const humanChoiceId = resolveHumanChoice();
    if (!humanChoiceId || !submissionKey.current) {
      setRound({
        round: null,
        busy: false,
        error: "Choose an allowed model before asking for a recommendation.",
      });
      return;
    }
    const host = hostId;
    const generation = scopeToken.current.generation;
    const inputVersion = inputGeneration.current;
    setRound({ round: null, busy: true, error: null });
    void (async () => {
      try {
        const dto = await createProviderRound(
          host,
          "default",
          task,
          humanChoiceId,
          editor.draft!,
          submissionKey.current!,
        );
        if (!isCurrentScope(host, generation) || inputGeneration.current !== inputVersion) return;
        setRound({ round: dto, busy: true, error: null });
        pollRound(host, dto.round_id, Date.now(), generation, inputVersion);
      } catch (cause) {
        if (!isCurrentScope(host, generation) || inputGeneration.current !== inputVersion) return;
        setRound({
          round: null,
          busy: false,
          error: cause instanceof Error ? cause.message : "Couldn't start the round.",
        });
      }
    })();
  }, [
    editor.draft,
    hostId,
    isCurrentScope,
    pollRound,
    resolveHumanChoice,
    round.busy,
    task,
    validation,
  ]);

  const handleConfirm = useCallback(
    (overrideId: string | null, reason: string | null) => {
      const host = hostId;
      const current = round.round;
      if (host === null || current === null || round.busy) return;
      if (launchAgentId === null || launchWorkspace === null) {
        setRound({
          round: current,
          busy: false,
          error: "Pick an agent and workspace before running.",
        });
        return;
      }
      const generation = scopeToken.current.generation;
      const inputVersion = inputGeneration.current;
      setRound({ round: current, busy: true, error: null });
      void (async () => {
        try {
          const dto = await confirmRound(
            host,
            current.round_id,
            current.version,
            { agent_id: launchAgentId, workspace: launchWorkspace },
            overrideId,
            reason,
          );
          if (!isCurrentScope(host, generation) || inputGeneration.current !== inputVersion) return;
          setRound({ round: dto, busy: false, error: null });
          if (dto.execution.session_id !== null) onLaunched(dto.execution.session_id);
        } catch (cause) {
          if (!isCurrentScope(host, generation) || inputGeneration.current !== inputVersion) return;
          setRound({
            round: current,
            busy: false,
            error: cause instanceof Error ? cause.message : "Couldn't run the round.",
          });
        }
      })();
    },
    [hostId, isCurrentScope, launchAgentId, launchWorkspace, onLaunched, round.busy, round.round],
  );

  const handleCancel = useCallback(() => {
    const host = hostId;
    const current = round.round;
    if (host === null || current === null || round.busy) return;
    const generation = scopeToken.current.generation;
    const inputVersion = inputGeneration.current;
    void (async () => {
      try {
        const dto = await cancelRound(host, current.round_id, current.version);
        if (isCurrentScope(host, generation) && inputGeneration.current === inputVersion)
          setRound({ round: dto, busy: false, error: null });
      } catch (cause) {
        if (isCurrentScope(host, generation) && inputGeneration.current === inputVersion)
          setRound({
            round: current,
            busy: false,
            error: cause instanceof Error ? cause.message : "Couldn't cancel the round.",
          });
      }
    })();
  }, [hostId, isCurrentScope, round.busy, round.round]);

  const review = providerReview(round.round);
  const reviewVisible =
    round.round !== null &&
    (round.round.state === "awaiting_confirmation" || round.round.state === "dispatch_claimed");
  if (hostId === null) return null;
  if (catalogError !== null)
    return (
      <p
        className="text-sm text-muted-foreground"
        role="status"
        data-testid="model-advisor-catalog-error"
      >
        Model advisor unavailable: {catalogError}
      </p>
    );
  return (
    <div className="space-y-3" data-testid="model-advisor-section">
      <ProviderSettingsPanel
        idPrefix={`model-advisor-${scope.replace(/[^A-Za-z0-9_-]/g, "-")}`}
        value={editor.draft}
        options={options}
        dirty={editor.dirty}
        busy={round.busy}
        error={editor.error}
        onChange={handleChange}
        onSave={handleSave}
      />
      {editor.draft?.enabled ? (
        <div className="space-y-2">
          <button
            type="button"
            className="rounded-md border px-3 py-2 text-sm disabled:opacity-50"
            disabled={round.busy || validation !== null}
            onClick={handlePropose}
          >
            {round.busy ? "Preparing recommendation…" : "Get recommendation"}
          </button>
          {validation ? (
            <p role="status" className="text-sm text-muted-foreground">
              {validation}
            </p>
          ) : null}
          <p className="text-xs text-muted-foreground">
            Your model remains selected in the composer. The advisor sees only the logical model and
            reasoning choices.
          </p>
        </div>
      ) : null}
      {reviewVisible && review ? (
        <ProviderAdvisorReview
          review={review}
          options={options}
          busy={round.busy}
          error={round.round?.launch_error ?? round.error}
          onConfirm={handleConfirm}
          onCancel={handleCancel}
        />
      ) : null}
      {!reviewVisible && round.error !== null ? (
        <p className="text-sm text-destructive" role="alert">
          {round.error}
        </p>
      ) : null}
      {round.round?.state === "blocked" ? (
        <p className="text-sm text-amber-600 dark:text-amber-500" role="alert">
          The round was blocked
          {round.round.failure_reason ? `: ${round.round.failure_reason}` : "."} Nothing ran. Start
          a new round to try again.
        </p>
      ) : null}
      {round.round?.execution.uncertain ? (
        <p className="text-sm text-amber-600 dark:text-amber-500" role="alert">
          The round was confirmed but its launch could not be verified. It is kept for inspection
          and will not retry automatically.
        </p>
      ) : null}
      {round.round?.requested_execution ? (
        <p className="text-xs text-muted-foreground">
          Requested: {round.round.requested_execution.model ?? "unknown model"}
          {round.round.requested_execution.reasoning_effort
            ? ` at ${round.round.requested_execution.reasoning_effort} reasoning`
            : ""}
          {round.round.requested_execution.access_lane
            ? ` via ${round.round.requested_execution.access_lane}`
            : ""}
          .
        </p>
      ) : null}
      {round.round?.actual_execution ? (
        <p className="text-xs text-muted-foreground">
          Actual:{" "}
          {round.round.actual_execution.status === "observed"
            ? `${round.round.actual_execution.model ?? "unknown model"}${round.round.actual_execution.reasoning_effort ? ` at ${round.round.actual_execution.reasoning_effort} reasoning` : ""}`
            : "unknown/unverified"}
          {round.round.actual_execution.reason ? ` — ${round.round.actual_execution.reason}` : ""}
        </p>
      ) : null}
    </div>
  );
}
