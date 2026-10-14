import { advisorAttachmentIdentity, encodeAdvisorAttachments } from "@/lib/advisorAttachments";
import { PromptTimingStatus } from "@/components/composer/PromptTimingStatus";
import { startPromptTiming, handoffPromptTiming, type PromptTiming } from "@/lib/promptTiming";
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
import { SettingsIcon, SparklesIcon } from "lucide-react";
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
  previewAdvisorPrompt,
  fetchPreferences,
  fetchRound,
  saveProviderPreferences,
  toLogicalOptions,
  toSavedProviderPreferences,
  type RoundDto,
} from "@/lib/modelAdvisorApi";
import {
  activeChoiceIds,
  emptyProviderPreferences,
  effectiveOptions,
  providerSelection,
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

const POLL_INTERVAL_MS = 1500;
const POLL_TIMEOUT_MS = 180_000;
const CURRENT_MODEL_KEY = "__current_model__";
const JEV_ROUTER_KEY = "__jev_router__";

function newSubmissionKey(): string {
  if (typeof globalThis.crypto?.randomUUID === "function") return globalThis.crypto.randomUUID();
  return `submission-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

export interface HumanModelPick {
  model: string;
  accessLane: string | null;
  effort: string;
}

export interface AdvisorExecutionChoice {
  modelIds: readonly string[];
  accessLanes: readonly string[];
  defaultAccessLanes: readonly string[];
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
  files?: File[];
  humanPick: HumanModelPick | null;
  launchAgentId: string | null;
  launchWorkspace: string | null;
  launchProjectId?: string | null;
  launchLabels?: Record<string, string>;
  continueSessionId?: string | null;
  onFlowStateChange?: (busy: boolean, reviewVisible: boolean) => void;
  onCurrentModelAdvisorChange?: (current: boolean) => void;
  onExecutionChoicesChange?: (
    value: { enabled: boolean; choices: readonly AdvisorExecutionChoice[] } | null,
  ) => void;
  confirmationBlockReason?: string | null;
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

const NO_FILES: File[] = [];

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
    files = NO_FILES,
    humanPick: suppliedHumanPick,
    launchAgentId,
    launchWorkspace,
    launchProjectId,
    launchLabels,
    continueSessionId = null,
    onFlowStateChange,
    onCurrentModelAdvisorChange,
    onExecutionChoicesChange,
    confirmationBlockReason,
    advisorModelTarget,
    keepChosenModel = false,
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
  const [promptTemplate, setPromptTemplate] = useState<string | null>(null);
  const [options, setOptions] = useState<readonly LogicalOption[]>([]);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const savingRef = useRef(false);
  const [promptTiming, setPromptTiming] = useState<PromptTiming | null>(null);
  const [round, setRound] = useState<RoundFlowState>(IDLE_ROUND);
  const pollTimer = useRef<ReturnType<typeof setInterval> | null>(null);
  const completedLaunches = useRef(new Set<string>());
  const mounted = useRef(true);
  const scopeToken = useRef({ generation: 0, host: "" });
  const inputGeneration = useRef(0);
  const submissionIdentity = useRef<string | null>(null);
  const submissionKey = useRef<string | null>(null);

  const usesCurrentModel = editor.draft?.advisor_model_mode === "current";
  const usesJevRouter = editor.draft?.advisor_model_mode === "jev";
  const currentAdvisorChoices = useMemo(
    () => options.filter((option) => humanPick && option.model_ids.includes(humanPick.model)),
    [options, humanPick],
  );
  const selectedAdvisor = options.find(
    (option) => option.choice_id === editor.draft?.advisor_choice_id,
  );
  const savedAdvisor = usesCurrentModel
    ? currentAdvisorChoices.find(
        (option) => option.reasoning_effort === editor.draft?.advisor_reasoning_effort,
      )
    : selectedAdvisor;
  const savedAdvisorUnavailable = Boolean(
    !usesJevRouter &&
    (usesCurrentModel || editor.draft?.advisor_choice_id) &&
    (!savedAdvisor || !savedAdvisor.available),
  );
  const advisorModelValue = usesJevRouter
    ? JEV_ROUTER_KEY
    : usesCurrentModel
      ? CURRENT_MODEL_KEY
      : savedAdvisor
        ? advisorModelKey(savedAdvisor)
        : (editor.draft?.advisor_choice_id ?? "");
  // Shared model presentation: the same row/group/route vocabulary the primary
  // picker renders, adapted to the advisor's logical (one row per checkpoint,
  // routes shown per row) persistence model.
  const advisorRows = useMemo(
    () => advisorModelRows(options.filter((option) => option.provider !== "anthropic")),
    [options],
  );
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
    result.unshift({
      id: JEV_ROUTER_KEY,
      selectionId: JEV_ROUTER_KEY,
      displayName: "Jev Router",
      groupLabel: "Routing",
      description: "Current prompt · models enabled in Settings · OpenCode / OpenRouter",
      keywords: ["jev", "typesafe", "router", "routing", "openrouter"],
    });
    result.unshift({
      id: CURRENT_MODEL_KEY,
      selectionId: CURRENT_MODEL_KEY,
      displayName: "Current model",
      groupLabel: "This chat",
      description: currentAdvisorChoices[0]
        ? `${currentAdvisorChoices[0].display_name} · use existing chat context`
        : "Select an execution model first",
      disabledReason: currentAdvisorChoices.some((choice) => choice.available)
        ? undefined
        : "Select an available execution model first",
      keywords: ["current", "context", "cache"],
    });
    if (!usesCurrentModel && !usesJevRouter && !savedAdvisor && editor.draft?.advisor_choice_id) {
      result.unshift({
        id: editor.draft.advisor_choice_id,
        selectionId: editor.draft.advisor_choice_id,
        displayName: "Saved advisor model unavailable",
        groupLabel: "Unavailable",
        disabledReason: "Choose an available recommender model explicitly",
      });
    }
    return result;
  }, [
    advisorRows,
    editor.draft?.advisor_choice_id,
    savedAdvisor,
    usesCurrentModel,
    usesJevRouter,
    currentAdvisorChoices,
  ]);
  const savedAdvisorRow = savedAdvisor
    ? (advisorRows.find((row) => row.key === advisorModelKey(savedAdvisor)) ?? null)
    : null;
  const savedAdvisorEffortOptions = useMemo(
    () =>
      advisorEffortOptions(
        usesCurrentModel ? currentAdvisorChoices : (savedAdvisorRow?.choices ?? []),
      ),
    [savedAdvisorRow, usesCurrentModel, currentAdvisorChoices],
  );
  useEffect(() => {
    onCurrentModelAdvisorChange?.(usesCurrentModel);
  }, [onCurrentModelAdvisorChange, usesCurrentModel]);

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
    savingRef.current = false;
    setSaving(false);
    setEditor({ saved: null, draft: null, dirty: false, error: null });
    setRound(IDLE_ROUND);
    setSessionHumanPick(null);
    setOptions([]);
    setPromptTemplate(null);
    setCatalogError(null);
    if (hostId === null) return;
    let cancelled = false;
    void (async () => {
      try {
        const [catalogResult, prefsResult] = await Promise.allSettled([
          fetchCatalog(hostId),
          fetchPreferences(hostId),
        ]);
        if (cancelled || !isCurrentScope(hostId, generation)) return;
        // Preferences remain usable when model discovery fails: an explicit
        // Advisor-off choice must still allow ordinary composer submission.
        if (catalogResult.status === "fulfilled") {
          const catalog = catalogResult.value;
          setPromptTemplate(catalog.prompt_template ?? null);
          setOptions(currentModelChoices(toLogicalOptions(catalog), (row) => row.model_id));
        } else {
          setCatalogError(
            catalogResult.reason instanceof Error
              ? catalogResult.reason.message
              : "Couldn't load advisor models.",
          );
        }
        if (prefsResult.status === "rejected") {
          setCatalogError(
            prefsResult.reason instanceof Error
              ? prefsResult.reason.message
              : "Couldn't load advisor settings.",
          );
          setEditor({
            saved: null,
            draft: { ...emptyProviderPreferences(), enabled: true },
            dirty: false,
            error: null,
          });
          return;
        }
        const prefs = prefsResult.value;
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
        ? {
            ...current,
            draft: { ...current.draft, enabled: enabledOverride },
            dirty:
              !current.saved ||
              !samePreferences(current.saved.preferences, {
                ...current.draft,
                enabled: enabledOverride,
              }),
          }
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
    if (!usesJevRouter && (savedAdvisorUnavailable || !savedAdvisor)) {
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
  }, [
    editor.draft,
    options,
    resolveHumanChoice,
    savedAdvisor,
    savedAdvisorUnavailable,
    usesJevRouter,
  ]);

  const executionChoices = useMemo(() => {
    const draft = editor.draft;
    if (!draft) return null;
    const activeIds = new Set(activeChoiceIds(draft, options));
    return {
      enabled: draft.enabled,
      choices: draft.enabled
        ? options
            .filter((option) => activeIds.has(option.choice_id))
            .map((option) => ({
              modelIds: option.model_ids,
              accessLanes: option.access_lanes,
              defaultAccessLanes: option.default_access_lanes ?? [],
              effort: option.reasoning_effort,
            }))
        : [],
    };
  }, [editor.draft, options]);

  useEffect(() => {
    onExecutionChoicesChange?.(executionChoices);
  }, [executionChoices, onExecutionChoicesChange]);

  useEffect(() => {
    const identity = JSON.stringify({
      scope,
      task,
      attachments: advisorAttachmentIdentity(files),
      humanChoiceId: resolveHumanChoice(),
      preferences: editor.draft,
      continueSessionId,
      keepChosenModel,
      launchAgentId,
      launchWorkspace,
      launchProjectId,
      launchLabels,
    });
    if (submissionIdentity.current !== null && submissionIdentity.current !== identity) {
      inputGeneration.current += 1;
      stopPolling();
      setRound(IDLE_ROUND);
    }
    if (submissionIdentity.current !== identity) submissionKey.current = newSubmissionKey();
    submissionIdentity.current = identity;
  }, [
    editor.draft,
    resolveHumanChoice,
    scope,
    stopPolling,
    task,
    files,
    continueSessionId,
    keepChosenModel,
    launchAgentId,
    launchWorkspace,
    launchProjectId,
    launchLabels,
  ]);

  const openLaunchedChat = useCallback(
    (dto: RoundDto) => {
      const sessionId = dto.execution.session_id;
      if (hostId === null || sessionId === null || completedLaunches.current.has(dto.round_id))
        return;
      completedLaunches.current.add(dto.round_id);
      if (editor.draft)
        writeSessionAdvisorChoices(hostId, sessionId, {
          preferences: editor.draft,
          humanPick,
        });
      if (promptTiming) handoffPromptTiming(sessionId, promptTiming);
      onLaunched(sessionId);
    },
    [editor.draft, hostId, humanPick, onLaunched, promptTiming],
  );

  const pollRound = useCallback(
    (
      host: string,
      initial: RoundDto,
      startedAt: number,
      generation: number,
      inputVersion: number,
    ) => {
      stopPolling();
      let latest = initial;
      let inFlight = false;
      let finished = false;
      const tick = () =>
        void (async () => {
          if (inFlight || finished) return;
          inFlight = true;
          try {
            const dto = await fetchRound(host, initial.round_id);
            if (!isCurrentScope(host, generation) || inputGeneration.current !== inputVersion)
              return;
            latest = dto;
            if (
              dto.state === "advisor_pending" ||
              dto.state === "assigning" ||
              dto.state === "dispatch_claimed"
            ) {
              if (Date.now() - startedAt > POLL_TIMEOUT_MS) {
                finished = true;
                stopPolling();
                setRound({
                  round: dto,
                  busy: false,
                  error: "Your request is still processing. Check progress to reconnect.",
                });
              }
              return;
            }
            finished = true;
            stopPolling();
            setRound({ round: dto, busy: false, error: null });
            openLaunchedChat(dto);
          } catch (cause) {
            if (!isCurrentScope(host, generation) || inputGeneration.current !== inputVersion)
              return;
            // A failed read does not mean the server stopped. Keep the same
            // round and reconnect rather than dropping its launch identity.
            if (cause instanceof TypeError && Date.now() - startedAt <= POLL_TIMEOUT_MS) return;
            finished = true;
            stopPolling();
            setRound({
              round: latest,
              busy: false,
              error:
                cause instanceof TypeError
                  ? "Connection interrupted. Check progress to reconnect."
                  : cause instanceof Error
                    ? cause.message
                    : "Couldn't load the round.",
            });
          } finally {
            inFlight = false;
          }
        })();
      tick();
      pollTimer.current = setInterval(tick, POLL_INTERVAL_MS);
    },
    [isCurrentScope, openLaunchedChat, stopPolling],
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
    if (hostId === null || !editor.draft || savingRef.current) return;
    savingRef.current = true;
    setSaving(true);
    setEditor((current) => ({ ...current, error: null }));
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
        if (!saved) throw new Error("The server did not confirm saved defaults. Please try again.");
        if (isCurrentScope(host, generation))
          setEditor((current) => {
            const draft =
              current.draft && !samePreferences(current.draft, submitted)
                ? current.draft
                : continueSessionId
                  ? { ...saved.preferences, enabled: true }
                  : saved.preferences;
            return { saved, draft, dirty: !samePreferences(saved.preferences, draft), error: null };
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
                error:
                  "Saved settings changed elsewhere. Your choices are kept. Save defaults again to apply them.",
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
      } finally {
        if (isCurrentScope(host, generation)) {
          savingRef.current = false;
          setSaving(false);
        }
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
    if (task.trim() === "" && files.length === 0) {
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
    const key = submissionKey.current;
    setPromptTiming(startPromptTiming());
    setRound({ round: null, busy: true, error: null });
    void (async () => {
      try {
        const attachments = await encodeAdvisorAttachments(files);
        if (!isCurrentScope(host, generation) || inputGeneration.current !== inputVersion) return;
        const dto = await createProviderRound(
          host,
          "default",
          task.trim() ? task : "Review the attached files.",
          resolvedHumanChoiceId,
          editor.draft!,
          key,
          continueSessionId ? { sessionId: continueSessionId, keepChosenModel } : undefined,
          taskTags,
          attachments,
        );
        if (!isCurrentScope(host, generation) || inputGeneration.current !== inputVersion) return;
        setRound({ round: dto, busy: true, error: null });
        pollRound(host, dto, Date.now(), generation, inputVersion);
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
    files,
    submissionBlockReason,
    validation,
  ]);

  const handleConfirm = useCallback(
    (overrideId: string | null, reason: string | null) => {
      const host = hostId;
      let current = round.round;
      if (host === null || current === null || round.busy || confirmationBlockReason) return;
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
          // Retrying must first discover whether the original request already
          // created a chat. Never dispatch again from a stale review version.
          if (round.error || current.state === "dispatch_claimed") {
            current = await fetchRound(host, current.round_id);
            if (!isCurrentScope(host, generation) || inputGeneration.current !== inputVersion)
              return;
            setRound({ round: current, busy: false, error: null });
            if (current.execution.session_id !== null) {
              openLaunchedChat(current);
              return;
            }
            if (current.state !== "awaiting_confirmation") {
              setRound({ round: current, busy: true, error: null });
              pollRound(host, current, Date.now(), generation, inputVersion);
              return;
            }
          }
          const dto = await confirmRound(
            host,
            current.round_id,
            current.version,
            {
              agent_id: launchAgentId,
              workspace: launchWorkspace,
              ...(continueSessionId
                ? { continue_session_id: continueSessionId }
                : {
                    ...(launchProjectId ? { project_id: launchProjectId } : {}),
                    ...(launchLabels ? { labels: launchLabels } : {}),
                  }),
            },
            overrideId,
            reason,
          );
          if (!isCurrentScope(host, generation) || inputGeneration.current !== inputVersion) return;
          setRound({ round: dto, busy: false, error: dto.launch_error ?? null });
          openLaunchedChat(dto);
          if (dto.state === "dispatch_claimed" && !dto.launch_error && !dto.execution.uncertain) {
            setRound({ round: dto, busy: true, error: null });
            pollRound(host, dto, Date.now(), generation, inputVersion);
          }
        } catch (cause) {
          if (!isCurrentScope(host, generation) || inputGeneration.current !== inputVersion) return;
          // The confirmation response can be lost after the server launches.
          // Recover its authoritative binding before reporting a failure.
          try {
            const fresh = await fetchRound(host, current.round_id);
            if (!isCurrentScope(host, generation) || inputGeneration.current !== inputVersion)
              return;
            current = fresh;
            if (fresh.execution.session_id !== null) {
              setRound({ round: fresh, busy: false, error: null });
              openLaunchedChat(fresh);
              return;
            }
            if (fresh.state === "dispatch_claimed" && !fresh.execution.uncertain) {
              setRound({ round: fresh, busy: true, error: null });
              pollRound(host, fresh, Date.now(), generation, inputVersion);
              return;
            }
          } catch {
            // Preserve the original round so an explicit retry can reconnect.
          }
          if (!isCurrentScope(host, generation) || inputGeneration.current !== inputVersion) return;
          setRound({
            round: current,
            busy: false,
            error:
              cause instanceof TypeError
                ? "Connection interrupted while starting your chat. Check progress to reconnect."
                : cause instanceof Error
                  ? cause.message
                  : "Couldn't run the round.",
          });
        }
      })();
    },
    [
      confirmationBlockReason,
      continueSessionId,
      hostId,
      isCurrentScope,
      launchAgentId,
      launchWorkspace,
      launchProjectId,
      launchLabels,
      openLaunchedChat,
      pollRound,
      round.busy,
      round.error,
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
    ((approvalPreferences
      ? providerSelection(approvalPreferences, assigned.provider).approval_choice_ids
      : undefined
    )?.includes(assigned.choice_id) ||
      (approvalPreferences
        ? providerSelection(approvalPreferences, assigned.provider).approval_model_ids
        : undefined
      )?.includes(assigned.model_id)),
  );
  const launchNeedsApproval = requireConfirmation || approvalRequired;
  const awaitingLaunch =
    round.round !== null &&
    (round.round.state === "awaiting_confirmation" || round.round.state === "dispatch_claimed");
  const reviewVisible = (launchNeedsApproval || Boolean(confirmationBlockReason)) && awaitingLaunch;
  const autoLaunchAttempts = useRef(new Set<string>());
  useEffect(() => {
    const current = round.round;
    if (
      launchNeedsApproval ||
      confirmationBlockReason ||
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
  }, [
    launchNeedsApproval,
    confirmationBlockReason,
    round,
    hostId,
    launchAgentId,
    launchWorkspace,
    handleConfirm,
  ]);
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
        // Off is independent of catalog readiness. Never silently bypass an
        // enabled Advisor, but let users opt out even after a discovery error.
        if (enabledOverride === false || editor.draft?.enabled === false) return false;
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
    [catalogError, editor.draft, enabledOverride, handlePropose, awaitingLaunch, round.busy],
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
  const controls = (
    <Button
      type="button"
      variant="ghost"
      size="sm"
      className="shrink-0 gap-2 px-1"
      role="switch"
      aria-label="Enable Advisor"
      aria-checked={editor.draft?.enabled ?? false}
      disabled={!editor.draft || round.busy || saving || disabled}
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
  if (catalogError !== null)
    return (
      <div className="flex flex-wrap items-center gap-2 py-2">
        {showComposerControls ? controls : null}
        <p
          className="text-sm text-muted-foreground"
          role="status"
          data-testid="model-advisor-catalog-error"
        >
          Model advisor unavailable: {catalogError}.{" "}
          {editor.draft?.enabled
            ? "Turn Advisor off to send with your selected model."
            : "Messages will use your selected model."}
        </p>
      </div>
    );
  const settingsPanel = settingsOpen ? (
    <ProviderSettingsPanel
      idPrefix={`model-advisor-${scope.replace(/[^A-Za-z0-9_-]/g, "-")}`}
      value={editor.draft}
      options={options}
      promptTemplate={promptTemplate}
      promptIdentity={JSON.stringify([
        hostId,
        task,
        editor.draft,
        continueSessionId,
        keepChosenModel,
        humanPick,
        advisorAttachmentIdentity(files),
      ])}
      onPreviewPrompt={async () => {
        const humanChoiceId = resolveHumanChoice();
        if (!hostId || !editor.draft || !humanChoiceId)
          throw new Error("Choose allowed models before previewing the prompt.");
        const result = await previewAdvisorPrompt(
          hostId,
          task.trim() ? task : "[Your next message]",
          humanChoiceId,
          editor.draft,
          continueSessionId ? { sessionId: continueSessionId, keepChosenModel } : undefined,
          await encodeAdvisorAttachments(files),
        );
        return result.prompt;
      }}
      dirty={editor.dirty}
      busy={disabled || round.busy || saving}
      saving={saving}
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
          {round.busy && promptTiming !== null ? (
            <PromptTimingStatus
              timing={promptTiming}
              label="Advisor progress"
              stage={
                round.round?.state === "awaiting_confirmation" ||
                round.round?.state === "assigning" ||
                round.round?.state === "dispatch_claimed"
                  ? "Starting your chat"
                  : "Advisor choosing a model"
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
          confirmationBlockReason={confirmationBlockReason}
          error={round.round?.launch_error ?? round.error}
          onConfirm={handleConfirm}
          onCancel={handleCancel}
        />
      ) : null}
      {!reviewVisible && round.error !== null ? (
        <p className="text-sm text-destructive" role="alert">
          {round.error}
          {!launchNeedsApproval && round.round !== null && !round.round.execution.uncertain ? (
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
          disabled={disabled || round.busy || saving || !editor.draft.enabled}
          onValueChange={(modelKey) => {
            if (modelKey === JEV_ROUTER_KEY && editor.draft) {
              handleChange({
                ...editor.draft,
                advisor_model_mode: "jev",
                human_probability_percent: 0,
              });
              return;
            }
            if (modelKey === CURRENT_MODEL_KEY && editor.draft) {
              const choice = reconcileAdvisorChoice(
                currentAdvisorChoices,
                editor.draft.advisor_reasoning_effort ??
                  savedAdvisor?.reasoning_effort ??
                  humanPick?.effort,
              );
              if (choice)
                handleChange({
                  ...editor.draft,
                  advisor_model_mode: "current",
                  advisor_reasoning_effort: choice.reasoning_effort,
                });
              return;
            }
            const row = advisorRows.find((candidate) => candidate.key === modelKey);
            // Switching models reconciles the effort: keep the current one
            // when the new model offers it, else its declared default, else
            // the deterministic supported fallback.
            const choice = row
              ? reconcileAdvisorChoice(row.choices, savedAdvisor?.reasoning_effort ?? null)
              : null;
            if (editor.draft && choice)
              handleChange({
                ...editor.draft,
                advisor_model_mode: "selected",
                advisor_choice_id: choice.choice_id,
              });
          }}
        />
        {!usesJevRouter ? (
          <ComposerEffortPicker
            value={
              usesCurrentModel
                ? (editor.draft.advisor_reasoning_effort ?? null)
                : (savedAdvisor?.reasoning_effort ?? null)
            }
            options={savedAdvisorEffortOptions.map((option) => ({
              value: option.value,
              label: option.label,
            }))}
            disabled={
              disabled ||
              round.busy ||
              saving ||
              savedAdvisorEffortOptions.length === 0 ||
              !editor.draft.enabled
            }
            label="Recommender reasoning effort"
            testIdPrefix="model-advisor-advisor"
            testId="model-advisor-advisor-effort"
            onSelect={(effort) => {
              if (editor.draft && usesCurrentModel) {
                if (
                  currentAdvisorChoices.some(
                    (option) => option.available && option.reasoning_effort === effort,
                  )
                )
                  handleChange({ ...editor.draft, advisor_reasoning_effort: effort });
                return;
              }
              if (!editor.draft || !savedAdvisorRow) return;
              const choice = savedAdvisorRow.choices.find(
                (option) => option.available && option.reasoning_effort === effort,
              );
              if (choice) handleChange({ ...editor.draft, advisor_choice_id: choice.choice_id });
            }}
          />
        ) : null}
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
  const currentModelContextNote = usesJevRouter ? (
    <p className="text-sm text-muted-foreground" data-testid="model-advisor-jev-context">
      Jev Router receives the current prompt and the model and reasoning choices enabled in
      Settings. It uses the host’s OpenCode Zen or OpenRouter API key. Your selected connection and
      approval settings still apply.
    </p>
  ) : usesCurrentModel ? (
    <p className="text-sm text-muted-foreground" data-testid="model-advisor-current-context">
      {continueSessionId
        ? "The Advisor uses this chat’s current model and existing context. Its reasoning level is separate from the execution reasoning level. Changing reasoning can reduce cache reuse."
        : "The Advisor uses the selected execution model at the reasoning level chosen here. Existing chat context is available after the chat starts."}
    </p>
  ) : null;
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
              {currentModelContextNote}
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
            {currentModelContextNote}
            {settingsPanel}
          </DialogContent>
        </Dialog>
      )}
      {feedbackTarget ? createPortal(feedback, feedbackTarget) : feedback}
    </>
  );
}
