import { useId, useState } from "react";

import type { LogicalOption } from "./providerPreferences";
import { effortLabel, PROVIDER_LABELS } from "./providerPreferences";

const controlClass = "rounded-md border px-3 py-2 text-sm";

export interface ProviderReviewView {
  round_fingerprint: string;
  human_choice_id: string;
  advisor_choice_id: string;
  rationale: string;
  assigned_choice_id: string;
  assigned_arm: "human" | "advisor" | "same";
  human_probability_percent: number;
}

interface Props {
  review: ProviderReviewView;
  options: readonly LogicalOption[];
  busy?: boolean;
  error?: string | null;
  onConfirm: (overrideId: string | null, reason: string | null) => void;
  onCancel: () => void;
}

export function ProviderAdvisorReview({
  review,
  options,
  busy = false,
  error,
  onConfirm,
  onCancel,
}: Props) {
  return (
    <ReviewForRound
      key={review.round_fingerprint}
      {...{ review, options, busy, error, onConfirm, onCancel }}
    />
  );
}

function ReviewForRound({ review, options, busy, error, onConfirm, onCancel }: Props) {
  const id = useId();
  const [override, setOverride] = useState(false);
  const [choice, setChoice] = useState(review.assigned_choice_id);
  const [reason, setReason] = useState("");
  const byId = new Map(options.map((option) => [option.choice_id, option]));
  const label = (choiceId: string) => {
    const option = byId.get(choiceId);
    return option
      ? `${PROVIDER_LABELS[option.provider]} · ${option.display_name} · ${effortLabel(option.reasoning_effort)}`
      : `Unavailable choice (${choiceId})`;
  };
  const executionId = override ? choice : review.assigned_choice_id;
  const available = byId.get(executionId)?.available === true;
  return (
    <section aria-label="Review model assignment" className="space-y-3 rounded-lg border p-4">
      <h3 className="font-medium">Review before running</h3>
      <p className="text-sm">Your original choice: {label(review.human_choice_id)}</p>
      <p className="text-sm">Advisor recommendation: {label(review.advisor_choice_id)}</p>
      <p className="text-sm text-muted-foreground">{review.rationale}</p>
      <p className="text-sm font-medium">
        {review.assigned_arm === "same"
          ? "You agreed"
          : `Assignment: ${review.assigned_arm === "human" ? "your choice" : "advisor choice"}`}{" "}
        — {label(review.assigned_choice_id)}
      </p>
      <p className="text-xs text-muted-foreground">
        Recorded balance: {review.human_probability_percent}% you /{" "}
        {100 - review.human_probability_percent}% advisor. Only the confirmed logical choice runs.
      </p>
      <fieldset disabled={busy} className="space-y-3">
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={override}
            onChange={(event) => setOverride(event.currentTarget.checked)}
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
              onChange={(event) => setChoice(event.currentTarget.value)}
            >
              {options.map((option) => (
                <option
                  key={option.choice_id}
                  value={option.choice_id}
                  disabled={!option.available}
                >
                  {label(option.choice_id)}
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
              onChange={(event) => setReason(event.currentTarget.value)}
            />
            <p className="text-xs text-muted-foreground">
              The original draw stays in the record; this run is marked as manually overridden.
            </p>
          </>
        ) : null}
        <div className="flex gap-2">
          <button
            type="button"
            className={controlClass}
            disabled={!available || (override && !reason.trim())}
            onClick={() => onConfirm(override ? choice : null, override ? reason.trim() : null)}
          >
            {busy ? "Starting…" : "Run selected model"}
          </button>
          <button type="button" className={controlClass} onClick={onCancel}>
            Cancel round
          </button>
        </div>
      </fieldset>
      {!available ? (
        <p role="alert">
          The selected logical choice is unavailable. Nothing will run automatically.
        </p>
      ) : null}
      {error ? <p role="alert">{error}</p> : null}
    </section>
  );
}
