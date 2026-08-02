# `env.conf.template` — Omnigent 0.7 production EnvironmentFile

This template is the single source of truth for **environment variables**
the production `omnigent.service` unit sources at start.

## Install

```sh
sudo install -d -m 0750 -o root -g omnigent /etc/omnigent
sudo install -m 0640 -o root -g omnigent \
    env.conf.template /etc/omnigent/env.conf
$EDITOR /etc/omnigent/env.conf
sudo systemctl daemon-reload
```

(`omnigent` is the conventional service-user name; if your unit uses a
different `User=`, set the group ownership to that user instead.)

## What goes here vs. what goes in `config.yaml`

| Concern | Where |
| --- | --- |
| Auth provider / header name / admin path / `OMNIGENT_AUTH_ENABLED` | **`env.conf`** |
| OpenTelemetry endpoint + headers | **`env.conf`** |
| `OMNIGENT_DATA_DIR`, harness tmp parent, harness idle timeout | **`env.conf`** |
| Per-harness binary path overrides (`OMNIGENT_<NAME>_PATH`) | **`env.conf`** |
| Secrets (API keys, tunnel tokens) | **`env.conf`** (mode 0640) |
| Admin roster (`admins:`), allowed domains (`allowed_domains:`) | `<data_dir>/config.yaml` (or the live `admins` / `allowed_domains` files for no-restart edits) |
| `artifact_location`, `database_uri`, `copy_max_files`, `policy_modules`, `default_agent`, `llm:`, `routing:`, `sandbox:`, `policies:`, `admins:`, `allowed_domains:`, `debug_router_modules:`, `execution_timeout`, `server:` | `<data_dir>/config.yaml` |
| Per-user `providers:` and `routing:` blocks | `~/.omnigent/config.yaml` (per-user) |

## What this file deliberately AVOIDS

- Legacy upstream env vars that v0.7 no longer reads
  (`HARNESS_<NAME>_PATH` — replaced by `OMNIGENT_<NAME>_PATH`).
- Hard-coded executable paths — the systemd unit's `ExecStart=`
  resolves the actual `omni` shim path at install time via
  `uv tool dir --bin` (or by querying the Python user-base after a
  `pip` install); see `deploy/rebuild/systemd/omnigent.service.template`.
- Per-harness model routing — that lives in `~/.omnigent/config.yaml`
  (`providers:` + `routing:` blocks) or in the agent bundle's
  `executor:` block, not in this env file.

## Variable reference

| Variable | Default | Effect |
| --- | --- | --- |
| `OMNIGENT_DATA_DIR` | `~/.omnigent` | Where `chat.db`, `config.yaml`, `artifacts/`, `local_server.{pid,sig,logpath}` live. **Pin this** on a real deploy so the data root is stable. |
| `OMNIGENT_AUTH_PROVIDER` | `header` | One of `header`, `oidc`, `accounts`. The production deploy is `header` (the reverse proxy injects the identity). |
| `OMNIGENT_AUTH_HEADER` | `X-Forwarded-Email` | Header name the server reads in header-auth mode. Cloudflare Access: `Cf-Access-Authenticated-User-Email`. |
| `OMNIGENT_AUTH_HEADER_STRIP_PREFIX` | (unset) | Google IAP: `accounts.google.com:` |
| `OMNIGENT_AUTH_ENABLED` | (unset) | When unset, the server is single-user local mode. Set to `1` for multi-user. |
| `OMNIGENT_ADMIN_LIST_PATH` | `<data_dir>/admins` | One identity per line; mtime-cached. |
| `OMNIGENT_ALLOWED_DOMAINS_PATH` | `<data_dir>/allowed_domains` | Optional email-domain allowlist (header / OIDC). |
| `OMNIGENT_TELEMETRY_ENABLED` | (unset) | Master OpenTelemetry opt-in. Unset = no spans, no exporter, no instrumentor hooks. |
| `OMNIGENT_OTEL_CAPTURE_CONTENT` | `false` | Whether spans carry message content. Off by default; PII risk. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | (unset) | OTLP exporter URL. Langfuse exposes `/api/public/otel`. |
| `OTEL_EXPORTER_OTLP_HEADERS` | (unset) | URL-encoded headers for the exporter. Langfuse requires `Authorization=Bearer <key>`. |
| `OTEL_SERVICE_NAME` | `omnigent` | Service name on emitted spans. |
| `OMNIGENT_HARNESS_TMP_PARENT` | system tmp | Per-session harness tmp root. |
| `OMNIGENT_HARNESS_IDLE_TIMEOUT_S` | `3600` | Idle reap timeout (seconds). `0` disables. |
| `OMNIGENT_<NAME>_PATH` | (unset) | Override path for the named harness binary (`PI`, `OPENCODE`, `CLAUDE`, `CODEX`, `CURSOR`, `HERMES`, etc.). |
| `OMNIGENT_INDEX_URL` | `pypi.org/simple` | Where `omni upgrade` looks for newer releases. |
| `OMNIGENT_RUNNER_TUNNEL_TOKEN` | (unset) | When set, the server accepts exactly this token from runner tunnels (instead of any-token-bound). |

## Verification

After install:

```sh
sudo systemctl show omnigent -p Environment
sudo systemctl show omnigent -p EnvironmentFiles
sudo journalctl -u omnigent -n 5 -o cat
```

The `Environment` line shows the merged environment
(EnvironmentFile + the unit's own `Environment=`);
`EnvironmentFiles` shows the absolute paths the unit sources.