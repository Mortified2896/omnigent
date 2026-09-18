# Control Room trusted-root policy

> **Legacy scope / superseded RTX assumptions (2026-09-14).**
> This is the opt-in policy for the historical ai-control-hub peers. It neither
> establishes RTX privileges nor authorizes installation or activation on the
> current runtime.
> Present-state decisions follow the [HomeLab current topology](https://github.com/Mortified2896/HomeLab/blob/codex/rtx-o1-migration/docs/omnigent-current-topology.md) and [operational workflow](https://github.com/Mortified2896/HomeLab/blob/codex/rtx-o1-migration/docs/codex-server-workflow.md).
> Active O1 is on RTX VM 100; no live O2 peer is required or available.
> Do not start, restore or recreate retired O2 to satisfy preflight.
> Recovery/installation text requires explicit owner authorization to
> reactivate retired services. Distinct-peer and rollback safety rules remain
> intact for an explicitly reintroduced two-peer topology.

This directory is the repository-managed source of truth for the opt-in
trusted-agent host policy used by the Control Room's O1 and O2 Omnigent host
services. It is intentionally separate from generic Omnigent installation and
is not applied by any upstream install path unless an operator explicitly runs
this installer.

The bundle owns these artifacts:

| Source | Installed destination |
| --- | --- |
| `systemd/omnigent-host.service.d/99-trusted-root.conf` | `/etc/systemd/system/omnigent-host.service.d/99-trusted-root.conf` |
| `systemd/omnigent-production-host.service.d/99-trusted-root.conf` | `/etc/systemd/system/omnigent-production-host.service.d/99-trusted-root.conf` |
| `env/omnigent-trusted-root.env` | `/etc/omnigent/trusted-root.env` |
| `env/omnigent-production-trusted-root.env` | `/etc/omnigent-production/trusted-root.env` |
| `sudoers/99-omnigent-agent-root` | `/etc/sudoers.d/99-omnigent-agent-root` |

The two drop-ins explicitly set `OMNIGENT_TRUSTED_ROOT_ACCESS=true`, load the
same value from the corresponding non-secret env fragment, clear the service
hardening that blocks authorized root administration, and reset inherited
syscall/capability restrictions. In particular, both drop-ins clear
`SystemCallArchitectures`; O2 must never inherit `SystemCallArchitectures=native`.

The sudoers rule is:

```text
hermes ALL=(ALL:ALL) NOPASSWD: ALL
```

Before installation, the script renders a candidate sudoers fragment and runs
`visudo -cf` against it. Only a successfully validated candidate is copied to
the destination. Existing application env files are not overwritten; the
dedicated fragments are loaded by the trusted host drop-ins.

Check the repository-owned sources without changing the host:

```bash
deploy/control-room/trusted-root/install.sh --check
```

Install or reconcile the Control Room host policy from a clean checkout:

```bash
sudo deploy/control-room/trusted-root/install.sh --install
```

The installer reloads systemd but never restarts O1 or O2. The historical
rollout instructions called for sequential peer activation with health checks.
Do not repeat that activation without explicit owner authorization to
reintroduce a two-peer topology and a reviewed deployment plan. Do not run the
installer as part of a generic
Omnigent deployment.
