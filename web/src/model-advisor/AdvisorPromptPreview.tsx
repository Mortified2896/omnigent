import { useEffect, useRef, useState } from "react";

export function AdvisorPromptPreview({
  template,
  identity,
  onPreview,
  disabled,
}: {
  template: string;
  identity: string;
  onPreview?: () => Promise<string>;
  disabled: boolean;
}) {
  const [prompt, setPrompt] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const generation = useRef(0);
  useEffect(() => {
    generation.current += 1;
    setPrompt(null);
    setError(null);
    setBusy(false);
    return () => {
      generation.current += 1;
    };
  }, [identity]);
  return (
    <details className="rounded-lg border p-3 text-sm">
      <summary className="cursor-pointer font-medium">Prompt sent to the recommender</summary>
      <p className="my-2 text-xs text-muted-foreground">
        These instructions come from the server. Each request includes your message and the allowed
        model and reasoning combinations. Follow-ups also include the current model and reasoning
        level. Attachments and conversation history are not sent to the recommender.
      </p>
      {onPreview ? (
        <button
          type="button"
          disabled={disabled || busy}
          onClick={async () => {
            const version = generation.current;
            setBusy(true);
            setError(null);
            try {
              const result = await onPreview();
              if (version === generation.current) setPrompt(result);
            } catch (cause) {
              if (version === generation.current)
                setError(cause instanceof Error ? cause.message : "Could not preview the prompt.");
            } finally {
              if (version === generation.current) setBusy(false);
            }
          }}
        >
          {busy ? "Loading prompt…" : "Preview current request"}
        </button>
      ) : null}
      <p className="my-2 text-xs text-muted-foreground">
        {prompt
          ? "Current request preview. The server qualifies the candidates again when you send."
          : "Template — bracketed values are filled for each request."}
        {onPreview ? " Previewing does not call a model or spend inference tokens." : ""}
      </p>
      {error ? <p role="alert">{error}</p> : null}
      <pre
        aria-label="Recommender prompt"
        className="max-h-96 overflow-auto whitespace-pre-wrap break-words rounded bg-muted p-3 text-xs"
      >
        {prompt ?? template}
      </pre>
    </details>
  );
}
