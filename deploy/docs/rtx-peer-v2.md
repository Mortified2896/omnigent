# RTX O1/O2 v2 deployment contract

This supersedes the historical Control Room deployment entrypoints on RTX VM100.
The supported promotion/recovery owner is `python -m peer_deployer.rtx`.
Legacy promotion scripts are refused on RTX and deprecated for removal in v0.14.0.
No legacy deployment daemon is installed. HomeLab owns the one-time bootstrap,
systemd units, environment files, snapshots and Tailscale mappings.

O1 is primary; O2 is the warm maintenance peer. Neither upgrades itself.
Each has separate `/srv/omnigent/o{1,2}/{config,home,state,releases}` roots,
a distinct host UUID and database binding, and a root-owned current pointer to
the same immutable accepted runtime. Immutable artifacts may be shared;
writable databases, session state and homes may not.

Accounts and OIDC support `OMNIGENT_SESSION_COOKIE_SUFFIX=O1` or `O2`.
HTTPS retains the `__Host-` prefix, Secure, HttpOnly, path=/ and no Domain.
An unset suffix preserves existing behavior. Set separate cookie secrets.
Use `OMNIGENT_STRICT_HOST_IDENTITY=1` to refuse active duplicate registrations.
`/v1/info` reports instance_id and embedded build_sha. O3 is disabled with
`OMNIGENT_O3_ROUTING_REVIEW=0`; Smart Routing and manual/default remain available.

Promotion requires target, supervisor, expected-current SHA, immutable acceptance
path and SHA256, and a unique transaction ID. All must be explicit. Acceptance
binds source ancestry, wheels, installed application/frontend files, executable,
dependency check, isolated boot, capabilities and exact DB schema. This first
release supports same-schema promotion only; schema changes fail closed.

Preflight checks actual `/proc` environment, command, cgroup ownership and duplicate
processes. A DB binding table prevents fresh/foreign DB substitution. Global flock
serializes promotions; stale nonterminal transactions require explicit recovery.
A durable journal records ownership and the mutation boundary BEFORE stopping any
target service. Pre-boundary rollback is forbidden. A stopped-target SQLite backup
and saved-state archive are retained before switching. A failed startup restores
the target DB/release; state archives remain available for manual recovery. The
supervisor identity/PIDs and rollback release must remain intact throughout.

Run focused regressions on RTX:

```sh
.venv/bin/python -m pytest tests/deploy/test_rtx_peer_v2.py tests/server/test_instance_cookie.py tests/server/test_accounts.py tests/server/test_host_registry.py
```

Current live topology is recorded in HomeLab `docs/omnigent-current-topology.md`.
This contract is a desired architecture, not evidence that either peer is accepted.
