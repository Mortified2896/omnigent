import { createContext, useContext, useId, useState } from "react";
import { Button } from "@/components/ui/button";
import {
  REPORT_ACTION_CHOICES,
  reportActionRequest,
  type ReportAction,
  type ReportActionChoice,
} from "@/lib/reportActions";

export const ReportActionsContext = createContext<{
  sessionId: string;
  prepare: (request: string) => boolean;
} | null>(null);

export function ReportActions({
  actions,
  responseId,
}: {
  actions: ReportAction[];
  responseId: string;
}) {
  const context = useContext(ReportActionsContext);
  const id = useId();
  const [choices, setChoices] = useState<Record<number, ReportActionChoice>>({});
  const [status, setStatus] = useState<string | null>(null);
  if (!context || actions.length === 0) return null;
  return (
    <section
      aria-label="Report decisions"
      className="my-5 w-full rounded-xl border border-border bg-card p-4"
    >
      <h3 className="font-semibold">What would you like to do?</h3>
      <p className="mt-1 text-sm text-muted-foreground">
        Choose any findings to follow up on. Add your choices to the chat, then send when ready.
      </p>
      <div className="mt-4 space-y-4">
        {actions.map((action, index) => (
          <fieldset key={JSON.stringify([action.target, action.title])} className="space-y-2">
            <legend className="font-medium">{action.title}</legend>
            <p className="text-xs font-medium text-muted-foreground">{action.target}</p>
            <p className="text-sm">{action.why}</p>
            {action.source_url && (
              <a
                className="text-xs underline underline-offset-2"
                href={action.source_url}
                target="_blank"
                rel="noopener noreferrer"
              >
                Source
              </a>
            )}
            <div className="grid gap-2 sm:grid-cols-3">
              {REPORT_ACTION_CHOICES.map((choice) => (
                <label
                  key={choice.value}
                  className="flex cursor-pointer items-start gap-2 rounded-lg border border-border p-3 has-[:checked]:border-primary has-[:checked]:bg-primary/10"
                >
                  <input
                    type="radio"
                    name={`${id}-${index}`}
                    value={choice.value}
                    checked={choices[index] === choice.value}
                    className="mt-1 accent-primary"
                    onChange={() => {
                      setChoices((previous) => ({ ...previous, [index]: choice.value }));
                      setStatus(null);
                    }}
                  />
                  <span>
                    <span className="block text-sm font-medium">{choice.label}</span>
                    <span className="mt-1 block text-xs text-muted-foreground">
                      {choice.detail}
                    </span>
                  </span>
                </label>
              ))}
            </div>
          </fieldset>
        ))}
      </div>
      <div className="mt-4 flex flex-wrap items-center gap-3">
        <Button
          disabled={Object.keys(choices).length === 0}
          onClick={() => {
            const prepared = context.prepare(reportActionRequest(responseId, actions, choices));
            setStatus(
              prepared
                ? "Choices added to your draft. Edit them or press Send to start."
                : "Could not add choices. Reconnect or open a writable chat and try again.",
            );
          }}
        >
          Add choices to chat
        </Button>
        {status && (
          <p role="status" className="text-sm text-muted-foreground">
            {status}
          </p>
        )}
      </div>
    </section>
  );
}
