import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@/lib/routing";
import { PageScroll } from "@/components/PageScroll";
import { Button } from "@/components/ui/button";
import { authenticatedFetch } from "@/lib/identity";

const dashboard = "https://sg-apps.taile0361b.ts.net:9443/availability";
interface Row {
  route: string;
  name: string;
  provider: string;
  effort: string;
  score: number | null;
  cost_class: string;
  status: string;
  account_ready: boolean;
  context_tokens: number | null;
  estimated_task_usd: number | null;
  last_test: { checked_at: number; message: string; tools_status?: string } | null;
}
interface Overview {
  rows: Row[];
  run: {
    status: string;
    completed: number;
    total: number;
    available: number;
    responding?: number;
  } | null;
  harness_observations: {
    route: string;
    effort: string;
    harness: string;
    outcome: string;
    tools_completed: boolean;
    checked_at: number;
  }[];
}
async function api<T>(path = "", body?: unknown): Promise<T> {
  const response = await authenticatedFetch(
    `/v1/routing/availability${path}`,
    body === undefined
      ? {}
      : {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        },
  );
  const data = await response.json();
  if (!response.ok)
    throw new Error(
      data.error?.message ??
        data.detail ??
        (typeof data.error === "string" ? data.error : "OmniRoute availability is unavailable"),
    );
  return data as T;
}
const when = (value?: number) => (value ? new Date(value * 1000).toLocaleString() : "Not checked");
export function ModelAvailabilityPage() {
  const client = useQueryClient();
  const [search, setSearch] = useState("");
  const [floor, setFloor] = useState(0);
  const [cost, setCost] = useState("free");
  const [showUnverified, setShowUnverified] = useState(false);
  const [limit, setLimit] = useState(50);
  const query = useQuery({
    queryKey: ["model-availability"],
    queryFn: () => api<Overview>(),
    refetchInterval: (q) => (q.state.data?.run?.status === "running" ? 3000 : 60000),
  });
  const refresh = useMutation({
    mutationFn: () =>
      api("/test", {
        cost_mode: "free",
        minimum_score: 0,
        include_unscored: true,
        retest: true,
        check_tools: false,
      }),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: ["model-availability"] });
      await client.invalidateQueries({ queryKey: ["tb4-preview"] });
    },
  });
  const data = query.data;
  const running = data?.run?.status === "running";
  const rows = (data?.rows ?? []).filter(
    (r) =>
      (showUnverified || (["available", "responding"].includes(r.status) && r.account_ready)) &&
      `${r.name} ${r.route}`.toLowerCase().includes(search.toLowerCase()) &&
      (floor === 0 || (r.score !== null && r.score >= floor)) &&
      (cost === "all" ||
        r.cost_class === "free" ||
        (cost === "included" && r.cost_class === "subscription")),
  );
  return (
    <PageScroll maxWidthClassName="max-w-5xl" contentClassName="px-4 sm:px-8">
      <div className="mb-5 flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold">Available models</h1>
          <p className="mt-2 text-sm text-muted-foreground">
            Shared OmniRoute results, highest TB4 first. Text responses, optional tool checks and
            observed harness turns are separate evidence.
          </p>
        </div>
        <Link to="/" className="text-sm underline">
          Back to new chat
        </Link>
      </div>
      <div className="mb-5 rounded-xl border border-border bg-card p-4">
        <div className="flex flex-wrap items-center gap-3">
          <Button disabled={running || refresh.isPending || !data} onClick={() => refresh.mutate()}>
            Refresh availability
          </Button>
          <Button variant="outline" onClick={() => query.refetch()}>
            Reload results
          </Button>
          <a href={dashboard} target="_blank" rel="noreferrer" className="text-sm underline">
            Tests, providers and quotas in OmniRoute ↗
          </a>
        </div>
        <p className="mt-3 text-xs text-muted-foreground">
          Refresh tests text responses on free routes without requiring tools. Configure other
          spending scopes and optional tool checks, inspect failures, or stop a scan in OmniRoute.
        </p>
        {data?.run && (
          <p role="status" className="mt-3 text-sm">
            {running ? "OmniRoute is testing" : "Last OmniRoute scan: " + data.run.status} ·{" "}
            {data.run.completed}/{data.run.total} processed ·{" "}
            {data.run.responding === undefined
              ? "text not measured (legacy scan)"
              : `${data.run.responding} text responses`}{" "}
            · {data.run.available} tool checks passed
          </p>
        )}
      </div>
      {(query.error || refresh.error) && (
        <p role="alert" className="mb-4 text-sm text-destructive">
          {query.error?.message ?? refresh.error?.message}
        </p>
      )}
      {query.isPending && <p>Loading shared availability…</p>}
      <div className="mb-4 grid gap-3 sm:grid-cols-3">
        <input
          aria-label="Search models"
          placeholder="Search model or provider…"
          value={search}
          onChange={(e) => {
            setSearch(e.target.value);
            setLimit(50);
          }}
          className="rounded-md border border-input bg-background p-2"
        />
        <label className="flex items-center gap-2 text-sm">
          Minimum TB4
          <input
            aria-label="Minimum TB4 score"
            type="number"
            min="0"
            max="100"
            value={floor}
            onChange={(e) => setFloor(Number(e.target.value))}
            className="min-w-0 flex-1 rounded-md border border-input bg-background p-2"
          />
        </label>
        <select
          aria-label="Cost filter"
          value={cost}
          onChange={(e) => setCost(e.target.value)}
          className="rounded-md border border-input bg-background p-2"
        >
          <option value="included">Free + included</option>
          <option value="free">Free only</option>
          <option value="all">All cost classes</option>
        </select>
      </div>
      <div className="mb-4 flex flex-wrap justify-between gap-3 text-xs text-muted-foreground">
        <span>{rows.length} routes · unscored models last</span>
        <label className="flex items-center gap-2">
          <input
            type="checkbox"
            checked={showUnverified}
            onChange={(e) => setShowUnverified(e.target.checked)}
          />
          Show unverified and failed routes
        </label>
      </div>
      {data && !rows.length && (
        <p className="rounded-lg border border-border p-5 text-sm">
          No verified model meets these filters. View all results in OmniRoute or refresh
          availability.
        </p>
      )}
      <div className="space-y-3">
        {rows.slice(0, limit).map((row) => {
          const observation = data?.harness_observations?.find(
            (h) => h.route === row.route && h.effort === row.effort,
          );
          return (
            <article
              key={row.route}
              data-testid="availability-row"
              className="grid gap-3 rounded-xl border border-border bg-card p-4 sm:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]"
            >
              <div>
                <div className="flex flex-wrap items-baseline gap-2">
                  <h2 className="break-words font-medium">{row.name}</h2>
                  <strong className="text-sm">
                    {row.score === null ? "TB4 —" : `TB4 ${row.score.toFixed(1)}%`}
                  </strong>
                </div>
                <p className="mt-1 break-all text-xs text-muted-foreground">
                  {row.route} · {row.effort}
                </p>
                <p className="mt-2 text-xs">
                  {row.provider} ·{" "}
                  {row.cost_class === "subscription" ? "Included subscription" : row.cost_class} ·
                  context {row.context_tokens?.toLocaleString() ?? "unknown"}
                </p>
                <p className="mt-1 text-xs text-muted-foreground">
                  Estimated 10k input + 1k output:{" "}
                  {row.estimated_task_usd === null
                    ? "unknown"
                    : row.estimated_task_usd === 0
                      ? "$0"
                      : `$${row.estimated_task_usd.toFixed(4)}`}
                </p>
              </div>
              <div>
                <p className="text-sm">
                  {row.status === "available"
                    ? "Text and tool check passed"
                    : row.status === "responding"
                      ? "Text response passed"
                      : row.status}
                  {!row.account_ready ? " · account unavailable" : ""}
                </p>
                {row.last_test?.tools_status && (
                  <p className="mt-1 text-xs text-muted-foreground">
                    Tool check: {row.last_test.tools_status.replaceAll("_", " ")}
                  </p>
                )}
                <p className="mt-1 text-xs text-muted-foreground">
                  Checked {when(row.last_test?.checked_at)}
                </p>
                <p className="mt-2 text-xs">
                  {observation
                    ? `${observation.harness}: ${observation.outcome.replaceAll("_", " ")}${observation.tools_completed ? " · tools completed" : ""}`
                    : "Harness: no observed client turn"}
                </p>
                {observation && (
                  <p className="text-xs text-muted-foreground">
                    Observed {when(observation.checked_at)}
                  </p>
                )}
              </div>
            </article>
          );
        })}
      </div>
      {rows.length > limit && (
        <Button className="mt-4" variant="outline" onClick={() => setLimit(limit + 50)}>
          Show more
        </Button>
      )}
      <p className="mt-5 text-xs text-muted-foreground">
        TB4 is a published benchmark prior, not a Codex success probability. Availability and quotas
        can change. Apply your TB4 floor and cost limits in the new-chat selector.
      </p>
    </PageScroll>
  );
}
