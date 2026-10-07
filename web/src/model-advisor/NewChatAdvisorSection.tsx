import { currentModelChoices } from "@/lib/currentModelChoices";
/** Live Model Advisor controller for the new-chat composer. */
import {
  useCallback,
  useEffect,
  useImperativeHandle,
  useMemo,
  useRef,
  useState,
  type Ref,
} from "react";
import { createPortal } from "react-dom";
import { Clock3Icon, SettingsIcon, SparklesIcon } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";

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
import { SearchableModelPicker, type ModelPickerOption } from "@/components/SearchableModelPicker";
import {
  advisorEffortOptions,
  advisorGroupHeading,
  advisorModelKey,
  advisorModelRows,
  reconcileAdvisorChoice,
  seedFreshAdvisorDraft,
} from "@/model-advisor/advisorModelPresentation";
import {
  ProviderAdvisorReview,
  type ProviderReviewView,
} from "@/model-advisor/ProviderAdvisorReview";
import { ComposerEffortPicker } from "@/components/composer/ComposerControls";
import { ProviderSettingsPanel } from "@/model-advisor/ProviderSettingsPanel";

import { readSessionAdvisorChoices, writeSessionAdvisorChoices } from "./sessionAdvisorPreference";

function AdvisorProgress({ startedAt, launching }: { startedAt: number; launching: boolean }) {
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);
  const elapsed = Math.max(0, Math.floor((now - startedAt) / 1000));
  return (
    <div
      role="status"
      aria-label="Advisor progress"
      className="flex items-center gap-3 rounded-lg border border-brand-accent/25 bg-brand-accent/5 px-3 py-3 text-brand-accent"
    >
      <Clock3Icon className="size-5 shrink-0 animate-pulse" />
      <div>
        <p className="text-base font-semibold tabular-nums">
          {elapsed}s elapsed · {launching ? "Starting your chat" : "Advisor choosing a model"}
        </p>
        <p className="text-xs text-muted-foreground">
          {launching
            ? "The model is chosen. Connecting to start your answer."
            : elapsed >= 30
              ? "Still waiting for the Advisor. No task has been sent to the answer model yet."
              : "Your message is submitted. The answer starts after the model is chosen."}
        </p>
      </div>
    </div>
  );
}

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

export interface AdvisorSubmitHandle {
  /** True means Advisor owns this send, including loading/error/review states. */
  submit: () => boolean;
}

export interface NewChatAdvisorSectionProps {
  submitRef?: Ref<AdvisorSubmitHandle>;
  submissionBlockReason?: string | null;
  /** Reserved for choices that need explicit approval before execution. */
  requireConfirmation?: boolean;
  hostId: string | null;
  task: string;
  humanPick: HumanModelPick | null;
  launchAgentId: string | null;
  launchWorkspace: string | null;
  continueSessionId?: string | null;
  onFlowStateChange?: (busy: boolean, reviewVisible: boolean) => void;
  advisorModelTarget?: HTMLElement | null;
  keepChosenModel?: boolean;
  taskTags?: string[];
  selectorsOnly?: boolean;
  feedbackTarget?: HTMLElement | null;
  enabledOverride?: boolean;
  autoSubmit?: boolean;
  /** Render persistent recommender/settings controls. Chat follow-ups disable this. */
  showComposerControls?: boolean;
  /** Optional externally controlled settings state and portal for compact chat integration. */
  settingsOpenOverride?: boolean;
  settingsPanelTarget?: HTMLElement | null;
  disabled?: boolean;
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
  const {
    submitRef,
    submissionBlockReason,
    requireConfirmation = false,
    hostId,
    task,
    humanPick: suppliedHumanPick,
    launchAgentId,
    launchWorkspace,
    continueSessionId = null,
    onFlowStateChange,
    advisorModelTarget,
    keepChosenModel = true,
    taskTags,
    selectorsOnly = false,
    feedbackTarget,
    enabledOverride,
    autoSubmit = true,
    showComposerControls = true,
    settingsOpenOverride,
    settingsPanelTarget,
    disabled = false,
    onLaunched,
  } = props;
  const [sessionHumanPick, setSessionHumanPick] = useState<HumanModelPick | null>(null);
  // The native composer is the execution-choice authority. Saved Advisor state is fallback only.
  const humanPick = suppliedHumanPick ?? sessionHumanPick;
  const scope = hostId ?? "";
  const [localSettingsOpen, setSettingsOpen] = useState(false);
  const settingsOpen = settingsOpenOverride ?? localSettingsOpen;
  const [editor, setEditor] = useState<EditorState>({
    saved: null,
    draft: null,
    dirty: false,
    error: null,
  });
  const [options, setOptions] = useState<readonly LogicalOption[]>([]);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [submittedAt, setSubmittedAt] = useState<number | null>(null);
  const [round, setRound] = useState<RoundFlowState>(IDLE_ROUND);
  const pollTimer = useRef<ReturnType<typeof setInterval> | null>(null);
  const mounted = useRef(true);
  const scopeToken = useRef({ generation: 0, host: "" });
  const inputGeneration = useRef(0);
  const submissionIdentity = useRef<string | null>(null);
  const submissionKey = useRef<string | null>(null);

  const savedAdvisor = options.find(
    (option) => option.choice_id === editor.draft?.advisor_choice_id,
  );
  const savedAdvisorUnavailable = Boolean(
    editor.draft?.advisor_choice_id && (!savedAdvisor || !savedAdvisor.available),
  );
  const advisorModelValue = savedAdvisor
    ? advisorModelKey(savedAdvisor)
    : (editor.draft?.advisor_choice_id ?? "");
  // Shared model presentation: the same row/group/route vocabulary the primary
  // picker renders, adapted to the advisor's logical (one row per checkpoint,
  // routes shown per row) persistence model.
  const advisorRows = useMemo(() => advisorModelRows(options), [options]);
  const advisorOptions = useMemo<ModelPickerOption[]>(() => {
    const result: ModelPickerOption[] = advisorRows.map((row) => ({
      id: row.modelId,
      selectionId: row.key,
      displayName: row.displayName,
      groupLabel: advisorGroupHeading(row.provider),
      disabledReason: row.available ? undefined : row.disabledReason,
      // The row stays a logical choice; the route line carries the transport
      // information instead of minting one row per lane.
      description: row.available ? row.routeSummary : undefined,
      keywords: row.keywords,
    }));
    if (!savedAdvisor && editor.draft?.advisor_choice_id) {
      result.unshift({
        id: editor.draft.advisor_choice_id,
        selectionId: editor.draft.advisor_choice_id,
        displayName: "Saved advisor model unavailable",
        groupLabel: "Unavailable",
        disabledReason: "Choose an available recommender model explicitly",
      });
    }
    return result;
  }, [advisorRows, editor.draft?.advisor_choice_id, savedAdvisor]);
  const savedAdvisorRow = savedAdvisor
    ? (advisorRows.find((row) => row.key === advisorModelKey(savedAdvisor)) ?? null)
    : null;
  const savedAdvisorEffortOptions = useMemo(
    () => (savedAdvisorRow ? advisorEffortOptions(savedAdvisorRow.choices) : []),
    [savedAdvisorRow],
  );

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
    setSessionHumanPick(null);
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
        setOptions(currentModelChoices(toLogicalOptions(catalog), (row) => row.model_id));
        const saved = toSavedProviderPreferences(prefs);
        const sessionChoices = continueSessionId
          ? readSessionAdvisorChoices(hostId, continueSessionId)
          : null;
        setSessionHumanPick(sessionChoices?.humanPick ?? null);
        if (saved) {
          setEditor({
            saved,
            draft: continueSessionId
              ? {
                  ...(sessionChoices?.preferences ?? saved.preferences),
                  enabled: true,
                }
              : saved.preferences,
            dirty: Boolean(
              sessionChoices && !samePreferences(saved.preferences, sessionChoices.preferences),
            ),
            error: null,
          });
        } else {
          const empty = { ...emptyProviderPreferences(), enabled: true };
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
  }, [hostId, continueSessionId, isCurrentScope, stopPolling]);

  useEffect(() => {
    if (enabledOverride === undefined) return;
    setEditor((current) =>
      current.draft && current.draft.enabled !== enabledOverride
        ? { ...current, draft: { ...current.draft, enabled: enabledOverride } }
        : current,
    );
  }, [enabledOverride, editor.draft?.enabled]);

  useEffect(() => {
    if (hostId && continueSessionId && editor.draft) {
      writeSessionAdvisorChoices(hostId, continueSessionId, {
        preferences: editor.draft,
        humanPick,
      });
    }
  }, [hostId, continueSessionId, editor.draft, humanPick]);

  const resolveHumanChoice = useCallback((): string | null => {
    if (!humanPick || humanPick.model === "") return null;
    const matches = options.filter(
      (option) =>
        option.available &&
        option.model_ids.includes(humanPick.model) &&
        (humanPick.effort
          ? option.reasoning_effort === humanPick.effort
          : option.reasoning_effort === "not_applicable" ||
            (option.default_access_lanes ?? []).some(
              (lane) =>
                (humanPick.accessLane === null || lane === humanPick.accessLane) &&
                option.access_lanes.includes(lane),
            )),
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
    const resolvedHumanChoiceId = resolveHumanChoice();
    if (
      !resolvedHumanChoiceId ||
      !effectiveOptions(draft, options).some((option) => option.choice_id === resolvedHumanChoiceId)
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

  // Enabling with a never-configured draft must land in a coherent state:
  // the catalog's declared defaults seed the pool/recommender instead of a
  // validation error being the normal post-toggle state. Configured drafts
  // pass through unchanged.
  useEffect(() => {
    const draft = editor.draft;
    if (!draft || catalogError !== null || options.length === 0) return;
    const seeded = seedFreshAdvisorDraft(draft, options, resolveHumanChoice());
    if (seeded === null || seeded === draft || samePreferences(seeded, draft)) return;
    handleChange(seeded);
  }, [editor.draft, options, catalogError, resolveHumanChoice, handleChange]);

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
          setEditor({
            saved,
            draft: continueSessionId ? { ...saved.preferences, enabled: true } : saved.preferences,
            dirty: false,
            error: null,
          });
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
  }, [continueSessionId, editor.draft, editor.saved?.version, hostId, isCurrentScope]);

  const handlePropose = useCallback(() => {
    if (hostId === null || round.busy || !editor.draft || validation !== null) {
      if (validation !== null) setRound({ round: null, busy: false, error: validation });
      return;
    }
    if (submissionBlockReason) {
      setRound({ round: null, busy: false, error: submissionBlockReason });
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
    const resolvedHumanChoiceId = resolveHumanChoice();
    if (!resolvedHumanChoiceId || !submissionKey.current) {
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
    setSubmittedAt(Date.now());
    setRound({ round: null, busy: true, error: null });
    void (async () => {
      try {
        const dto = await createProviderRound(
          host,
          "default",
          task,
          resolvedHumanChoiceId,
          editor.draft!,
          submissionKey.current!,
          continueSessionId ? { sessionId: continueSessionId, keepChosenModel } : undefined,
          taskTags,
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
    continueSessionId,
    keepChosenModel,
    hostId,
    isCurrentScope,
    pollRound,
    resolveHumanChoice,
    round.busy,
    task,
    taskTags,
    submissionBlockReason,
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
            {
              agent_id: launchAgentId,
              workspace: launchWorkspace,
              ...(continueSessionId ? { continue_session_id: continueSessionId } : {}),
            },
            overrideId,
            reason,
          );
          if (!isCurrentScope(host, generation) || inputGeneration.current !== inputVersion) return;
          setRound({ round: dto, busy: false, error: dto.launch_error ?? null });
          if (dto.execution.session_id !== null) {
            if (editor.draft)
              writeSessionAdvisorChoices(host, dto.execution.session_id, {
                preferences: editor.draft,
                humanPick,
              });
            onLaunched(dto.execution.session_id);
          }
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
    [
      continueSessionId,
      editor.draft,
      humanPick,
      hostId,
      isCurrentScope,
      launchAgentId,
      launchWorkspace,
      onLaunched,
      round.busy,
      round.round,
    ],
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
  const assigned = options.find((option) => option.choice_id === review?.assigned_choice_id);
  const approvalPreferences = round.round?.decision_context?.preferences_snapshot ?? editor.draft;
  const approvalRequired = Boolean(
    review?.assigned_arm === "advisor" &&
    assigned &&
    (approvalPreferences?.providers[assigned.provider].approval_choice_ids?.includes(
      assigned.choice_id,
    ) ||
      approvalPreferences?.providers[assigned.provider].approval_model_ids?.includes(
        assigned.model_id,
      )),
  );
  const launchNeedsApproval = requireConfirmation || approvalRequired;
  const awaitingLaunch =
    round.round !== null &&
    (round.round.state === "awaiting_confirmation" || round.round.state === "dispatch_claimed");
  const reviewVisible = launchNeedsApproval && awaitingLaunch;
  const autoLaunchAttempts = useRef(new Set<string>());
  useEffect(() => {
    const current = round.round;
    if (
      launchNeedsApproval ||
      round.busy ||
      round.error ||
      current?.state !== "awaiting_confirmation" ||
      current.launch_error ||
      current.execution.uncertain
    )
      return;
    const identity = `${hostId}:${current.round_id}`;
    if (autoLaunchAttempts.current.has(identity)) return;
    autoLaunchAttempts.current.add(identity);
    handleConfirm(null, null);
  }, [launchNeedsApproval, round, hostId, launchAgentId, launchWorkspace, handleConfirm]);
  useEffect(() => {
    onFlowStateChange?.(
      round.busy || (!launchNeedsApproval && awaitingLaunch && !round.error),
      reviewVisible,
    );
  }, [
    onFlowStateChange,
    reviewVisible,
    round.busy,
    round.error,
    launchNeedsApproval,
    awaitingLaunch,
  ]);
  useImperativeHandle(
    submitRef,
    () => ({
      submit: () => {
        if (!editor.draft || catalogError !== null) {
          setRound({
            round: null,
            busy: false,
            error: catalogError ?? "Wait for advisor settings to load before sending.",
          });
          return true;
        }
        if (!editor.draft.enabled) return false;
        if (!round.busy && !awaitingLaunch) handlePropose();
        return true;
      },
    }),
    [catalogError, editor.draft, handlePropose, awaitingLaunch, round.busy],
  );
  // Follow-up Send has already requested review. Start it once after its
  // saved settings load; there is no second submission button in the dialog.
  const submissionFeedbackRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!continueSessionId && (round.busy || reviewVisible || round.error)) {
      submissionFeedbackRef.current?.scrollIntoView?.({ block: "nearest", behavior: "smooth" });
    }
  }, [continueSessionId, round.busy, reviewVisible, round.error]);
  const autoSubmittedIdentity = useRef<string | null>(null);
  useEffect(() => {
    if (
      !autoSubmit ||
      !continueSessionId ||
      !editor.draft ||
      !editor.draft.enabled ||
      validation !== null ||
      catalogError !== null
    )
      return;
    const identity = submissionIdentity.current;
    if (!identity || autoSubmittedIdentity.current === identity) return;
    autoSubmittedIdentity.current = identity;
    handlePropose();
  }, [autoSubmit, continueSessionId, editor.draft, validation, catalogError, handlePropose]);
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
  const controls = (
    <Button
      type="button"
      variant="ghost"
      size="sm"
      className="shrink-0 gap-2 px-1"
      role="switch"
      aria-label="Enable Advisor"
      aria-checked={editor.draft?.enabled ?? false}
      disabled={!editor.draft || round.busy || disabled}
      onClick={() => {
        if (!editor.draft) return;
        const next = { ...editor.draft, enabled: !editor.draft.enabled };
        handleChange(
          next.enabled
            ? (seedFreshAdvisorDraft(next, options, resolveHumanChoice()) ?? next)
            : next,
        );
      }}
    >
      <SparklesIcon className="size-4 text-muted-foreground" />
      Advisor
      <span
        aria-hidden="true"
        className={
          editor.draft?.enabled
            ? "relative h-4 w-7 rounded-full bg-foreground"
            : "relative h-4 w-7 rounded-full bg-muted"
        }
      >
        <span
          className={
            editor.draft?.enabled
              ? "absolute right-0.5 top-0.5 size-3 rounded-full bg-background"
              : "absolute left-0.5 top-0.5 size-3 rounded-full bg-background"
          }
        />
      </span>
    </Button>
  );
  const settingsPanel = settingsOpen ? (
    <ProviderSettingsPanel
      idPrefix={`model-advisor-${scope.replace(/[^A-Za-z0-9_-]/g, "-")}`}
      value={editor.draft}
      options={options}
      dirty={editor.dirty}
      busy={disabled || round.busy}
      enabledLocked={Boolean(continueSessionId)}
      error={editor.error}
      onChange={handleChange}
      onSave={handleSave}
    />
  ) : null;
  const feedback = (
    <div className="space-y-3" data-testid="model-advisor-section">
      {editor.error && !settingsOpen ? <p role="alert">{editor.error}</p> : null}
      {feedbackTarget && !settingsPanelTarget ? (
        <>
          {validation && autoSubmit ? (
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={() => setSettingsOpen((open) => !open)}
            >
              Recommender settings
            </Button>
          ) : null}
          {settingsPanel}
        </>
      ) : null}
      {editor.draft?.enabled && (!continueSessionId || autoSubmit) ? (
        <div className="space-y-2" ref={submissionFeedbackRef}>
          {round.busy && submittedAt !== null ? (
            <AdvisorProgress
              startedAt={submittedAt}
              launching={
                round.round?.state === "awaiting_confirmation" || round.round?.state === "assigning"
              }
            />
          ) : null}
          {validation ? (
            <p role="status" className="text-sm text-muted-foreground">
              {validation}
            </p>
          ) : null}
        </div>
      ) : null}
      {reviewVisible && review ? (
        <ProviderAdvisorReview
          review={review}
          approvalRequired={approvalRequired}
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
          {!launchNeedsApproval && round.round?.state === "awaiting_confirmation" ? (
            <Button type="button" disabled={round.busy} onClick={() => handleConfirm(null, null)}>
              Retry launch
            </Button>
          ) : null}
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
      {requireConfirmation && round.round?.requested_execution ? (
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
      {requireConfirmation && round.round?.actual_execution ? (
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
  const recommenderControls = editor.draft ? (
    <div
      className="ml-auto flex min-w-0 items-center justify-end gap-1"
      data-testid="model-advisor-composer-choice"
    >
      <label
        hidden
        htmlFor={`model-advisor-${scope.replace(/[^A-Za-z0-9_-]/g, "-")}-advisor-model`}
        className="min-w-0 shrink-0 text-xs text-muted-foreground"
      >
        Recommender
      </label>
      <div className="flex min-w-0 items-center gap-1 [&>button:first-child]:w-20 min-[480px]:[&>button:first-child]:w-32 [&>button:first-child]:shrink-0">
        <SearchableModelPicker
          id={`model-advisor-${scope.replace(/[^A-Za-z0-9_-]/g, "-")}-advisor-model`}
          value={advisorModelValue}
          options={advisorOptions}
          loading={false}
          composer
          includeDefault={false}
          placeholder="Choose model…"
          ariaLabel="Recommender model"
          testId="model-advisor-advisor-choice"
          searchTestId="model-advisor-advisor-choice-search"
          disabled={disabled || round.busy || !editor.draft.enabled}
          onValueChange={(modelKey) => {
            const row = advisorRows.find((candidate) => candidate.key === modelKey);
            // Switching models reconciles the effort: keep the current one
            // when the new model offers it, else its declared default, else
            // the deterministic supported fallback.
            const choice = row
              ? reconcileAdvisorChoice(row.choices, savedAdvisor?.reasoning_effort ?? null)
              : null;
            if (editor.draft && choice)
              handleChange({ ...editor.draft, advisor_choice_id: choice.choice_id });
          }}
        />
        <ComposerEffortPicker
          value={savedAdvisor?.reasoning_effort ?? null}
          options={savedAdvisorEffortOptions.map((option) => ({
            value: option.value,
            label: option.label,
          }))}
          disabled={disabled || round.busy || !savedAdvisor || !editor.draft.enabled}
          label="Recommender reasoning effort"
          testIdPrefix="model-advisor-advisor"
          testId="model-advisor-advisor-effort"
          onSelect={(effort) => {
            if (!editor.draft || !savedAdvisorRow) return;
            const choice = savedAdvisorRow.choices.find(
              (option) => option.available && option.reasoning_effort === effort,
            );
            if (choice) handleChange({ ...editor.draft, advisor_choice_id: choice.choice_id });
          }}
        />
      </div>
      {savedAdvisorUnavailable ? (
        <p role="alert" className="col-span-2 text-xs text-destructive">
          The saved advisor model is unavailable from this host. Choose a valid model and reasoning
          level to continue.
        </p>
      ) : null}
    </div>
  ) : null;
  // Expand the shared composer selectors directly beneath the Advisor button.
  const composerControls = (
    <div className="flex h-14 w-full min-w-0 items-center gap-1">
      {controls}
      {recommenderControls}
      <span aria-hidden="true" className="size-8 shrink-0 md:size-7" />
      <Button
        type="button"
        size="icon-sm"
        variant="ghost"
        className="size-8 shrink-0 md:size-7"
        disabled={round.busy || disabled}
        onClick={() => setSettingsOpen((open) => !open)}
        aria-expanded={settingsOpen}
        aria-label="Recommender settings"
      >
        <SettingsIcon className="size-4" />
      </Button>
    </div>
  );
  return (
    <>
      {showComposerControls
        ? advisorModelTarget
          ? createPortal(selectorsOnly ? recommenderControls : composerControls, advisorModelTarget)
          : composerControls
        : null}
      {settingsPanelTarget ? (
        settingsPanel ? (
          createPortal(
            <div className="space-y-3">
              {!showComposerControls ? recommenderControls : null}
              {settingsPanel}
            </div>,
            settingsPanelTarget,
          )
        ) : null
      ) : settingsOpenOverride !== undefined ? null : feedbackTarget ? null : (
        <Dialog open={settingsOpen} onOpenChange={setSettingsOpen}>
          <DialogContent className="max-h-[85dvh] overflow-y-auto sm:max-w-2xl">
            <DialogHeader>
              <DialogTitle>Recommender settings</DialogTitle>
              <DialogDescription>
                Choose the Advisor model, allowed models and approval rules.
              </DialogDescription>
            </DialogHeader>
            {settingsPanel}
          </DialogContent>
        </Dialog>
      )}
      {feedbackTarget ? createPortal(feedback, feedbackTarget) : feedback}
    </>
  );
}
