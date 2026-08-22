# Historical Control Room broad-privilege bundle

> **Historical and non-operative. Do not install or restore this bundle.**

This directory preserves source evidence for a superseded Control Room design.
It is not current desired policy, it does not describe ordinary O1/O2 child
capability, and it is not an installation or recovery runbook. The installer
accepts source validation only and refuses installation.

Current desired capability policy belongs to authorized machine-enforced
configuration. Current observed capability belongs to authorized timestamped
observation. Public prose cannot grant or prove either. Ordinary O1/O2 coding
agents and harness children cannot elevate, and broad child sudo must not be
restored. Human emergency root and the root-owned, narrowly scoped deployment
interface are separate boundaries.

The retired bundle contained these artifacts:

| Source | Installed destination |
| --- | --- |
| `systemd/omnigent-host.service.d/99-trusted-root.conf` | `/etc/systemd/system/omnigent-host.service.d/99-trusted-root.conf` |
| `systemd/omnigent-production-host.service.d/99-trusted-root.conf` | `/etc/systemd/system/omnigent-production-host.service.d/99-trusted-root.conf` |
| `env/omnigent-trusted-root.env` | `/etc/omnigent/trusted-root.env` |
| `env/omnigent-production-trusted-root.env` | `/etc/omnigent-production/trusted-root.env` |
| `sudoers/99-omnigent-agent-root` | `/etc/sudoers.d/99-omnigent-agent-root` |

These files describe the rejected broad-privilege design and remain only for
source-history review while issue #115 defines a truthful future capability
contract. They are not evidence of installed or executable capability.

Check the historical source bundle without changing the host:

```bash
deploy/control-room/trusted-root/install.sh --check
```

There is no installation mode. Do not copy these fragments into host
configuration or use them for recovery. Follow the authoritative dual-instance
safety contract and the authorized HomeLab machine-policy authority instead.
