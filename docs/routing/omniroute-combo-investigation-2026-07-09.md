# OmniRoute Combos × Postgres Policy — Investigation Report

**Date:** 2026-07-09
**Scope:** Whether Pi (or any agent) can configure OmniRoute combos for
task-specific routing, and the cleanest architecture for storing a
quality-tier task → combo → model/provider/reasoning policy in Postgres.

This is **investigation only**. No code, no schema, no combos, no API
calls were created. Read-only against OmniRoute's source tree, the
live SQLite, and the existing Postgres migration files.

---

## 1. Headline answer

**Yes — partially, but the gap is small and addressable.** OmniRoute
has a production-grade "combo" system: 17 routing strategies, SQLite
persistence, HTTP API, CLI, MCP tools, WebSocket events, dashboard UI,
and a runtime engine with 11-factor live scoring. The live instance
has **0 combos configured today**, so we have a clean slate.

The **user-preference shift** ("not 100% free, use subscription freely,
reserve API-billed, never downgrade hard work") is achievable today
via a Postgres policy layer in front of OmniRoute — _not_ by changing
OmniRoute itself. Postgres stores the _intent_ (which combos exist,
which candidates are allowed, billing preference order); OmniRoute
executes the _contract_ (combo name → provider/model resolution with
the chosen strategy).

---

## 2. OmniRoute combo support — what is and isn't there

### 2.1 Combos exist as a first-class concept

| Concern            | Path                                                                            | Notes                                                                                  |
| ------------------ | ------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------- |
| Step schema        | `src/lib/combos/steps.ts:1-308`                                                 | `comboModelStep` (model + provider + connection) and `comboRefStep` (nested combo)     |
| Strategy enum      | `src/shared/constants/routingStrategies.ts:1-216`                               | 17 strategies                                                                          |
| Runtime engine     | `open-sse/services/combo.ts` (3386 lines)                                       | All dispatch + scoring                                                                 |
| DB persistence     | `src/lib/db/combos.ts` (~330 lines)                                             | `getCombos / createCombo / updateCombo / deleteCombo / reorderCombos / setActiveCombo` |
| Auto-combo scoring | `open-sse/services/autoCombo/{engine,scoring,virtualFactory,builtinCatalog}.ts` | 11-factor scoring                                                                      |
| Cascade resolver   | `open-sse/services/comboConfig.ts`                                              | Per-provider override                                                                  |
| Schema             | `src/shared/validation/schemas/combo.ts:243-262`                                | `createComboSchema`                                                                    |

**17 strategies** (verbatim from README:213-254):
`priority`, `fill-first`, `weighted`, `round-robin`, `p2c`, `least-used`,
`random`, `strict-random`, `cost-optimized`, `headroom`, `reset-window`,
`reset-aware`, `context-relay`, `context-optimized`, `lkgp`, `auto`,
`fusion`.

### 2.2 Where combos live

- **Storage:** SQLite table `combos` (live path
  `~/.omniroute/storage.sqlite`).
  Schema (live DB inspected at 2026-07-09):
  ```sql
  CREATE TABLE combos (
      id TEXT PRIMARY KEY,
      name TEXT NOT NULL UNIQUE,
      data TEXT NOT NULL,         -- JSON blob with strategy, models, config, etc.
      sort_order INTEGER NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      system_message TEXT DEFAULT NULL,
      tool_filter_regex TEXT DEFAULT NULL,
      context_cache_protection INTEGER DEFAULT 0
  );
  ```
  Plus related tables: `model_combo_mappings`, `combo_adaptation_state`,
  `routing_decisions.combo_id`, `context_handoffs`,
  `session_model_history`, `compression_combos`,
  `compression_combo_assignments`, `quota_combos`.
- **Live DB state:** `SELECT COUNT(*) FROM combos` → **0**.
  `activeComboId` key in `key_value` is `'null'`. Clean slate.
- **14 provider connections** already configured (live DB inspected):
  `cerebras`, `deepinfra`, `deepseek`, `gemini`, `groq`, `mimocode`,
  `minimax`, `mistral`, `nvidia`, `openrouter`, … (full list in
  `provider_connections`). All API-billed connectors via OpenRouter-style
  credentials.

### 2.3 How combos are configured (programmatic surfaces)

| Surface                                 | Path                                                                      | Auth                         | Safe to call from Pi?                                      |
| --------------------------------------- | ------------------------------------------------------------------------- | ---------------------------- | ---------------------------------------------------------- |
| HTTP API                                | `POST /api/combos` / `PATCH /api/combos/{id}` / `DELETE /api/combos/{id}` | management session           | **Yes, with user-managed session token**                   |
| Reorder                                 | `POST /api/combos/reorder`                                                | management                   | Yes                                                        |
| Test (live execution)                   | `POST /api/combos/test`                                                   | management                   | Read-only — Pi can call but it does make a real model call |
| Metrics                                 | `GET /api/combos/metrics`                                                 | management                   | Read-only                                                  |
| Builder options (catalog introspection) | `GET /api/combos/builder/options`                                         | management                   | Read-only                                                  |
| Auto-combo score                        | `POST /api/combos/auto`                                                   | management                   | Read-only                                                  |
| Public catalog (no auth)                | `GET /api/v1/combos`                                                      | **none** — OpenAI-compatible | **Read-only** and safe                                     |
| OpenCode token alias                    | `GET /api/v1/vscode/combos/{token}`                                       | token                        | Read-only                                                  |
| Active combo toggle                     | `PATCH /api/settings` (field `activeCombo`)                               | management                   | Yes                                                        |
| Cascade defaults                        | `GET/PATCH /api/settings/combo-defaults`                                  | management                   | Yes                                                        |
| CLI                                     | `omniroute combo list\|create\|delete\|switch\|suggest`                   | local socket                 | Yes                                                        |
| DB direct                               | import from `src/lib/db/combos.ts`                                        | in-process                   | No — would require running inside OmniRoute's Node process |

OpenAPI excerpt (`dist/docs/openapi.yaml:6654-6690`) declares the
`ComboCreate` shape with `name`, `model`, `strategy`, `nodes[]`. The
runtime Zod schema (`createComboSchema`) is richer: it accepts
discriminated `models[]` of `comboModelStep` / `comboRefStep` objects
plus a 100-key `config` object.

### 2.4 Feature matrix (vs. brief's checklist)

| #   | Feature                              | Supported?     | Where                                                                                                                                                                                                                                                                            |
| --- | ------------------------------------ | -------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | Priority strategy                    | ✅             | `src/domain/comboResolver.ts:38-40`                                                                                                                                                                                                                                              |
| 2   | Weighted per-step                    | ✅             | `src/shared/validation/schemas/combo.ts:11-15`                                                                                                                                                                                                                                   |
| 3   | Cost-optimized                       | ✅             | `open-sse/services/combo.ts:1503`                                                                                                                                                                                                                                                |
| 4   | Health / quota routing               | ✅             | `headroom`, `reset-aware`, `reset-window`, `quota-share`                                                                                                                                                                                                                         |
| 5   | Context-optimized routing            | ✅             | `context-optimized`, `context-relay`                                                                                                                                                                                                                                             |
| 6   | Fallback chains                      | ✅             | `src/domain/comboResolver.ts:99-104`                                                                                                                                                                                                                                             |
| 7   | **Billing-class filter (per-combo)** | ⚠️ **Partial** | `service_tier` exists on `usage_history`, `tier_config.ts` + `tierResolver.ts` exist at connection level; **no per-combo billing-class gate in the schema**. Workaround: enforce via `allowedProviders[]`.                                                                       |
| 8   | Model tags                           | ✅ Per-step    | `comboModelStepInputSchema.tags`                                                                                                                                                                                                                                                 |
| 9   | Provider tags                        | ✅             | `getConnectionRoutingTags()`                                                                                                                                                                                                                                                     |
| 10  | **Reasoning / effort (per-combo)**   | ⚠️ **Partial** | `reasoningTokenBuffer.ts` + `thinkingBudget.ts` exist; reasoning is **per-request via the chat executor**, not a per-combo knob. Workaround: set `reasoningTokenBufferEnabled: true` and `maxMessagesForSummary` per combo; pass `reasoning_effort` per-request from the runner. |
| 11  | Per-combo allowed provider/model     | ⚠️ **Partial** | `allowedProviders: z.array(z.string().max(200)).optional()` declared **but not wired into dispatch**; per-step `connectionId` + `allowedConnectionIds` work. Models addressed by string, no per-combo `allowedModels[]`.                                                         |
| 12  | Per-combo forbidden provider/model   | ❌             | No `forbidden*` schema field; no enforcement code.                                                                                                                                                                                                                               |

**Bonus features already in production** (the brief didn't ask):

- `auto` strategy with **11-factor live scoring** (quota, health,
  costInv, latencyInv, taskFit, stability, tierPriority, tierAffinity,
  specificityMatch, contextAffinity, resetWindowAffinity)
- `fusion` (parallel panel + judge)
- `lkgp` (Last-Known-Good-Path, sticky)
- `p2c` (power-of-two-choices)
- Nested combos (`combo-ref` step)
- Provider wildcards (`provider/*` step)
- Shadow routing
- SLA routing
- Eval-routing (gate on suite scores)
- Composite tiers
- Hedging / predictive TTFT
- Auto-promote (winning model → position 1)
- WebSocket real-time combo events

### 2.5 Auto-strategy scoring weights (the `auto` strategy)

```ts
scoringWeightsSchema = {
  quota,
  health,
  costInv,
  latencyInv,
  taskFit,
  stability,
  tierPriority: 0.05,
  tierAffinity: 0.05,
  specificityMatch: 0.05,
  contextAffinity: 0.08,
  resetWindowAffinity: 0,
};
```

This is exactly what we want for a quality-tier router. We can either
(1) let OmniRoute's `auto` strategy do the scoring and just declare
allowed providers + weights per combo, or (2) compute the score in
Omnigent and pin a `priority` strategy with explicit ordering. (1) is
preferred for hard combos; (2) for combos where we want deterministic
order.

---

## 3. Live OmniRoute read-only state

| Probe                                            | Result                                                                                                                   |
| ------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------ |
| `GET /`                                          | `307` redirect to `/login` (dashboard)                                                                                   |
| `GET /health`                                    | Returns the dashboard HTML (not a true health endpoint)                                                                  |
| `GET /api/health`                                | `401 AUTH_001` — authentication required                                                                                 |
| `GET /api/v1/health`                             | `401 AUTH_002` — authentication required                                                                                 |
| `GET /api/v1/models`                             | `401 AUTH_002` — authentication required                                                                                 |
| `GET /api/v1/combos`                             | `401 AUTH_002` — authentication required                                                                                 |
| `GET /api/v1/combos/builder/options`             | `401 AUTH_002` — authentication required                                                                                 |
| `~/.omniroute/storage.sqlite` (read-only sqlite) | 0 combos; 14 provider connections; virtual auto-aliases registered as `{provider: opencode, connectionId: noauth}` stubs |

**Interpretation:** the live OmniRoute is up, correctly auth-gated
(AUTH_001/AUTH_002 are proper responses, not bugs), and is at a
zero-combo baseline. There are **no public unauthenticated endpoints**
that leak combo or model data — `AUTH_002` is a closed door on
everything we probed. We did not attempt to bypass it.

`/api/v1/combos` is documented as the public catalog but it requires
auth in this build — consistent with the agent's read of
`dist/docs/openapi.yaml` which shows management-tier keys. We'll
need an `X-Management-Session` (or session cookie) for the write
paths. We did not extract or attempt to use any token.

---

## 4. Existing Postgres catalog state (read-only)

Database: `homelab` (HomeLab's PG). Connection via
`HOMELAB_DATABASE_URL` (psycopg2). Existing migrations
`001..006` in `HomeLab/scripts/migrations/`.

### 4.1 Tables that exist

| Table                        | Migration | Purpose                                     | Key columns                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| ---------------------------- | --------- | ------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `omniroute_model_catalog`    | 001       | Read-only mirror from OmniRoute TS registry | `provider_key, model_id, external_model_id, context_window, input_price_per_1m, output_price_per_1m, supports_tools, supports_vision, supports_json, supports_json_mode, supports_reasoning, tier, status, source, raw JSONB`                                                                                                                                                                                                                                                                                               |
| `free_provider_catalog`      | 002       | Deprecated legacy                           | Same shape as 004 but narrower                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| `provider_signup_tasks`      | 003       | Signup/browser-automation task queue        | `provider_key (FK→omniroute_provider_catalog after 004), task_type, status, manual_gate, last_browser_url, raw`                                                                                                                                                                                                                                                                                                                                                                                                             |
| `omniroute_provider_catalog` | **004**   | **Canonical** all-provider catalog          | `provider_key (UNIQUE), provider_source, free_tier_status, free_evidence_status, is_free_candidate, is_confirmed_free, signup_blocker, requires_credit_card, requires_phone, requires_chinese_phone, requires_wechat, requires_business_email, requires_invite, region_restricted, supports_api_key, supports_oauth, omniroute_registry_present, omniroute_runtime_present, omniroute_accuracy_status, needs_omniroute_correction, omniroute_trust_score, recommended_priority, recommended_action, model_count, raw JSONB` |
| `provider_research_evidence` | 006       | Search-result / page-fetch evidence         | `provider_key (FK), evidence_type, source_url, signals_* booleans, confidence, checked_by, browsed_by_human, raw JSONB`                                                                                                                                                                                                                                                                                                                                                                                                     |

### 4.2 Views that exist

| View                                  | Migration | Purpose                                              |
| ------------------------------------- | --------- | ---------------------------------------------------- |
| `omniroute_free_provider_candidates`  | 005       | Free-provider candidates                             |
| `omniroute_provider_research_queue`   | 005       | Prioritization queue                                 |
| `omniroute_provider_factcheck_export` | 005       | Flat export for ChatGPT fact-check                   |
| `omniroute_correction_queue`          | 005       | Correction/trust review queue                        |
| `free_provider_catalog_v`             | 004       | Compatibility shim over `omniroute_provider_catalog` |

### 4.3 Routing / combo tables

**No `omniroute_combo_*` tables exist** anywhere — clean slate.

Routing telemetry tables in **Control Room** (`control_room` DB,
separate from `homelab`):

- `coding_model_routing_settings` (0021)
- `routing_decision_panel_runs` (0023)
- `router_decision_runs` (0017)
- `router_recommendation_runs`
- `provider_usage_snapshots` (0022)
- `coding_runs` (0015)

These are **telemetry** tables (what the user saw, what they picked),
not policy tables. The proposed `routing_task_profiles`,
`omniroute_combo_profiles`, `omniroute_combo_candidates` are **new**.

### 4.4 What the existing catalogs already cover

| User wish                              | Covered? | Where                                                                    |
| -------------------------------------- | -------- | ------------------------------------------------------------------------ |
| Provider id                            | ✅       | `omniroute_provider_catalog.provider_key`                                |
| Model id                               | ✅       | `omniroute_model_catalog.model_id`                                       |
| Context length                         | ✅       | `omniroute_model_catalog.context_window`                                 |
| Coding capability                      | ⚠️       | `supports_tools` + `supports_json`; no explicit "coding_score"           |
| Reasoning support                      | ✅       | `supports_reasoning` boolean                                             |
| Free / sub / API-billed classification | ✅       | `free_tier_status` + `is_confirmed_free` + `omniroute_claimed_free`      |
| Quota info                             | ⚠️       | Not in Postgres; lives in OmniRoute runtime only                         |
| Provider health                        | ⚠️       | `omniroute_runtime_present` boolean; freshness not tracked               |
| Estimated quality / rank               | ⚠️       | `recommended_priority` integer (operator-curated, not measured)          |
| Source / evidence / confidence         | ✅       | `provider_source`, `provider_research_evidence.signals_*` + `confidence` |
| Last refreshed                         | ✅       | `last_checked_at`, `last_seen_at`, `last_compared_at`                    |

**Gap:** no per-(provider, model) quality scores in Postgres today. We
need to add them in the new `omniroute_combo_candidates` table (or
denormalize them into `omniroute_model_catalog`).

---

## 5. Recommended Postgres schema (minimal, no overkill)

These tables go in the **`homelab` DB** (next migration = `007_…`), not
the `omnigent` DB and not the `control_room` DB. Why:

- They live alongside the catalog research data.
- They are operator-curated, not generated by Omnigent runtime.
- They are read by Omnigent (via env-configured DB URL), not written
  by it. Omnigent stays free to use its own SQLite (`~/.omnigent/chat.db`).
- The ownership-split doc explicitly puts catalog data on the HomeLab
  side as read-only mirror + research data; this matches.

### 5.1 `routing_task_profiles`

A row per _task category_ (what the user is trying to do).

| Column                       | Type                                             | Notes                                                                                               |
| ---------------------------- | ------------------------------------------------ | --------------------------------------------------------------------------------------------------- |
| `id`                         | `uuid PK DEFAULT gen_random_uuid()`              |                                                                                                     |
| `slug`                       | `text NOT NULL UNIQUE`                           | `router_decision`, `planning_light`, `coding_standard`, `coding_max`, …                             |
| `display_name`               | `text NOT NULL`                                  |                                                                                                     |
| `description`                | `text`                                           |                                                                                                     |
| `default_harness`            | `text`                                           | e.g. `opencode-native`, `claude-native`, `codex-native`                                             |
| `default_combo_slug`         | `text REFERENCES omniroute_combo_profiles(slug)` |                                                                                                     |
| `default_reasoning_effort`   | `text`                                           | `low`, `medium`, `high`, `max`, `null`                                                              |
| `default_permission_mode`    | `text`                                           | `read_only`, `supervised`, `yolo`, etc.                                                             |
| `quality_tier`               | `text NOT NULL`                                  | `router`, `light`, `standard`, `strong`, `max`                                                      |
| `allowed_combos`             | `text[] NOT NULL DEFAULT '{}'`                   | Whitelist of combo slugs the task may route to                                                      |
| `allowed_billing_classes`    | `text[] NOT NULL DEFAULT '{free,subscription}'`  | Per the new policy: subscription is normal for serious work; API-billed only with explicit approval |
| `forbidden_billing_classes`  | `text[] NOT NULL DEFAULT '{api_billed,unknown}'` | Hard policy                                                                                         |
| `requires_explicit_approval` | `boolean NOT NULL DEFAULT false`                 | For `coding_max` etc.                                                                               |
| `is_enabled`                 | `boolean NOT NULL DEFAULT true`                  |                                                                                                     |
| `priority`                   | `integer NOT NULL DEFAULT 100`                   | Tiebreaker for router model                                                                         |
| `created_at`, `updated_at`   | `timestamptz DEFAULT now()`                      |                                                                                                     |

### 5.2 `omniroute_combo_profiles`

A row per _combo contract_. Independent from OmniRoute implementation
— Postgres is the authority; OmniRoute mirrors it.

| Column                       | Type                                                       | Notes                                                                |
| ---------------------------- | ---------------------------------------------------------- | -------------------------------------------------------------------- |
| `id`                         | `uuid PK DEFAULT gen_random_uuid()`                        |                                                                      |
| `slug`                       | `text NOT NULL UNIQUE`                                     | `router-cheap-low`, `coding-max`, …                                  |
| `display_name`               | `text NOT NULL`                                            |                                                                      |
| `purpose`                    | `text`                                                     |                                                                      |
| `quality_tier`               | `text NOT NULL`                                            | `router`, `light`, `standard`, `strong`, `max`                       |
| `strategy`                   | `text NOT NULL DEFAULT 'priority'`                         | OmniRoute strategy name                                              |
| `allowed_billing_classes`    | `text[] NOT NULL`                                          |                                                                      |
| `forbidden_billing_classes`  | `text[] NOT NULL`                                          |                                                                      |
| `billing_preference_order`   | `text[] NOT NULL DEFAULT '{free_equivalent,subscription}'` | Tells the ranker how to break ties                                   |
| `prefer_free_equivalent`     | `boolean NOT NULL DEFAULT true`                            | The new policy: free over sub **only when equivalent quality**       |
| `allow_subscription`         | `boolean NOT NULL DEFAULT true`                            |                                                                      |
| `allow_api_billed`           | `boolean NOT NULL DEFAULT false`                           | Default off per the new policy                                       |
| `requires_explicit_approval` | `boolean NOT NULL DEFAULT false`                           |                                                                      |
| `min_context_tokens`         | `integer NOT NULL DEFAULT 0`                               | Hard reject below this                                               |
| `requires_coding_capability` | `boolean NOT NULL DEFAULT false`                           |                                                                      |
| `requires_reasoning_support` | `boolean NOT NULL DEFAULT false`                           |                                                                      |
| `default_reasoning_effort`   | `text`                                                     | `low`, `medium`, `high`, `max`, `null`                               |
| `max_reasoning_effort`       | `text`                                                     | Cap                                                                  |
| `allow_provider_fallback`    | `boolean NOT NULL DEFAULT true`                            |                                                                      |
| `allow_paid_fallback`        | `boolean NOT NULL DEFAULT false`                           | Hard off per the new policy                                          |
| `allow_unknown_billing`      | `boolean NOT NULL DEFAULT false`                           | Hard off per the new policy                                          |
| `fallback_policy`            | `text NOT NULL DEFAULT 'same_quality_class'`               | `none`, `same_quality_class`, `same_model_only`, `downgrade_allowed` |
| `omniroute_combo_name`       | `text`                                                     | The name in OmniRoute's `combos` table (mirror target)               |
| `is_enabled`                 | `boolean NOT NULL DEFAULT true`                            |                                                                      |
| `created_at`, `updated_at`   | `timestamptz DEFAULT now()`                                |                                                                      |

### 5.3 `omniroute_combo_candidates`

Per-combo ordered list of provider/model/reasoning combinations,
with manual override columns.

| Column                     | Type                                                                      | Notes                                                                           |
| -------------------------- | ------------------------------------------------------------------------- | ------------------------------------------------------------------------------- |
| `id`                       | `uuid PK DEFAULT gen_random_uuid()`                                       |                                                                                 |
| `combo_profile_id`         | `uuid NOT NULL REFERENCES omniroute_combo_profiles(id) ON DELETE CASCADE` |                                                                                 |
| `provider_key`             | `text NOT NULL REFERENCES omniroute_provider_catalog(provider_key)`       |                                                                                 |
| `model_id`                 | `text NOT NULL`                                                           | Must match `omniroute_model_catalog.model_id` for the provider                  |
| `model_display_name`       | `text`                                                                    |                                                                                 |
| `reasoning_effort`         | `text`                                                                    | `low`, `medium`, `high`, `max`, `null`                                          |
| `billing_class`            | `text NOT NULL`                                                           | `free`, `subscription`, `api_billed`, `unknown`                                 |
| `is_free_equivalent`       | `boolean NOT NULL DEFAULT false`                                          | TRUE iff this candidate is the same model/quality as another candidate but free |
| `subscription_source`      | `text`                                                                    | `minimax-coding-plan`, `codex-subscription`, etc.                               |
| `context_tokens`           | `integer`                                                                 | Override or pull from `omniroute_model_catalog.context_window`                  |
| `coding_score`             | `numeric(4,3)`                                                            | 0.000–1.000, curated or measured                                                |
| `reasoning_score`          | `numeric(4,3)`                                                            |                                                                                 |
| `latency_score`            | `numeric(4,3)`                                                            |                                                                                 |
| `reliability_score`        | `numeric(4,3)`                                                            |                                                                                 |
| `cost_score`               | `numeric(4,3)`                                                            | Inverse of cost; higher = cheaper                                               |
| `quality_score`            | `numeric(4,3)`                                                            | Curated overall quality                                                         |
| `quota_score`              | `numeric(4,3)`                                                            | Higher = more quota headroom                                                    |
| `overall_rank`             | `integer NOT NULL DEFAULT 100`                                            | Curated order; 1 = best                                                         |
| `weight`                   | `integer NOT NULL DEFAULT 0`                                              | 0–100, fed to OmniRoute `weighted` strategy                                     |
| `manual_rank_boost`        | `integer NOT NULL DEFAULT 0`                                              | +N/-N override                                                                  |
| `hard_exclude`             | `boolean NOT NULL DEFAULT false`                                          | TRUE ⇒ skip even if other rules admit it                                        |
| `exclude_reason`           | `text`                                                                    | `unsupported_in_runtime`, `quota_tracking_failed`, …                            |
| `source`                   | `text NOT NULL DEFAULT 'manual'`                                          | `manual`, `auto_synth`, `imported`                                              |
| `confidence`               | `text NOT NULL DEFAULT 'medium'`                                          | `low`, `medium`, `high`                                                         |
| `manual_notes`             | `text`                                                                    |                                                                                 |
| `last_verified_at`         | `timestamptz`                                                             |                                                                                 |
| `created_at`, `updated_at` | `timestamptz DEFAULT now()`                                               |                                                                                 |
| UNIQUE                     | `(combo_profile_id, provider_key, model_id, reasoning_effort)`            |                                                                                 |

### 5.4 `routing_policy_versions` (optional but useful)

| Column                              | Type                             | Notes                       |
| ----------------------------------- | -------------------------------- | --------------------------- |
| `id`                                | `uuid PK`                        |                             |
| `version_name`                      | `text NOT NULL UNIQUE`           | `2026-07-09-r1`             |
| `description`                       | `text`                           |                             |
| `is_active`                         | `boolean NOT NULL DEFAULT false` | Only one row true at a time |
| `created_at`, `created_by`, `notes` | …                                |                             |

Used to snapshot the (task_profiles, combo_profiles, combo_candidates)
triple at a known good state. Useful for rollback after a bad change.

### 5.5 `routing_decision_logs` (later — Phase D, not MVP)

Per-decision audit row written by Omnigent when it submits a task.

| Column                                                    | Type                         | Notes                   |
| --------------------------------------------------------- | ---------------------------- | ----------------------- |
| `id`                                                      | `uuid PK`                    |                         |
| `conversation_id`, `message_id`                           | `text`                       |                         |
| `task_profile_slug`                                       | `text`                       |                         |
| `selected_harness`                                        | `text`                       |                         |
| `selected_combo_slug`                                     | `text`                       |                         |
| `selected_reasoning_effort`                               | `text`                       |                         |
| `selected_permission_mode`                                | `text`                       |                         |
| `router_model`                                            | `text`                       | e.g. `qwen3-coder:free` |
| `router_rationale`                                        | `text`                       |                         |
| `approved_by_user`                                        | `boolean`                    |                         |
| `actual_provider`, `actual_model`, `actual_billing_class` | `text`                       | From OmniRoute response |
| `fallback_attempts`                                       | `integer NOT NULL DEFAULT 0` |                         |
| `tokens_input`, `tokens_output`                           | `integer`                    |                         |
| `error`                                                   | `text`                       |                         |
| `created_at`                                              | `timestamptz DEFAULT now()`  |                         |

**Evaluation of overkill:** `routing_task_profiles`,
`omniroute_combo_profiles`, `omniroute_combo_candidates` are the MVP.
`routing_policy_versions` is useful but skippable for the first pass.
`routing_decision_logs` belongs in **Phase D** (after the panel +
router call works end-to-end).

---

## 6. Recommended quality-tier combo contracts

All `slug` values are stable identifiers we will reference from
`routing_task_profiles.allowed_combos`, from the router model's
prompt, and from the OmniRoute `combos.name`.

### 6.1 `router-cheap-low`

- **Purpose:** classify task + pick harness + combo + reasoning + permission + rationale
- **Strategy:** `auto` with weights biased toward schema reliability + cost safety
- **Quality tier:** `router`
- **Billing preference order:** `['free_equivalent', 'subscription']`
- **Allow API-billed:** false. **Allow subscription:** true. **Allow unknown:** false.
- **Requires reasoning support:** false
- **Default reasoning effort:** `low`
- **Min context tokens:** 16 000 (router prompts need room)
- **Fallback policy:** `same_quality_class` (downgrade only if router-class entirely empty)

### 6.2 `planning-light`

- **Purpose:** implementation plans, architecture sketches, task decomposition, prompt writing
- **Strategy:** `priority`
- **Quality tier:** `light`
- **Billing preference order:** `['free_equivalent', 'subscription']`
- **Default reasoning effort:** `low`/`medium`
- **Requires coding capability:** false (general reasoning OK)
- **Fallback policy:** `same_quality_class`

### 6.3 `coding-light`

- **Purpose:** small code questions, read-only debugging, simple edits, explanations
- **Strategy:** `priority`
- **Quality tier:** `light`
- **Default reasoning effort:** `low`
- **Requires coding capability:** true
- **Billing preference order:** `['free_equivalent', 'subscription']`
- **Fallback policy:** `same_quality_class`

### 6.4 `coding-standard`

- **Purpose:** default normal repo work; small-to-medium edits, tests, ordinary debugging
- **Strategy:** `priority` with curated order
- **Quality tier:** `standard`
- **Default reasoning effort:** `medium`
- **Requires coding capability:** true
- **Billing preference order:** `['free_equivalent', 'subscription']` (subscription strongly preferred; free only if equal quality)
- **Fallback policy:** `same_quality_class`

### 6.5 `coding-strong`

- **Purpose:** multi-file edits, tricky bugs, failing tests, refactors, tool-heavy work
- **Strategy:** `priority`
- **Quality tier:** `strong`
- **Default reasoning effort:** `medium`/`high`
- **Requires coding capability:** true
- **Requires reasoning support:** true
- **Billing preference order:** `['subscription', 'free_equivalent']` (subscription **strongly preferred**)
- **Fallback policy:** `same_quality_class`

### 6.6 `coding-max`

- **Purpose:** hardest tasks only — large architectural changes, severe production bugs, complex migrations, multi-service changes, repeated failures from lower combos
- **Strategy:** `priority`
- **Quality tier:** `max`
- **Default reasoning effort:** `high`/`max`
- **Requires coding capability:** true
- **Requires reasoning support:** true
- **Billing preference order:** `['subscription']` (API-billed still requires explicit approval)
- **Requires explicit approval:** true — panel copy: _"This uses the highest-quality / highest-reasoning combo."_
- **Fallback policy:** `downgrade_allowed` (down to `coding-strong` only)

### 6.7 `review-standard`

- **Purpose:** PR review, diff review, explanation, bug-risk analysis
- **Strategy:** `priority`
- **Quality tier:** `standard`
- **Default reasoning effort:** `medium`
- **Requires coding capability:** true (code-aware)
- **Billing preference order:** `['free_equivalent', 'subscription']`

### 6.8 `large-context`

- **Purpose:** huge files, long repo context, broad project review
- **Strategy:** `context-optimized`
- **Quality tier:** `strong` (context-first, then quality)
- **Min context tokens:** 200 000 (large-context threshold)
- **Default reasoning effort:** `medium`/`high` depending on risk
- **Requires coding capability:** true
- **Billing preference order:** `['subscription', 'free_equivalent']` (subscription large-context models preferred)
- **Fallback policy:** `none` — never fall back to a tiny-context model

---

## 7. Ranking formula and fields

The brief's proposed formula is right; here it is made explicit.

### 7.1 General formula

```
overall_score =
  0.25 * quality_score
+ 0.20 * coding_score        (zero if !requires_coding_capability)
+ 0.15 * context_score       (monotonic in context_tokens, capped at 1M)
+ 0.15 * reliability_score
+ 0.10 * quota_score
+ 0.10 * cost_safety_score   (per-combo weighting — see below)
+ 0.05 * latency_score
```

Then apply the billing preference order as a hard tiebreaker:

1. Drop any candidate with `billing_class` in `forbidden_billing_classes`.
2. Drop any candidate with `hard_exclude = true`.
3. Among remaining, sort by `overall_score DESC`.
4. If two candidates are within Δ of each other (e.g. 0.02) and the
   preferred one is "free-equivalent" of the other, prefer the free one
   (`prefer_free_equivalent = true` on the combo).
5. If `prefer_free_equivalent = true` AND the two are NOT equivalent
   (different model families), **do not** swap — quality wins.

### 7.2 Per-combo weight overrides

| Combo              | quality  | coding   | context  | reliability | quota | cost_safety | latency |
| ------------------ | -------- | -------- | -------- | ----------- | ----- | ----------- | ------- |
| `router-cheap-low` | 0.15     | 0.00     | 0.10     | 0.20        | 0.10  | **0.35**    | 0.10    |
| `planning-light`   | 0.25     | 0.10     | 0.15     | 0.15        | 0.10  | 0.15        | 0.10    |
| `coding-light`     | 0.20     | 0.20     | 0.10     | 0.15        | 0.10  | **0.20**    | 0.05    |
| `coding-standard`  | **0.30** | 0.20     | 0.15     | 0.15        | 0.10  | 0.05        | 0.05    |
| `coding-strong`    | **0.40** | **0.25** | 0.15     | 0.10        | 0.05  | 0.00        | 0.05    |
| `coding-max`       | **0.50** | 0.20     | 0.10     | 0.10        | 0.05  | 0.00        | 0.05    |
| `review-standard`  | 0.25     | 0.20     | 0.15     | 0.15        | 0.10  | 0.10        | 0.05    |
| `large-context`    | 0.25     | 0.15     | **0.40** | 0.10        | 0.05  | 0.00        | 0.05    |

These weights live in the **ranker** (Omnigent-side service or in the
prompt's instructions), not in Postgres. If we want to surface them
in Postgres for audit, add a `combo_ranker_weights` table later — but
that's a Phase D concern.

### 7.3 Manual override fields

Already in §5.3:

- `hard_exclude` + `exclude_reason` — never let this candidate be picked.
- `manual_rank_boost` — ±N applied to `overall_score` before sorting.
- `manual_notes` — free text the router/panel can show.

---

## 8. Postgres vs OmniRoute config — the storage split

| What                                     | Postgres     | OmniRoute config                                   |
| ---------------------------------------- | ------------ | -------------------------------------------------- |
| Task profiles                            | ✅ authority | — (mirrored if needed)                             |
| Combo contracts (intent)                 | ✅ authority | —                                                  |
| Provider/model candidates + scores       | ✅ authority | —                                                  |
| Manual overrides / `hard_exclude`        | ✅ authority | —                                                  |
| Policy versions                          | ✅           | —                                                  |
| Decision audit logs                      | ✅ (later)   | partial (`call_logs`, `routing_decisions`)         |
| Actual runnable combo name               | ❌           | ✅ authority (`combos.data`)                       |
| Concrete provider/model pool for a combo | ❌           | ✅ authority (steps inside `combos.data.models[]`) |
| Connection-level credentials             | ❌           | ✅ (`provider_connections`, `api_keys`)            |
| Per-call quota / health runtime state    | ❌           | ✅ (`usage_history`, `quota_snapshots`)            |
| WebSocket real-time push                 | ❌           | ✅                                                 |
| Active combo toggle                      | ❌           | ✅ (`key_value.activeComboId`)                     |

**Rule of thumb:** Postgres stores **intent + curation + audit**.
OmniRoute stores **execution + runtime state**. Omnigent reads
Postgres to plan, calls OmniRoute to execute.

---

## 9. How Pi could set this up safely

| Capability                                                     | Safe today?                           | What's needed                                                                                        |
| -------------------------------------------------------------- | ------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| Read OmniRoute source                                          | ✅                                    | —                                                                                                    |
| Inspect live OmniRoute SQLite (`~/.omniroute/storage.sqlite`)  | ✅ (read-only mode)                   | `sqlite3` CLI not installed; `python3 -m sqlite3` works                                              |
| Probe live HTTP endpoints                                      | ⚠️ read-only safe; auth-gated         | Need a management session token (not currently in env)                                               |
| Read-only `GET /api/combos`, `GET /api/combos/builder/options` | ⚠️ requires auth                      | Get token from user; never print it                                                                  |
| `POST /api/combos` (write)                                     | ❌ Not without explicit user approval | User must explicitly approve combo writes; Pi should print the proposed combo first and wait for ack |
| Run Postgres migrations                                        | ✅ for `homelab` DB                   | Need `HOMELAB_DATABASE_URL`; never print secret                                                      |
| Seed Postgres                                                  | ✅                                    | Same as above                                                                                        |
| Call `POST /api/combos/test` (live model call)                 | ❌                                    | This makes a real model call. Never auto-call.                                                       |
| Restart OmniRoute                                              | ❌                                    | User-driven only                                                                                     |

**Permissions Pi would need:**

- Read-only access to `~/.omniroute/storage.sqlite` (default user can do this).
- Read-only access to a `HOMELAB_DATABASE_URL` with a role that can `SELECT` from existing tables and `CREATE TABLE` for new migrations. (Not granted today — Pi must ask.)
- A user-supplied management session token (or a dedicated, scoped combo-write token) for the write API.

**What should always require explicit user approval:**

- Any `POST /api/combos`, `PATCH /api/combos/{id}`, `DELETE /api/combos/{id}` call.
- Any `POST /api/combos/test` (it makes a live model call).
- Any `PATCH /api/settings` that toggles `activeCombo`.
- Any Postgres `INSERT`/`UPDATE`/`DELETE` on `omniroute_combo_profiles`, `routing_task_profiles`, or `omniroute_combo_candidates` once seeded — treat them like config in production.
- Switching `routing_policy_versions.is_active` (a policy version bump).

---

## 10. Phased implementation plan

### Phase A — read-only inventory ✅ (this pass)

- Inspect OmniRoute combo support — done.
- Inspect Postgres schema/catalogs — done.
- Produce candidate combo schema and seed rows — done in this report.

### Phase B — DB-only MVP (recommended next step)

- Create Postgres migrations `007_add_routing_task_profiles.sql`,
  `008_add_omniroute_combo_profiles.sql`,
  `009_add_omniroute_combo_candidates.sql`,
  `010_add_routing_policy_versions.sql` (optional) in
  `HomeLab/scripts/migrations/`.
- Seed the 8 combo slugs (`router-cheap-low`, `planning-light`,
  `coding-light`, `coding-standard`, `coding-strong`, `coding-max`,
  `review-standard`, `large-context`) and ~12 task profiles.
- Seed the candidate rows **as a starting scaffold only** with
  `confidence='low'` and `source='manual'` — operator reviews and
  promotes to `confidence='high'` after measurement.
- Do **not** touch OmniRoute runtime yet.
- Do **not** touch the router model prompt yet.
- Verification: row counts via `SELECT COUNT(*)` against each new table;
  ensure `\d` shows expected columns; ensure seed rows have valid FK
  targets.

### Phase C — OmniRoute integration

- Pick the simplest safe write path. Recommendation: HTTP API
  (`POST /api/combos` with a management session token the user
  supplies), one combo at a time, with the combo `data` payload
  generated by a script that reads Postgres rows.
- For each `omniroute_combo_profiles` row where `is_enabled = true`:
  - Compute `models[]` from `omniroute_combo_candidates` filtered by
    `combo_profile_id` and `hard_exclude = false`, ordered by
    `overall_rank + manual_rank_boost`.
  - Generate the `ComboCreate` body: `{name, description, models,
strategy, config: {...}, allowedProviders, system_message,
context_length, dimensions}`.
  - POST to `/api/combos`. Surface the response to the user. If 4xx,
    don't retry without a fix; report and pause.
- Keep Postgres as the source of truth: a "mirror" job (cron/manual)
  reconciles Postgres → OmniRoute. Drift is detectable because both
  sides carry the same `omniroute_combo_name`.

### Phase D — Omnigent integration

- Update the router prompt (likely in `control-room/lib/router/` —
  see `prompts.ts`, `recommender-chain.ts`) to:
  1. Receive the list of `routing_task_profiles` from Postgres at boot.
  2. Emit `(task_profile_slug, harness, reasoning_effort,
permission_mode, billing_class_request, combo_slug, rationale)`.
- Update the routing-decision panel (Control Room
  `components/assistant-ui/routing-decision-panel.tsx`) to render the
  combo name + billing class + approval-required flag.
- Update the executor (OpenCode native lane executor +
  `omnigent/inner/_opencode_native_lane_config.py`) to accept an
  explicit `combo_name` argument and pass it to OmniRoute as
  `--combo <name>` on the wire.
- Add `routing_decision_logs` writes in Omnigent after each successful
  turn.

### Phase E — observability + Langfuse (later)

- W3C `traceparent` propagation from Omnigent → OmniRoute (already
  documented in the ownership-split doc §5.4).
- Per-combo metrics surfaced in `~/.omniroute/storage.sqlite` already
  via `usage_history.combo_strategy` and `call_logs.combo_step_id`.

---

## 11. The shift in billing-class policy

The user's preference is now: **not** 100% free whenever possible; use
subscription freely; reserve API-billed + max reasoning for hard
tasks; never downgrade hard coding work to a weak cheap model.

Encoded as:

1. **In `routing_task_profiles.allowed_billing_classes`:**
   - `coding-light`, `planning-light`, `review-standard`:
     `{free, subscription}` (free-equivalent preferred).
   - `coding-standard`, `coding-strong`:
     `{free, subscription}` (subscription strongly preferred when
     not equivalent).
   - `coding-max`:
     `{subscription, api_billed}` (the latter requires explicit
     approval and the panel copy).
   - `router-cheap-low`: `{free, subscription}` (never API-billed).

2. **In `omniroute_combo_profiles.billing_preference_order`:**
   - Most combos: `['free_equivalent', 'subscription']`.
   - `coding-strong`, `coding-max`, `large-context`:
     `['subscription', 'free_equivalent']` (reversed — quality first).

3. **In `omniroute_combo_profiles.allow_api_billed`:**
   - Default `false` everywhere.
   - `coding-max` set to `true` **only when the user has approved the
     `coding-max` tier in settings**; otherwise `false`.

4. **In `omniroute_combo_profiles.forbidden_billing_classes`:**
   - Default `['api_billed', 'unknown']` everywhere.
   - `coding-max` has `['unknown']` only when API-billed is allowed.

5. **In the ranker:** `prefer_free_equivalent = true` only when the
   two candidates are within `Δ ≈ 0.02` on `overall_score`. Otherwise
   quality wins. This is the explicit "never downgrade hard work"
   invariant.

6. **Approval card:** `requires_explicit_approval = true` on
   `coding-max`; the panel renders _"This uses the highest-quality /
   highest-reasoning combo."_ and the user must click Accept.

---

## 12. Suggested next prompt for Phase B (DB-only MVP)

> Plan and implement Phase B — DB-only MVP. Add four new Postgres
> migrations under `HomeLab/scripts/migrations/`:
> `007_add_routing_task_profiles.sql`,
> `008_add_omniroute_combo_profiles.sql`,
> `009_add_omniroute_combo_candidates.sql`,
> `010_add_routing_policy_versions.sql`. Define columns per §5.1–§5.4
> of `docs/routing/omniroute-combo-investigation-2026-07-09.md`.
>
> Seed:
>
> - 12 `routing_task_profiles` rows (router_decision, planning_light,
>   general_chat, coding_light, coding_standard, coding_strong,
>   coding_max, large_context, code_review, test_debugging,
>   browser_agent, shell_risk_review).
> - 8 `omniroute_combo_profiles` rows per §6 of the report, with
>   `omniroute_combo_name = NULL` (we mirror to OmniRoute in Phase C).
> - 1 `routing_policy_versions` row: `version_name='2026-07-09-r1'`,
>   `is_active=true`.
> - 0 candidates rows — those are operator-curated after measurement
>   in Phase C. Place a note in `notes` saying so.
>
> Do **not** create the new tables in the `control_room` DB. Do
> **not** call `POST /api/combos`. Do **not** touch the router
> prompt. Do **not** restart anything.
>
> Use the user's existing safe psql pattern (sudo to `postgres`,
> `-d homelab -f ...`). After applying, run the suggested read-only
> row-count query and report back. Confirm all FKs resolve and the
> seed rows are correct before declaring done.
