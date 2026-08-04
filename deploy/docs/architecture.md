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
| Telemetry port | `9461` | (separate OTel export) |
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
   `:2222` and `:1111` respectively, distinct host-port mappings so the
   existing `:9461` mapping is untouched.

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
   names `omnigent.service`, `omnigent-host.service`, or ports
   `4097`/`9461`. This is belt-and-braces — neither shell scripting nor
   operator mistake can reach into the maintenance instance.

## Tailscale Serve

Production serves through `:2222`; maintenance continues through `:1111`
and `:9461`. New mapping is additive only — existing mappings were not
removed or replaced.

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

`deploy/scripts/verify-omnigent-production.sh` re-asserts these at every
post-deploy check.