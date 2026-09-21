/** Gated model-advisor controller for the new-chat landing composer.
 *
 * The parent (NewChatLandingScreen) owns the composer state; this component
 * owns the advisor API lifecycle only: hydrate saved settings per host once,
 * reserve one round on an explicit Get recommendation, poll it, and run or
 * cancel after the visible review. No provider call happens on typing,
 * mount or settings load — hydration reads saved preferences and the same
 * host model catalog the model picker already shows.
 */
import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";

import {
  AdvisorConflictError,
  cancelRound,
  confirmRound,
  createRound,
  fetchCatalog,
  fetchPreferences,
  fetchRound,
  savePreferences,
  toAdvisorOptions,
  toSavedPreferences,
  toReviewView,
  type RoundDto,
} from "@/lib/modelAdvisorApi";
import { ModelAdvisorPanel, ModelAdvisorReview } from "@/model-advisor/ModelAdvisorPanel";
import { editorReducer, initialEditor, type AdvisorOption } from "@/model-advisor/editor";

const POLL_INTERVAL_MS = 1500;
const POLL_TIMEOUT_MS = 180_000;

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
  /** Reflects the composer's model/effort when the human pick maps to a catalog candidate. */
  onHumanCandidateChosen: (pick: HumanModelPick) => void;
  /** Called once the server bound the round to its created session. */
  onLaunched: (sessionId: string) => void;
}

interface RoundFlowState {
  round: RoundDto | null;
  busy: boolean;
  error: string | null;
}

const IDLE_ROUND: RoundFlowState = { round: null, busy: false, error: null };

export function NewChatAdvisorSection(props: NewChatAdvisorSectionProps) {
  const {
    hostId,
    task,
    humanPick,
    launchAgentId,
    launchWorkspace,
    onHumanCandidateChosen,
    onLaunched,
  } = props;
  const scope = hostId ?? "";
  const [editor, dispatchEditor] = useReducer(editorReducer, scope, initialEditor);
  const [options, setOptions] = useState<readonly AdvisorOption[]>([]);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [round, setRound] = useState<RoundFlowState>(IDLE_ROUND);
  const pollTimer = useRef<ReturnType<typeof setInterval> | null>(null);
  const onScreen = useRef(true);

  useEffect(() => {
    onScreen.current = true;
    return () => {
      onScreen.current = false;
      if (pollTimer.current !== null) clearInterval(pollTimer.current);
      pollTimer.current = null;
    };
  }, []);

  // Scope change = host change: reset everything, hydrate saved settings and
  // the qualified catalog. Reads only; no provider calls start here.
  useEffect(() => {
    dispatchEditor({ type: "scope", scope });
    setRound(IDLE_ROUND);
    setCatalogError(null);
    if (hostId === null) return;
    let cancelled = false;
    const hydrate = async () => {
      try {
        const [catalog, prefs] = await Promise.all([
          fetchCatalog(hostId),
          fetchPreferences(hostId),
        ]);
        if (cancelled) return;
        setOptions(toAdvisorOptions(catalog));
        const saved = toSavedPreferences(prefs);
        if (saved) {
          dispatchEditor({ type: "hydrate", scope, saved });
        } else {
          // First visit: an empty disabled draft the user can fill and save.
          dispatchEditor({
            type: "hydrate",
            scope,
            saved: {
              version: 0,
              etag: "",
              preferences: {
                schema_version: 1,
                enabled: false,
                allowed_candidate_ids: [],
                advisor_candidate_id: null,
                human_probability_percent: 50,
              },
            },
          });
        }
      } catch (cause) {
        if (!cancelled) {
          setCatalogError(
            cause instanceof Error ? cause.message : "Couldn't load advisor settings.",
          );
        }
      }
    };
    void hydrate();
    return () => {
      cancelled = true;
    };
  }, [hostId, scope]);

  const stopPolling = useCallback(() => {
    if (pollTimer.current !== null) clearInterval(pollTimer.current);
    pollTimer.current = null;
  }, []);

  const pollRound = useCallback(
    (host: string, roundId: string, startedAt: number) => {
      stopPolling();
      const tick = () => {
        void (async () => {
          try {
            const dto = await fetchRound(host, roundId);
            if (!onScreen.current) return;
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
            stopPolling();
            setRound({
              round: null,
              busy: false,
              error: cause instanceof Error ? cause.message : "Couldn't load the round.",
            });
          }
        })();
      };
      // First check lands immediately (most rounds finish between the create
      // response and the first tick), then keep polling on the interval.
      tick();
      pollTimer.current = setInterval(tick, POLL_INTERVAL_MS);
    },
    [stopPolling],
  );

  const resolveHumanCandidate = useCallback((): string | null => {
    const pick = humanPick;
    if (!pick || pick.model === "") return null;
    const matching = options.filter((option) => option.model_id === pick.model);
    if (matching.length === 0) return null;
    const byLane = pick.accessLane
      ? matching.filter((option) => option.lane_id === pick.accessLane)
      : matching;
    const pool = byLane.length > 0 ? byLane : matching;
    const exactEffort = pick.effort
      ? pool.find((option) => option.reasoning_effort === pick.effort)
      : undefined;
    if (exactEffort) return exactEffort.candidate_id;
    const notApplicable = pool.find((option) => option.reasoning_effort === "not_applicable");
    return notApplicable?.candidate_id ?? pool[0]?.candidate_id ?? null;
  }, [humanPick, options]);

  const handlePropose = useCallback(() => {
    if (hostId === null || round.busy) return;
    const draft = editor.draft;
    if (!draft?.enabled) return;
    if (task.trim() === "") {
      setRound({
        round: null,
        busy: false,
        error: "Write the task before asking for a recommendation.",
      });
      return;
    }
    const humanCandidateId = resolveHumanCandidate();
    if (humanCandidateId === null) {
      setRound({
        round: null,
        busy: false,
        error:
          "Your composer pick is not one of the allowed answers. Choose an allowed model first.",
      });
      return;
    }
    setRound({ round: null, busy: true, error: null });
    void (async () => {
      try {
        const dto = await createRound(hostId, "default", task, humanCandidateId);
        if (!onScreen.current) return;
        setRound({ round: dto, busy: true, error: null });
        pollRound(hostId, dto.round_id, Date.now());
      } catch (cause) {
        if (!onScreen.current) return;
        setRound({
          round: null,
          busy: false,
          error: cause instanceof Error ? cause.message : "Couldn't start the round.",
        });
      }
    })();
  }, [editor.draft, hostId, pollRound, resolveHumanCandidate, round.busy, task]);

  const handleSave = useCallback(() => {
    if (hostId === null || !editor.draft) return;
    const submitted = editor.draft;
    void (async () => {
      try {
        const dto = await savePreferences(hostId, "default", submitted, editor.saved?.version ?? 0);
        const saved = toSavedPreferences(dto);
        if (saved && onScreen.current) {
          dispatchEditor({ type: "saved", scope, saved, submitted });
        }
      } catch (cause) {
        if (cause instanceof AdvisorConflictError) {
          dispatchEditor({
            type: "error",
            scope,
            message: "Saved settings changed elsewhere. Reload the panel and reapply.",
          });
          try {
            const prefs = await fetchPreferences(hostId);
            const fresh = toSavedPreferences(prefs);
            if (fresh && onScreen.current) dispatchEditor({ type: "hydrate", scope, saved: fresh });
          } catch {
            // Keep the conflict error visible.
          }
          return;
        }
        dispatchEditor({
          type: "error",
          scope,
          message: cause instanceof Error ? cause.message : "Couldn't save settings.",
        });
      }
    })();
  }, [editor.draft, editor.saved?.version, hostId, scope]);

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
      setRound({ round: current, busy: true, error: null });
      void (async () => {
        try {
          const dto = await confirmRound(
            host,
            current.round_id,
            current.version,
            {
              agent_id: launchAgentId,
              workspace: launchWorkspace,
            },
            overrideId,
            reason,
          );
          if (!onScreen.current) return;
          setRound({ round: dto, busy: false, error: null });
          if (dto.execution.session_id !== null) {
            onLaunched(dto.execution.session_id);
          }
        } catch (cause) {
          if (!onScreen.current) return;
          setRound({
            round: current,
            busy: false,
            error: cause instanceof Error ? cause.message : "Couldn't run the round.",
          });
        }
      })();
    },
    [hostId, launchAgentId, launchWorkspace, onLaunched, round.busy, round.round],
  );

  const handleCancel = useCallback(() => {
    const host = hostId;
    const current = round.round;
    if (host === null || current === null || round.busy) return;
    void (async () => {
      try {
        const dto = await cancelRound(host, current.round_id, current.version);
        if (onScreen.current) setRound({ round: dto, busy: false, error: null });
      } catch (cause) {
        if (onScreen.current) {
          setRound({
            round: current,
            busy: false,
            error: cause instanceof Error ? cause.message : "Couldn't cancel the round.",
          });
        }
      }
    })();
  }, [hostId, round.busy, round.round]);

  const review = useMemo(() => toReviewView(round.round ?? undefinedRound), [round.round]);
  const reviewVisible =
    round.round !== null &&
    (round.round.state === "awaiting_confirmation" || round.round.state === "dispatch_claimed");

  if (hostId === null) return null;
  if (catalogError !== null) {
    return (
      <p
        className="text-sm text-muted-foreground"
        role="status"
        data-testid="model-advisor-catalog-error"
      >
        Model advisor unavailable: {catalogError}
      </p>
    );
  }
  return (
    <div className="space-y-3" data-testid="model-advisor-section">
      <ModelAdvisorPanel
        value={editor.draft}
        options={options}
        dirty={editor.dirty}
        busy={round.busy}
        error={editor.error}
        humanCandidateId={resolveHumanCandidate()}
        onChange={(next) => dispatchEditor({ type: "edit", preferences: next })}
        onHumanChoice={(candidateId) => {
          const option = options.find((row) => row.candidate_id === candidateId);
          if (option) {
            onHumanCandidateChosen({
              model: option.model_id,
              accessLane: option.lane_id,
              effort: option.reasoning_effort === "not_applicable" ? "" : option.reasoning_effort,
            });
          }
        }}
        onSave={handleSave}
        onPropose={handlePropose}
      />
      {reviewVisible && review !== null ? (
        <ModelAdvisorReview
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
      {round.round?.observed_execution ? (
        <p className="text-xs text-muted-foreground">
          Actually ran {round.round.observed_execution.model ?? "unknown model"}
          {round.round.observed_execution.reasoning_effort
            ? ` at ${round.round.observed_execution.reasoning_effort} reasoning`
            : ""}
          {round.round.observed_execution.access_lane
            ? ` via ${round.round.observed_execution.access_lane}`
            : ""}
          .
        </p>
      ) : null}
    </div>
  );
}

const undefinedRound: RoundDto = {
  object: "model_advisor.round",
  round_id: "",
  state: "cancelled",
  version: 0,
  etag: "",
  failure_reason: null,
  execution: { session_id: null, uncertain: false },
};
