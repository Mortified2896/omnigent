# Control Room trusted-root policy

> **Legacy scope / superseded RTX assumptions (2026-09-14).**
> This is the opt-in policy for the historical ai-control-hub peers. It neither
> establishes RTX privileges nor authorizes installation or activation on the
> current runtime.
> Present-state selection follows the [HomeLab environment guide](https://github.com/Mortified2896/HomeLab/tree/main/docs/environments).
> This historical opt-in bundle does not establish current runtime topology or
> privileges. Never start, restore or recreate retired services to satisfy a
> preflight. Installation/reactivation requires explicit owner authorization and
> the selected application's distinct-peer and rollback safety contract.

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
