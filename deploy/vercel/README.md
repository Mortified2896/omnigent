# Omnigent on Vercel

Run the Omnigent server as a **Vercel container function**: Vercel builds the
`Dockerfile.vercel` shim at the repo root (it pulls the prebuilt
`ghcr.io/omnigent-ai/omnigent-server` image), serves it over HTTPS on
`*.vercel.app`, and scales it on Fluid compute. Postgres comes from the Neon
marketplace integration; artifacts go to an S3-compatible bucket you bring
(AWS S3, Cloudflare R2, Tigris, …) — Vercel has no persistent disk.

> [!NOTE]
> **Know the tradeoffs before picking this target.** Vercel's WebSocket
> support (public beta) closes every connection when the function hits its
> max duration — **300 s on Hobby, 800 s on Pro** (1800 s beta). Omnigent's
> runner and host tunnels auto-reconnect (~0.5 s, validated end to end) and
> in-flight turns survive the cut, so sessions keep working — but the
> tunnels churn every few minutes, and a request that is mid-flight over
> the tunnel at the instant of the cut fails once. Traffic can also spread
> across function instances with no way to pin it (see
> [Constraints](#constraints)). For an always-on tunnel with no churn, use
> Render, Railway, Fly, or Modal instead. This target suits kicking the
> tires and light single-user use.

## How it works

```
        HTTPS / SSE / WebSocket
browser ───────────────►  Vercel (Fluid compute)
runner  ───────────────►      │ container function
                              ▼
                        omnigent server ──► DATABASE_URL (Neon Postgres,
                        (Dockerfile.vercel │  marketplace integration)
                         → prebuilt image) │
                                           └► OMNIGENT_ARTIFACT_URI
                                              s3://… (required, durable)
```

- **`Dockerfile.vercel`** (repo root) — auto-detected by Vercel; the image is
  built on Vercel's builders (no local Docker) and pushed to Vercel's
  registry. It's a `FROM ghcr.io/omnigent-ai/omnigent-server` shim that
  overlays this checkout's entrypoint + Blob backend (so the deploy tracks
  the repo even when the pulled image lags it), pre-compiles bytecode for
  faster cold boots, and adds the `vercel`/`boto3` SDKs.
- **`vercel.json`** (repo root) — pins the function region to `iad1` to sit
  next to Neon's default region; if you provision your database elsewhere,
  change it to match. Cross-country DB round-trips dominate request latency
  otherwise.
- **Bind-first entrypoint** (`entrypoint.py` here) — Vercel gives a container
  **15 s** to accept TCP connections, but a first boot runs Alembic
  migrations (~1 min against Neon), and even a normal cold boot takes
  ~10–25 s. The standard entrypoint binds only after migrating, so this
  wrapper inverts the order: it binds immediately and answers **503** (HTTP)
  or a retryable WS-upgrade denial while the real server boots behind it,
  then delegates everything. Runners, hosts, and browsers just retry.
- **Neon Postgres** — provisioned through the Vercel marketplace;
  `DATABASE_URL` is injected automatically.
- **S3-compatible bucket** — **required**, unlike most other targets. The
  container's disk is per-instance and ephemeral, and requests round-robin
  across instances, so agent bundles and user files must live in a bucket
  (`OMNIGENT_ARTIFACT_URI`) via the native `S3ArtifactStore`. Without it,
  even the built-in agents fail to load (`failed to load agent spec`).

## Prerequisites

- A Vercel account with **Fluid compute** (the default for projects created
  since April 2025). WebSocket support is a public beta; the Hobby plan
  works, Pro raises the tunnel-cut interval from 300 s to 800 s.
- **Node** for the `vercel` CLI (`npx vercel …`); no local Docker needed.
- An **S3-compatible bucket** + access keys (AWS S3, Cloudflare R2, Tigris,
  Backblaze B2, MinIO, …). For R2 key minting, see
  [`../cloudflare/README.md`](../cloudflare/README.md#4-r2-s3-credentials-for-the-artifact-store).

## Deploy

### 1. Create the project

From the repo root (a clone or your fork):

```bash
npx vercel login
npx vercel link --yes --project omnigent
```

### 2. Provision Neon Postgres

```bash
npx vercel install neon    # injects DATABASE_URL into the project env
```

The first run prints a browser URL to accept Neon's marketplace terms —
open it, accept, and re-run the command. (Or from the dashboard:
**Storage → Create Database → Neon**.)

### 3. Set the required env vars

```bash
# Session cookie secret — pin it: the disk is ephemeral, and a re-minted
# secret would log everyone out on every instance recycle.
openssl rand -hex 32 | npx vercel env add OMNIGENT_ACCOUNTS_COOKIE_SECRET production

# Artifact store (required — see above): bucket + S3 credentials.
npx vercel env add OMNIGENT_ARTIFACT_URI production      # s3://<bucket>[/prefix]
npx vercel env add AWS_ACCESS_KEY_ID production
npx vercel env add AWS_SECRET_ACCESS_KEY production
npx vercel env add AWS_ENDPOINT_URL_S3 production        # non-AWS only (e.g. R2, Tigris)
```

The public base URL is auto-detected from Vercel's
`VERCEL_PROJECT_PRODUCTION_URL`, and the container binds the port Vercel
routes to by default — no `PORT` or URL config needed.

### 4. Deploy to production

```bash
npx vercel deploy --prod --yes
```

While the server boots, requests return `503` with a JSON
`"server starting"` detail. The **first** boot runs all migrations against
Neon (~1 minute); later cold starts are ~10–25 s. Then:

```bash
curl https://<project>.vercel.app/health   # {"status":"ok"}
```

### 5. First admin + connect a host

Open the URL — the Setup screen claims the first admin (username +
password). Then connect a machine to actually run agents (the server is
just the control plane):

```bash
omnigent login https://<project>.vercel.app
omnigent host  --server https://<project>.vercel.app
```

## Raise the tunnel-cut interval (Pro)

On Hobby, function max duration is fixed at 300 s, so runner/host tunnels
reconnect every 5 minutes. Observed cycle: a clean cut at ~310 s, then —
because the tunnel was what kept the instance alive — the reconnect lands
on a fresh instance and retries through its boot-time 503s, reconnecting
~20 s later. The tunnel is up ~93% of the time on Hobby, hands-free. On
Pro, raise the project's default function max duration to **800 s** in the
dashboard (**Settings → Functions**) to stretch the cycle to ~13 minutes
(~97% uptime).

## Use your own IdP instead (OIDC)

Switch the provider with env vars (OIDC requires HTTPS, which `*.vercel.app`
provides):

```bash
npx vercel env add OMNIGENT_AUTH_PROVIDER production        # oidc
npx vercel env add OMNIGENT_OIDC_ISSUER production          # e.g. https://github.com
npx vercel env add OMNIGENT_OIDC_CLIENT_ID production
npx vercel env add OMNIGENT_OIDC_CLIENT_SECRET production
npx vercel env add OMNIGENT_OIDC_REDIRECT_URI production    # https://<project>.vercel.app/auth/callback
openssl rand -hex 32 | npx vercel env add OMNIGENT_OIDC_COOKIE_SECRET production
```

Redeploy to apply. For Google Workspace, also set
`OMNIGENT_OIDC_ALLOWED_DOMAINS` to restrict logins to your domain.

## Constraints

- **Tunnel churn.** Every WebSocket closes at the function's max duration
  (300 s Hobby / 800 s Pro / 1800 s beta). Runners and hosts reconnect with
  ~0.5 s backoff and running turns resume; a tunnel-proxied request caught
  mid-flight at the cut fails once. Browser SSE streams and terminal tabs
  reconnect the same way.
- **Requests spread across instances.** The server keeps its runner
  registry and live-event fan-out in process memory, and Vercel pins each
  WebSocket to one instance while routing other requests freely — including
  to brand-new cold instances that answer 503 while booting. Under light
  traffic one warm instance serves everything and this works; under bursty
  load or mid-redeploy, requests can land on an instance that can't see
  your runner's tunnel and fail until the tunnels cycle. There is no
  single-instance pin on Vercel (unlike Modal's `max_containers=1`).
- **No persistent disk.** Postgres is required (no SQLite lite tier), the
  cookie secret must be pinned via env, and artifacts **must** live in an
  S3-compatible bucket.
- **4.5 MB request-body cap** on Vercel functions — pushing an agent bundle
  larger than that fails; trim the bundle.
- **No scale-to-zero with connected runners.** A live tunnel keeps the
  instance provisioned (memory is billed for instance lifetime; CPU only
  while messages flow). With no runners or browsers connected, the instance
  scales in after ~5 idle minutes and the next request cold-starts behind
  ~10–25 s of 503s.

## Cost

Fluid compute bills Active CPU (~$0.13/CPU-hr), provisioned memory
(~$0.01/GB-hr while any instance is up), and invocations. A lightly used
deploy with a runner connected during working hours lands in the low
dollars/month on Pro; Hobby's included allotment covers kicking the tires.
Neon has a free tier; marketplace billing is unified through Vercel. The
artifact bucket is billed by its provider (R2/Tigris free tiers cover light
use).
