# Omnigent 2 · Production Architecture

This document describes the Omnigent 2 production instance deployed alongside
the existing Omnigent 1 maintenance instance on host `ai-control-hub`.

## Goal

Run a second, completely independent Omnigent installation ("Omnigent 2 ·
Production") so the production build/test traffic cannot impact the
maintenance instance that controls the rest of the host. The maintenance
instance ("Omnigent 1 · Maintenance") must remain untouched and online at all
times; this work must never restart `omnigent.service` or
`omnigent-host.service`.

## Source separation

| Asset | Omnigent 1 · Maintenance | Omnigent 2 · Production |
|---|---|---|
| Source repo | (existing checkout) | `~/workspace/repos/omnigent-production` |
| Branch | (existing) | `bootstrap/omnigent-production-2` |
| Release commit | (existing wheel) | `28d5e1c6323dbeb2ee5f8cadff40b45de5f2224f` |
| Wheel SHA-256 | (existing) | `b583836b1b37aa8bed1a5fc7d4440858036f8a9ee08f9324630fb8e9980e1b49` |

## Runtime separation

| Resource | Maintenance | Production |
|---|---|---|
| Install root | `/opt/omnigent` | `/opt/omnigent-production` |
| Active symlink | `/opt/omnigent/...` | `/opt/omnigent-production/current -> releases/<sha>/` |
| Config root | `/etc/omnigent` | `/etc/omnigent-production` |
| State root (HOME) | `/var/lib/omnigent/home` | `/var/lib/omnigent-production/home` |
| Database | `/var/lib/omnigent/chat.db` | `/var/lib/omnigent-production/chat.db` |
| Logs | `/var/log/omnigent` | `/var/lib/omnigent-production/logs` |
| Artifacts | `/var/lib/omnigent/...` | `/var/lib/omnigent-production/artifacts` |
| Server port | `4097` | `4197` |
| Server systemd unit | `omnigent.service` | `omnigent-production.service` |
| Host systemd unit | `omnigent-host.service` | `omnigent-production-host.service` |
| Tailscale URL | `https://hermes-agent.taile0361b.ts.net:1111/` | `https://hermes-agent.taile0361b.ts.net:2222/` |

The two installs share the host Pi install at `/opt/omnigent/pi` (Pi is
out-of-immutables for this task and is the harness both instances launch).

## Isolation invariants

1. **Different HOME.** The production host runs with
   `HOME=/var/lib/omnigent-production/home`. Its `~/.omnigent/` therefore
   holds a distinct `config.yaml`, `host.pid`, and `daemons/` registry —
   the maintenance host identity, daemon registry and PID file all live in
   `/var/lib/omnigent/home/.omnigent/` and are never touched by the
   production services.

2. **Different OMNIGENT_CONFIG_HOME / OMNIGENT_DATA_DIR.** Set in
   `/etc/omnigent-production/omnigent.env`. The server reads its
   `config.yaml` from `/etc/omnigent-production/config.yaml`; the host's
   log files, runner workspaces, and agent caches live under
   `/var/lib/omnigent-production/`.

3. **Different database.** Production points at
   `/var/lib/omnigent-production/chat.db`. The maintenance instance's
   `chat.db` is read only by `omnigent.service` and never opened by the
   production server.

4. **Different port + bind host.** Production binds `127.0.0.1:4197`.
   Maintenance binds `127.0.0.1:4097`. Tailscale Serve fronts them on
   `:2222` and `:1111` respectively. The legacy `:9461` alias for the
   maintenance backend was deliberately removed after both Tailnet URLs
   were proven live so that the only Tailnet mapping for `4097` is now
   `:1111` (see `deploy/docs/dual-instance-deployment.md`).

5. **Different service units, independent lifecycle.**
   `omnigent-production.service` and `omnigent-production-host.service`
   neither `Requires=` nor `After=` the maintenance units. A failure of
   one is invisible to the other.

6. **No shared writable paths.** systemd units run with
   `ProtectSystem=strict`, `ReadWritePaths=` scoped to
   `/var/lib/omnigent-production` and `/opt/omnigent-production` only.
   `ProtectHome=read-only` keeps the production services from touching
   `/home/hermes` (and therefore never reading maintenance credentials).

7. **Hard-refusal controller.** `/usr/local/sbin/deploy-omnigent-production`
   aborts on any argument, env var, or path that resolves into
   `/opt/omnigent`, `/etc/omnigent`, or `/var/lib/omnigent`, or that
   names `omnigent.service`, `omnigent-host.service`, or port `4097`.
   This is belt-and-braces — neither shell scripting nor operator
   mistake can reach into the maintenance instance.

## Tailscale Serve

| URL | Backend | Purpose |
|---|---|---|
| `https://hermes-agent.taile0361b.ts.net:1111/` | `127.0.0.1:4097` | Omnigent 1 maintenance (primary alias) |
| `https://hermes-agent.taile0361b.ts.net:2222/` | `127.0.0.1:4197` | Omnigent 2 production |
| `https://hermes-agent.taile0361b.ts.net:9460/` | `127.0.0.1:4096` | (other, pre-existing) |
| `https://hermes-agent.taile0361b.ts.net:9462/` | `127.0.0.1:5000` | (other, pre-existing) |

The legacy `:9461` alias for the maintenance backend was removed in the
dual-instance acceptance pass after both `:1111` and `:2222` were proven
live. The removal was scoped (`tailscale serve --https=9461 off`); no
`tailscale serve reset` was used. See
`deploy/docs/dual-instance-deployment.md` for the acceptance evidence
(session ID, runner ID, file-edit acceptance, PIDs before/after the
controlled `omnigent-production.service` restart).

## Deployment

The release lives at
`/opt/omnigent-production/releases/<sha>/venv` with `bin/omnigent` and
`lib/python*/site-packages/omnigent/...`. The active symlink
`/opt/omnigent-production/current` points at one release directory.
`deploy-omnigent-production` builds a new release, swaps the symlink
atomically, restarts the two production units, and rolls back
automatically if the post-switch health check fails.

## Maintenance baseline (must never change)

Captured before this work began:

- `omnigent.service` `MainPID=1620126`, `ActiveEnterTimestamp=Tue 2026-08-04 07:54:42 UTC`
- `omnigent-host.service` `MainPID=1614576`, `ActiveEnterTimestamp=Tue 2026-08-04 07:45:46 UTC`

Both PIDs were re-checked after the controlled
`omnigent-production.service` restart (acceptance step 5) that confirmed
Omnigent 2 self-recovers. They remain `1620126` / `1614576` unchanged.
`deploy/scripts/verify-omnigent-production.sh` re-asserts this at every
post-deploy check.