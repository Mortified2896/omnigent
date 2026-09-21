/** Controlled, additive UI. Parent supplies authorized catalog, saved settings and API actions.
 * Not yet registered in NewChatDialog. Never executes models or edits global defaults.
 */
import { useId, useState } from "react";
import {
  type AdvisorOption,
  type AdvisorPreferences,
  optionLabel,
  toggleCandidate,
  validateEditor,
} from "./editor";

const controlClass = "rounded-md border border-input bg-background px-3 py-2 text-sm";
const buttonClass = "rounded-md border px-3 py-2 text-sm disabled:opacity-50";

export interface ModelAdvisorPanelProps {
  value: AdvisorPreferences | null;
  options: readonly AdvisorOption[];
  dirty: boolean;
  busy?: boolean;
  error?: string | null;
  humanCandidateId: string | null;
  onChange: (next: AdvisorPreferences) => void;
  onHumanChoice: (candidateId: string) => void;
  onSave: () => void;
  onPropose: () => void;
}

export function ModelAdvisorPanel(props: ModelAdvisorPanelProps) {
  const id = useId();
  const { value, options, busy = false } = props;
  if (!value) return <p role="status">Loading saved advisor settings…</p>;
  const known = new Set(options.map((option) => option.candidate_id));
  const unavailable = value.allowed_candidate_ids.filter((key) => !known.has(key));
  const problem = validateEditor(value, options);
  const humanReady =
    value.allowed_candidate_ids.includes(props.humanCandidateId ?? "") &&
    options.some((option) => option.candidate_id === props.humanCandidateId && option.available);
  return (
    <section aria-label="Model advisor" className="space-y-4 rounded-lg border p-4">
      <header>
        <h3 className="font-medium">Model advisor</h3>
        <p className="text-sm text-muted-foreground">
          One prompt. Two independent choices. One model runs.
        </p>
      </header>
      <fieldset disabled={busy} className="space-y-4">
        <label className="flex items-center gap-2">
          <input
            type="checkbox"
            checked={value.enabled}
            onChange={(event) => props.onChange({ ...value, enabled: event.target.checked })}
          />
          Compare my choice with the advisor
        </label>
        <details>
          <summary className="cursor-pointer text-sm">Allowed answers and advisor settings</summary>
          <div className="mt-3 space-y-3">
            <fieldset className="space-y-2">
              <legend className="text-sm font-medium">
                Allowed answers — shared by you and the advisor
              </legend>
              {options.map((option) => (
                <label key={option.candidate_id} className="flex items-start gap-2 text-sm">
                  <input
                    type="checkbox"
                    checked={value.allowed_candidate_ids.includes(option.candidate_id)}
                    disabled={
                      !option.available &&
                      !value.allowed_candidate_ids.includes(option.candidate_id)
                    }
                    onChange={(event) =>
                      props.onChange(
                        toggleCandidate(value, option.candidate_id, event.target.checked),
                      )
                    }
                  />
                  <span>
                    {optionLabel(option)}
                    {!option.available ? ` — ${option.unavailable_reason ?? "Unavailable"}` : ""}
                  </span>
                </label>
              ))}
              {unavailable.map((key) => (
                <label key={key} className="flex items-start gap-2 text-sm">
                  <input
                    type="checkbox"
                    checked
                    onChange={() => props.onChange(toggleCandidate(value, key, false))}
                  />
                  <span>Saved choice unavailable: {key}. Uncheck to remove.</span>
                </label>
              ))}
            </fieldset>
            <label htmlFor={`${id}-advisor`} className="block text-sm font-medium">
              Advisor model and reasoning level
            </label>
            <select
              id={`${id}-advisor`}
              className={controlClass}
              value={value.advisor_candidate_id ?? ""}
              onChange={(event) =>
                props.onChange({ ...value, advisor_candidate_id: event.target.value || null })
              }
            >
              <option value="">Choose the advisor…</option>
              {value.advisor_candidate_id && !known.has(value.advisor_candidate_id) ? (
                <option value={value.advisor_candidate_id} disabled>
                  Saved advisor unavailable
                </option>
              ) : null}
              {options.map((option) => (
                <option
                  key={option.candidate_id}
                  value={option.candidate_id}
                  disabled={!option.available}
                >
                  {optionLabel(option)}
                  {!option.available ? " — Unavailable" : ""}
                </option>
              ))}
            </select>
            <p className="text-xs text-muted-foreground">
              The advisor may be outside the allowed answer pool. Its call also uses the selected
              plan allowance.
            </p>
            <label htmlFor={`${id}-balance`} className="block text-sm">
              Decision balance: {value.human_probability_percent}% you /{" "}
              {100 - value.human_probability_percent}% advisor
            </label>
            <input
              id={`${id}-balance`}
              type="range"
              min={0}
              max={100}
              step={5}
              className="w-full"
              value={value.human_probability_percent}
              onChange={(event) =>
                props.onChange({ ...value, human_probability_percent: Number(event.target.value) })
              }
            />
          </div>
        </details>
        <div className="flex items-center gap-3">
          <button
            type="button"
            className={buttonClass}
            disabled={!props.dirty || problem !== null}
            onClick={props.onSave}
          >
            Save defaults
          </button>
          <span className="text-xs text-muted-foreground">
            {props.dirty
              ? "Changes apply to this round only until saved."
              : "Saved defaults apply to new chats."}
          </span>
        </div>
        {value.enabled ? (
          <>
            <label htmlFor={`${id}-human`} className="block text-sm font-medium">
              Your model and reasoning level
            </label>
            <select
              id={`${id}-human`}
              className={controlClass}
              value={props.humanCandidateId ?? ""}
              onChange={(event) => props.onHumanChoice(event.target.value)}
            >
              <option value="">Choose before seeing the recommendation…</option>
              {options
                .filter((option) => value.allowed_candidate_ids.includes(option.candidate_id))
                .map((option) => (
                  <option
                    key={option.candidate_id}
                    value={option.candidate_id}
                    disabled={!option.available}
                  >
                    {optionLabel(option)}
                  </option>
                ))}
            </select>
            <button
              type="button"
              className={buttonClass}
              disabled={problem !== null || !humanReady}
              onClick={props.onPropose}
            >
              {busy ? "Preparing recommendation…" : "Get recommendation"}
            </button>
            <p className="text-xs text-muted-foreground">
              This locks your pick for the round. Review the assignment before running; an override
              is recorded separately.
            </p>
          </>
        ) : null}
      </fieldset>
      {problem ? (
        <p role="status" className="text-sm">
          {problem}
        </p>
      ) : null}
      {props.error ? (
        <p role="alert" className="text-sm">
          {props.error}
        </p>
      ) : null}
    </section>
  );
}

export interface ReviewView {
  round_fingerprint: string;
  human_candidate_id: string;
  advisor_candidate_id: string;
  rationale: string;
  assigned_candidate_id: string;
  assigned_arm: "human" | "advisor" | "same";
  human_probability_percent: number;
}

export interface ModelAdvisorReviewProps {
  review: ReviewView;
  /** Frozen round choices, enriched with current availability by the server. */
  options: readonly AdvisorOption[];
  busy?: boolean;
  error?: string | null;
  onConfirm: (overrideId: string | null, reason: string | null) => void;
  onCancel: () => void;
}

export function ModelAdvisorReview(props: ModelAdvisorReviewProps) {
  // Internal key avoids retaining an override draft when the parent changes round.
  return <ReviewForRound key={props.review.round_fingerprint} {...props} />;
}

function ReviewForRound({
  review,
  options,
  busy = false,
  error,
  onConfirm,
  onCancel,
}: ModelAdvisorReviewProps) {
  const id = useId();
  const [override, setOverride] = useState(false);
  const [choice, setChoice] = useState(review.assigned_candidate_id);
  const [reason, setReason] = useState("");
  const label = (key: string) => {
    const option = options.find((row) => row.candidate_id === key);
    return option ? optionLabel(option) : `Unavailable selection (${key})`;
  };
  const executionId = override ? choice : review.assigned_candidate_id;
  const available = options.some(
    (option) => option.candidate_id === executionId && option.available,
  );
  return (
    <section aria-label="Review model assignment" className="space-y-3 rounded-lg border p-4">
      <h3 className="font-medium">Review before running</h3>
      <p className="text-sm">Your original choice: {label(review.human_candidate_id)}</p>
      <p className="text-sm">Advisor recommendation: {label(review.advisor_candidate_id)}</p>
      <p className="text-sm text-muted-foreground">{review.rationale}</p>
      <p className="text-sm font-medium">
        {review.assigned_arm === "same"
          ? "You agreed"
          : `Assignment: ${review.assigned_arm === "human" ? "your choice" : "advisor choice"}`}{" "}
        — {label(review.assigned_candidate_id)}
      </p>
      <p className="text-xs text-muted-foreground">
        Recorded balance: {review.human_probability_percent}% you /{" "}
        {100 - review.human_probability_percent}% advisor. Only the confirmed model runs.
      </p>
      <fieldset disabled={busy} className="space-y-3">
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={override}
            onChange={(event) => setOverride(event.target.checked)}
          />
          Override this assignment
        </label>
        {override ? (
          <>
            <label htmlFor={`${id}-override`} className="block text-sm">
              Run instead
            </label>
            <select
              id={`${id}-override`}
              className={controlClass}
              value={choice}
              onChange={(event) => setChoice(event.target.value)}
            >
              {options.map((option) => (
                <option
                  key={option.candidate_id}
                  value={option.candidate_id}
                  disabled={!option.available}
                >
                  {label(option.candidate_id)}
                </option>
              ))}
            </select>
            <label htmlFor={`${id}-reason`} className="block text-sm">
              Override reason
            </label>
            <input
              id={`${id}-reason`}
              className={controlClass}
              value={reason}
              maxLength={600}
              onChange={(event) => setReason(event.target.value)}
            />
            <p className="text-xs text-muted-foreground">
              The original draw stays in the record; this run is marked as manually overridden.
            </p>
          </>
        ) : null}
        <div className="flex gap-2">
          <button
            type="button"
            className={buttonClass}
            disabled={!available || (override && !reason.trim())}
            onClick={() => onConfirm(override ? choice : null, override ? reason.trim() : null)}
          >
            {busy ? "Starting…" : "Run selected model"}
          </button>
          <button type="button" className={buttonClass} onClick={onCancel}>
            Cancel round
          </button>
        </div>
      </fieldset>
      {!available ? (
        <p role="alert">The selected route is unavailable. Nothing will run automatically.</p>
      ) : null}
      {error ? <p role="alert">{error}</p> : null}
    </section>
  );
}
