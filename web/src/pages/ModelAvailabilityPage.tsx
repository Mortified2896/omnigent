import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@/lib/routing";
import { PageScroll } from "@/components/PageScroll";
import { Button } from "@/components/ui/button";
import { authenticatedFetch } from "@/lib/identity";

interface Policy {
  cost_mode: "free" | "included" | "metered";
  minimum_score: number;
  include_unscored: boolean;
  max_input_usd_per_million: number;
  max_output_usd_per_million: number;
  estimated_budget_usd: number;
  retest: boolean;
}
interface Row {
  route: string;
  provider: string;
  name: string;
  score: number | null;
  effort: string;
  source_url: string | null;
  cost_class: string;
  context_tokens: number | null;
  account_ready: boolean;
  enabled_accounts: number;
  status: string;
  input_usd_per_million: number | null;
  output_usd_per_million: number | null;
  estimated_task_usd: number | null;
  last_test: { checked_at: number; message: string; latency_ms: number; tools_ok: boolean } | null;
}
interface Provider {
  provider: string;
  enabled_accounts: number;
  disabled_accounts: number;
  account_ready: boolean;
  limits: Record<string, number>[];
  policy_limits: { unit: string; window: string; limit: number; source: string }[];
  usage: {
    window: string;
    remaining: number;
    total: number;
    account: number;
    observed_at: number;
    stale: boolean;
    source: string;
    reset_at: number | null;
  }[];
  quota: {
    account: number;
    scope: string;
    window: string;
    remaining_percent: number;
    observed_at: number;
    stale: boolean;
    reset_at: number | null;
  }[];
}
interface Run {
  id: string;
  status: string;
  total: number;
  completed: number;
  available: number;
  current: string[];
  skipped: Record<string, number>;
  estimated_probe_usd: number;
}
interface Overview {
  rows: Row[];
  providers: Provider[];
  catalogue_at: number;
  account_evidence_at: number | null;
  benchmark: { source_url: string; retrieved_at: string };
  run: Run | null;
}
interface Plan {
  targets: number;
  skipped: Record<string, number>;
  estimated_probe_usd: number;
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
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(
      error?.error?.message ??
        (typeof error?.error === "string" ? error.error : undefined) ??
        error?.detail ??
        "Availability request failed. Try again.",
    );
  }
  return response.json() as Promise<T>;
}
const money = (value: number | null) =>
  value === null ? "Unknown" : value === 0 ? "$0" : `$${value.toFixed(4)}`;
const when = (value: number | null | undefined) =>
  value ? new Date(value * 1000).toLocaleString() : "Not reported";
const statusName: Record<string, string> = {
  available: "Available · tools passed",
  responding: "Responds · tools unverified",
  failed: "Failed",
  limited: "Quota / rate limited",
  timeout: "Timed out",
  incompatible: "Compatibility failed",
  untested: "Untested",
  stale: "Test expired",
};

export function ModelAvailabilityPage() {
  const queryClient = useQueryClient();
  const [policy, setPolicy] = useState<Policy>({
    cost_mode: "included",
    minimum_score: 0,
    include_unscored: true,
    max_input_usd_per_million: 1,
    max_output_usd_per_million: 5,
    estimated_budget_usd: 1,
    retest: false,
  });
  const [search, setSearch] = useState("");
  const [provider, setProvider] = useState("");
  const [filter, setFilter] = useState("all");
  const [page, setPage] = useState(0);
  const overview = useQuery({
    queryKey: ["model-availability"],
    queryFn: () => api<Overview>(),
    refetchInterval: (query) => (query.state.data?.run?.status === "running" ? 3000 : 60000),
  });
  const data = overview.data;
  const running = data?.run?.status === "running";
  const planning = useQuery({
    queryKey: ["model-probe-plan", policy, data?.run?.completed],
    queryFn: () => api<Plan>("/plan", policy),
    enabled: !!data && !running,
  });
  const action = useMutation({
    mutationFn: ({ path, body }: { path: string; body: unknown }) => api(path, body),
    onSuccess: async (_, variables) => {
      if (variables.path === "/test") setFilter("all");
      await queryClient.invalidateQueries({ queryKey: ["model-availability"] });
      await queryClient.invalidateQueries({ queryKey: ["model-probe-plan"] });
      await queryClient.invalidateQueries({ queryKey: ["tb4-preview"] });
    },
  });
  const rows = useMemo(
    () =>
      (data?.rows ?? []).filter(
        (row) =>
          (!provider || row.provider === provider) &&
          `${row.route} ${row.name}`.toLowerCase().includes(search.toLowerCase()) &&
          (filter === "all" ||
            (filter === "available"
              ? row.status === "available" && row.account_ready
              : filter === "untested"
                ? ["untested", "stale"].includes(row.status)
                : !["available", "responding", "untested", "stale"].includes(row.status))),
      ),
    [data?.rows, provider, search, filter],
  );
  const ready =
    data?.rows.filter((row) => row.status === "available" && row.account_ready).length ?? 0;
  const currentPage = Math.min(page, Math.max(0, Math.ceil(rows.length / 50) - 1));
  const visible = rows.slice(currentPage * 50, (currentPage + 1) * 50);
  const inputClass = "mt-1 w-full rounded-md border border-input bg-background p-2";
  const update = (patch: Partial<Policy>) => setPolicy((old) => ({ ...old, ...patch }));
  return (
    <PageScroll
      maxWidthClassName="max-w-6xl"
      contentClassName="px-4 sm:px-8"
      data-testid="model-availability-page"
    >
      <div className="mb-6 flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold">Model availability</h1>
          <p className="mt-2 max-w-2xl text-sm text-muted-foreground">
            Test the OmniRoute pool and compare working routes by Terminal-Bench 4 score.
          </p>
        </div>
        <Link to="/" className="text-sm underline">
          Back to new chat
        </Link>
      </div>
      {overview.isPending && <p role="status">Loading providers and models…</p>}
      {overview.error && <p role="alert">{overview.error.message}</p>}
      {data && (
        <>
          <div className="mb-5 grid grid-cols-3 gap-2" aria-label="Availability summary">
            {[
              [ready, "Verified with tools"],
              [data.rows.length, "Model routes"],
              [data.providers.filter((p) => p.enabled_accounts > 0).length, "Active providers"],
            ].map(([count, label]) => (
              <div key={label} className="rounded-xl border border-border bg-card p-3 sm:p-4">
                <strong className="block text-2xl">{count}</strong>
                <span className="text-xs text-muted-foreground">{label}</span>
              </div>
            ))}
          </div>
          <section
            className="mb-6 rounded-xl border border-border bg-card p-4"
            aria-label="Test scope"
          >
            <h2 className="font-semibold">Test all providers</h2>
            <p className="mt-1 text-sm text-muted-foreground">
              Two providers at a time, one model per provider. Each check sends up to two short
              requests and simulates a harmless tool call. You can leave this page while it runs.
            </p>
            <fieldset
              disabled={running || action.isPending}
              className="mt-4 grid gap-4 sm:grid-cols-3"
            >
              <label className="text-sm">
                Test spending
                <select
                  aria-label="Test spending"
                  className={inputClass}
                  value={policy.cost_mode}
                  onChange={(e) => update({ cost_mode: e.target.value as Policy["cost_mode"] })}
                >
                  <option value="free">Free quota only</option>
                  <option value="included">Free + included subscriptions</option>
                  <option value="metered">Also test metered models</option>
                </select>
              </label>
              <label className="text-sm">
                Minimum TB4 score (%)
                <input
                  aria-label="Test minimum TB4"
                  type="number"
                  min={0}
                  max={100}
                  className={inputClass}
                  value={policy.minimum_score}
                  onChange={(e) => update({ minimum_score: Number(e.target.value) })}
                />
              </label>
              <div className="space-y-2 text-sm">
                <label className="flex gap-2">
                  <input
                    type="checkbox"
                    checked={policy.include_unscored}
                    onChange={(e) => update({ include_unscored: e.target.checked })}
                  />
                  Include models without a TB4 score
                </label>
                <label className="flex gap-2">
                  <input
                    type="checkbox"
                    checked={policy.retest}
                    onChange={(e) => update({ retest: e.target.checked })}
                  />
                  Retest models checked in the last 24 hours
                </label>
              </div>
              {policy.cost_mode === "metered" && (
                <>
                  <label className="text-sm">
                    Max input $ / million
                    <input
                      type="number"
                      min={0}
                      max={1000}
                      step={0.1}
                      className={inputClass}
                      value={policy.max_input_usd_per_million}
                      onChange={(e) =>
                        update({ max_input_usd_per_million: Number(e.target.value) })
                      }
                    />
                  </label>
                  <label className="text-sm">
                    Max output $ / million
                    <input
                      type="number"
                      min={0}
                      max={10000}
                      step={0.1}
                      className={inputClass}
                      value={policy.max_output_usd_per_million}
                      onChange={(e) =>
                        update({ max_output_usd_per_million: Number(e.target.value) })
                      }
                    />
                  </label>
                  <label className="text-sm">
                    Estimated batch budget ($)
                    <input
                      type="number"
                      min={0.01}
                      max={10}
                      step={0.1}
                      className={inputClass}
                      value={policy.estimated_budget_usd}
                      onChange={(e) => update({ estimated_budget_usd: Number(e.target.value) })}
                    />
                  </label>
                </>
              )}
            </fieldset>
            <div className="mt-4 flex flex-wrap items-center gap-3">
              <Button
                disabled={
                  running || action.isPending || planning.isFetching || !planning.data?.targets
                }
                onClick={() => action.mutate({ path: "/test", body: policy })}
              >
                Test all models in scope{planning.data ? ` (${planning.data.targets})` : ""}
              </Button>
              <Button
                variant="outline"
                disabled={action.isPending || running}
                onClick={() => action.mutate({ path: "/refresh", body: {} })}
              >
                Refresh catalogue
              </Button>
              {running && (
                <Button
                  variant="outline"
                  disabled={action.isPending}
                  onClick={() => action.mutate({ path: "/cancel", body: {} })}
                >
                  Stop testing
                </Button>
              )}
              {!running && planning.data && (
                <span className="text-sm text-muted-foreground">
                  Estimated test cost {money(planning.data.estimated_probe_usd)} · quotas still
                  apply
                </span>
              )}
            </div>
            {(action.error || planning.error) && (
              <p role="alert" className="mt-3 text-sm text-destructive">
                {action.error?.message ?? planning.error?.message}
              </p>
            )}
            {!running && planning.data && (
              <details className="mt-3 text-xs text-muted-foreground">
                <summary className="cursor-pointer">Why some models won’t be tested</summary>
                <ul className="mt-2 space-y-1">
                  {Object.entries(planning.data.skipped).map(([reason, count]) => (
                    <li key={reason}>
                      {reason}: {count}
                    </li>
                  ))}
                </ul>
              </details>
            )}
            <p className="mt-3 text-xs text-muted-foreground">
              Unknown prices are skipped. Budget estimates use up to 2,048 input and 512 output
              tokens per route; actual billing can differ. A batch stops after 45 minutes. Search
              and provider filters below only change the displayed results.
            </p>
            {data.run && (
              <div className="mt-4 rounded-lg bg-muted p-3 text-sm" role="status">
                <p className="font-medium">
                  {running ? "Testing" : `Last test: ${data.run.status}`} · {data.run.completed}/
                  {data.run.total} processed · {data.run.available} passed with tools
                </p>
                {running && (
                  <progress
                    aria-label="Model test progress"
                    className="mt-2 w-full"
                    max={data.run.total}
                    value={data.run.completed}
                  />
                )}
                {data.run.current.map((route) => (
                  <p key={route} className="mt-1 break-all text-xs">
                    {route}
                  </p>
                ))}
                {!!Object.keys(data.run.skipped).length && (
                  <details className="mt-2">
                    <summary>Skipped routes</summary>
                    {Object.entries(data.run.skipped).map(([reason, count]) => (
                      <p key={reason}>
                        {reason}: {count}
                      </p>
                    ))}
                  </details>
                )}
              </div>
            )}
          </section>
          <section aria-label="Available models">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h2 className="text-lg font-semibold">Models · highest TB4 first</h2>
              <span className="text-xs text-muted-foreground">
                {rows.length} results · 50 per page
              </span>
            </div>
            <div className="my-3 grid gap-3 sm:grid-cols-3">
              <input
                aria-label="Search models"
                placeholder="Search model or route…"
                className={inputClass}
                value={search}
                onChange={(e) => {
                  setSearch(e.target.value);
                  setPage(0);
                }}
              />
              <select
                aria-label="Filter provider"
                className={inputClass}
                value={provider}
                onChange={(e) => {
                  setProvider(e.target.value);
                  setPage(0);
                }}
              >
                <option value="">All providers</option>
                {data.providers.map((p) => (
                  <option key={p.provider} value={p.provider}>
                    {p.provider}
                  </option>
                ))}
              </select>
              <select
                aria-label="Filter availability"
                className={inputClass}
                value={filter}
                onChange={(e) => {
                  setFilter(e.target.value);
                  setPage(0);
                }}
              >
                <option value="all">All results</option>
                <option value="available">Available with tools</option>
                <option value="untested">Untested / expired</option>
                <option value="failed">Failed / limited</option>
              </select>
            </div>
            {!rows.length && (
              <p className="rounded-lg border border-border p-5 text-sm">
                No models match this view. Run a test or change the filters.
              </p>
            )}
            <div className="space-y-3">
              {visible.map((row) => (
                <article
                  key={row.route}
                  className="grid gap-3 rounded-xl border border-border bg-card p-4 sm:grid-cols-[minmax(0,2fr)_minmax(0,1fr)_minmax(0,1fr)]"
                  data-testid="availability-row"
                >
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-baseline gap-2">
                      <h3 className="break-words font-medium">{row.name}</h3>
                      <strong className="whitespace-nowrap text-sm">
                        {row.score === null ? "TB4 —" : `TB4 ${row.score.toFixed(1)}%`}
                      </strong>
                    </div>
                    <p className="mt-1 break-all text-xs text-muted-foreground">{row.route}</p>
                    <p className="mt-2 text-xs text-muted-foreground">
                      Effort: {row.effort} · Context:{" "}
                      {row.context_tokens?.toLocaleString() ?? "unknown"}
                    </p>
                    {row.source_url ? (
                      <a
                        href={row.source_url}
                        target="_blank"
                        rel="noreferrer"
                        className="text-xs underline"
                      >
                        Published benchmark configuration
                      </a>
                    ) : (
                      <p className="mt-1 text-xs text-muted-foreground">
                        No exact supported TB4 configuration
                      </p>
                    )}
                  </div>
                  <div className="text-sm">
                    <p className="font-medium">{statusName[row.status] ?? row.status}</p>
                    {!row.account_ready && (
                      <p className="mt-1 text-xs text-destructive">
                        Account unavailable or health evidence stale
                      </p>
                    )}
                    <p className="mt-1 text-xs text-muted-foreground">
                      {row.last_test?.message ?? "No response test yet"}
                    </p>
                    <p className="mt-1 text-xs text-muted-foreground">
                      Checked: {when(row.last_test?.checked_at)}
                      {row.last_test ? ` · ${(row.last_test.latency_ms / 1000).toFixed(1)}s` : ""}
                    </p>
                  </div>
                  <div className="text-sm">
                    <p className="font-medium">
                      {row.provider} ·{" "}
                      {row.cost_class === "subscription" ? "Included subscription" : row.cost_class}
                    </p>
                    <p className="mt-1 text-xs">
                      Estimated 10k input + 1k output: {money(row.estimated_task_usd)}
                    </p>
                    {row.cost_class === "metered" && (
                      <p className="mt-1 text-xs text-muted-foreground">
                        {money(row.input_usd_per_million)} input /{" "}
                        {money(row.output_usd_per_million)} output per million
                      </p>
                    )}
                    <p className="mt-1 text-xs text-muted-foreground">
                      Provider limits and remaining quota below
                    </p>
                  </div>
                </article>
              ))}
            </div>
            {rows.length > 50 && (
              <div className="my-4 flex items-center justify-between gap-3 text-sm">
                <Button
                  variant="outline"
                  disabled={currentPage === 0}
                  onClick={() => setPage(currentPage - 1)}
                >
                  Previous
                </Button>
                <span>
                  Page {currentPage + 1} / {Math.ceil(rows.length / 50)}
                </span>
                <Button
                  variant="outline"
                  disabled={(currentPage + 1) * 50 >= rows.length}
                  onClick={() => setPage(currentPage + 1)}
                >
                  Next
                </Button>
              </div>
            )}
          </section>
          <details className="mt-6 rounded-xl border border-border p-4">
            <summary className="cursor-pointer font-semibold">
              Provider policy and remaining usage
            </summary>
            <p className="my-3 text-xs text-muted-foreground">
              Configured limits are not remaining credits. Shared subscription quota cannot be
              converted reliably into a number of coding tasks. Missing limits do not mean unlimited
              use.
            </p>
            <div className="grid gap-3 sm:grid-cols-2">
              {data.providers.map((p) => (
                <div className="rounded-lg bg-muted p-3 text-sm" key={p.provider}>
                  <strong>{p.provider}</strong>
                  {p.policy_limits.map((limit) => (
                    <p key={JSON.stringify(limit)} className="mt-1 text-xs">
                      Policy: {limit.limit.toLocaleString()} {limit.unit} / {limit.window} (
                      {limit.source})
                    </p>
                  ))}
                  {p.usage.map((usage) => (
                    <p key={`${usage.account}-${usage.window}`} className="mt-1 text-xs">
                      Account {usage.account} · {usage.window}: {usage.remaining.toLocaleString()} /{" "}
                      {usage.total.toLocaleString()} remaining{usage.stale ? " (stale)" : ""} ·{" "}
                      {usage.source} · observed {when(usage.observed_at)} · reset{" "}
                      {when(usage.reset_at)}
                    </p>
                  ))}
                  <p>
                    {p.enabled_accounts} enabled · {p.disabled_accounts} disabled ·{" "}
                    {p.account_ready ? "Account ready" : "Unavailable / unverified"}
                  </p>
                  {p.limits.length ? (
                    p.limits.map((limits) => (
                      <p className="mt-1 text-xs" key={JSON.stringify(limits)}>
                        {Object.entries(limits)
                          .map(([k, v]) => `${k}: ${v}`)
                          .join(" · ")}
                      </p>
                    ))
                  ) : (
                    <p className="mt-1 text-xs">Configured request/token limits: not reported</p>
                  )}
                  {p.quota.length ? (
                    p.quota.map((q) => (
                      <p key={`${q.account}-${q.scope}-${q.window}`} className="mt-1 text-xs">
                        {q.window}: {q.remaining_percent}% remaining{q.stale ? " (stale)" : ""} ·
                        observed {when(q.observed_at)} · reset {when(q.reset_at)}
                      </p>
                    ))
                  ) : p.usage.length === 0 ? (
                    <p className="mt-1 text-xs">Remaining quota: not reported</p>
                  ) : null}
                </div>
              ))}
            </div>
          </details>
          <p className="mt-5 text-xs text-muted-foreground">
            Catalogue: {when(data.catalogue_at)} · account evidence:{" "}
            {when(data.account_evidence_at)}. Probe results expire after 24 hours and are a
            point-in-time check, not a guarantee. This list covers advertised text-model routes from
            configured providers; hidden models and virtual combos are excluded.
          </p>
          <p className="mt-2 text-xs text-muted-foreground">
            <a
              className="underline"
              href={data.benchmark.source_url}
              target="_blank"
              rel="noreferrer"
            >
              Terminal-Bench 4 / Artificial Analysis
            </a>
            , retrieved {data.benchmark.retrieved_at.slice(0, 10)}. Published mini-swe-agent results
            are a prior, not measured Codex success rates. Unscored models appear last.
          </p>
        </>
      )}
    </PageScroll>
  );
}
