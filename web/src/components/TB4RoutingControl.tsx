import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";

export interface TB4Policy {
  minimum_score: number;
  cost_mode: "free" | "included" | "metered";
  max_input_usd_per_million: number;
  max_output_usd_per_million: number;
  minimum_context_tokens: number;
  image_input: boolean;
}

interface Preview {
  eligible_routes: number;
  eligible_models: number;
  catalogue_routes: number;
  exclusions: Record<string, number>;
  eligible: {
    route: string;
    name: string;
    score: number;
    effort: string;
    cost_class: string;
    health: string;
    source_url: string;
  }[];
  benchmark: { retrieved_at: string; source_url: string };
}

export const defaultTB4Policy: TB4Policy = {
  minimum_score: 25,
  cost_mode: "included",
  max_input_usd_per_million: 1,
  max_output_usd_per_million: 5,
  minimum_context_tokens: 64000,
  image_input: false,
};

export function TB4RoutingControl({
  available,
  value,
  onChange,
}: {
  available: boolean;
  value: TB4Policy | null;
  onChange: (value: TB4Policy | null) => void;
}) {
  const status = useQuery({
    queryKey: ["tb4-routing-enabled"],
    queryFn: async () => {
      const response = await authenticatedFetch("/v1/routing/tb4");
      return response.ok ? ((await response.json()) as { enabled: boolean }) : { enabled: false };
    },
    staleTime: 60000,
  });
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timeout = setTimeout(() => setDebounced(value), 250);
    return () => clearTimeout(timeout);
  }, [value]);
  useEffect(() => {
    if (!available && value !== null) onChange(null);
  }, [available, value, onChange]);
  const preview = useQuery({
    queryKey: ["tb4-preview", debounced],
    enabled: status.data?.enabled === true && available && debounced !== null,
    queryFn: async ({ signal }) => {
      const response = await authenticatedFetch("/v1/routing/tb4/preview", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(debounced),
        signal,
      });
      if (!response.ok)
        throw new Error(
          "Route evidence is unavailable. Try again after routing health refresh succeeds.",
        );
      return (await response.json()) as Preview;
    },
    staleTime: 15000,
  });
  if (!status.data?.enabled || !available) return null;
  const update = (patch: Partial<TB4Policy>) =>
    onChange({ ...defaultTB4Policy, ...value, ...patch });
  const pending = value !== debounced || preview.isFetching;
  const data = pending ? undefined : preview.data;
  return (
    <section
      className="my-3 rounded-xl border border-border bg-card p-4 text-sm"
      aria-label="TB4 model selection"
    >
      <label className="flex cursor-pointer items-center gap-2 font-medium">
        <input
          type="checkbox"
          checked={value !== null}
          onChange={(event) => onChange(event.target.checked ? defaultTB4Policy : null)}
        />
        Choose by TB4 score and cost
        <span className="rounded bg-muted px-2 py-0.5 text-xs text-muted-foreground">Preview</span>
      </label>
      {value && (
        <div className="mt-4 space-y-4">
          <div className="grid gap-4 sm:grid-cols-2">
            <label className="space-y-2">
              <span className="flex justify-between">
                <span>Minimum TB4 score</span>
                <strong>{value.minimum_score}%</strong>
              </span>
              <input
                className="w-full accent-primary"
                aria-label="Minimum TB4 score"
                type="range"
                min="0"
                max="100"
                step="1"
                value={value.minimum_score}
                onChange={(event) => update({ minimum_score: Number(event.target.value) })}
              />
            </label>
            <label className="space-y-2">
              <span className="block">Acceptable cost</span>
              <select
                className="w-full rounded-md border border-input bg-background p-2"
                value={value.cost_mode}
                onChange={(event) =>
                  update({ cost_mode: event.target.value as TB4Policy["cost_mode"] })
                }
              >
                <option value="free">Free quota only</option>
                <option value="included">Free quota + included subscriptions</option>
                <option value="metered">Also allow metered models below my rates</option>
              </select>
            </label>
          </div>
          {value.cost_mode === "metered" && (
            <div className="grid gap-3 sm:grid-cols-2">
              <label>
                Maximum input $ / million tokens
                <input
                  className="mt-1 w-full rounded border border-input bg-background p-2"
                  type="number"
                  min="0"
                  max="1000"
                  step="0.1"
                  value={value.max_input_usd_per_million}
                  onChange={(event) =>
                    update({ max_input_usd_per_million: Number(event.target.value) })
                  }
                />
              </label>
              <label>
                Maximum output $ / million tokens
                <input
                  className="mt-1 w-full rounded border border-input bg-background p-2"
                  type="number"
                  min="0"
                  max="10000"
                  step="0.1"
                  value={value.max_output_usd_per_million}
                  onChange={(event) =>
                    update({ max_output_usd_per_million: Number(event.target.value) })
                  }
                />
              </label>
            </div>
          )}
          <p className="text-xs text-muted-foreground">
            Uses published Terminal-Bench 4 scores from mini-swe-agent as a prior for Codex. Scores
            are not a prediction of success on this task. Prices limit model token rates, not the
            total task bill. Unknown scores and costs are excluded. Each route also needs a recent
            successful Responses request.
          </p>
          <div aria-live="polite">
            {pending ? (
              <p>Checking the live OmniRoute pool…</p>
            ) : preview.error ? (
              <p role="alert" className="text-destructive">
                {preview.error.message}
              </p>
            ) : data ? (
              <>
                <p className="font-medium">
                  {data.eligible_models} models · {data.eligible_routes} eligible routes
                </p>
                {data.eligible.length ? (
                  <p className="mt-1 text-muted-foreground">
                    First choice: {data.eligible[0].name} via {data.eligible[0].route.split("/")[0]}
                    . Rechecked when you start.
                  </p>
                ) : (
                  <p className="mt-1 text-muted-foreground">
                    No verified route meets these limits. Unavailable or unverified routes stay
                    excluded even at a zero score floor. Try again later or change the cost policy.
                  </p>
                )}
                <details className="mt-3">
                  <summary className="cursor-pointer">
                    Inspect eligible routes and exclusions
                  </summary>
                  <div className="mt-2 max-h-64 overflow-auto">
                    <table className="w-full text-left text-xs">
                      <thead>
                        <tr>
                          <th className="p-1">Route</th>
                          <th className="p-1">TB4</th>
                          <th className="p-1">Cost / health</th>
                        </tr>
                      </thead>
                      <tbody>
                        {data.eligible.map((row) => (
                          <tr key={row.route} className="border-t border-border">
                            <td className="max-w-48 break-words p-1">
                              <a
                                href={row.source_url}
                                target="_blank"
                                rel="noreferrer"
                                className="underline"
                              >
                                {row.route}
                              </a>
                              <span className="block text-muted-foreground">{row.effort}</span>
                            </td>
                            <td className="p-1">{row.score.toFixed(1)}%</td>
                            <td className="p-1">
                              {row.cost_class}
                              <span className="block text-muted-foreground">{row.health}</span>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                    <ul className="mt-3 space-y-1 text-muted-foreground">
                      {Object.entries(data.exclusions).map(([reason, count]) => (
                        <li key={reason}>
                          {reason}: {count}
                        </li>
                      ))}
                    </ul>
                  </div>
                  <p className="mt-2 text-xs text-muted-foreground">
                    Source:{" "}
                    <a
                      className="underline"
                      href={data.benchmark.source_url}
                      target="_blank"
                      rel="noreferrer"
                    >
                      Artificial Analysis
                    </a>
                    , retrieved {data.benchmark.retrieved_at.slice(0, 10)}.
                  </p>
                </details>
              </>
            ) : null}
          </div>
        </div>
      )}
    </section>
  );
}
