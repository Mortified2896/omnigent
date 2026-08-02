# Phase D canary systemd unit — `omnigent-canary.service`

This directory holds the **Phase D pre-cutover canary** systemd
template. It is separate from the production unit template
(`omnigent.service.template` in this same directory) and
**must never be confused with it**.

## The two units

| File | Purpose | Phase D impact |
| --- | --- | --- |
| `omnigent.service.template` | Production unit. Installed at `/etc/systemd/system/omnigent.service` during the Phase E cutover. | Untouched during Phase D. |
| `omnigent-canary.service.template` | Temp canary unit. Installed (optionally) at `/etc/systemd/system/omnigent-canary.service` only when systemd-level service behaviour must be validated. | Created on demand; removed at the end of Phase D. |

## When the canary unit is required

Phase D's **default** is a temp foreground process under `bash`
— that exercises every code path the production unit does
because the rebuild's `omni` shim is the same binary in both
cases. The canary systemd unit is only required when the
operator needs to validate systemd-specific behaviour, such as:

- `journald` field capture (the in-process check 1 looks at
  `journalctl` for the first-boot migration log line, but only
  when systemd is in play).
- `LimitNOFILE=`, `MemoryMax=`, `CPUQuota=` integration with
  the wheel's process manager.
- `Restart=` integration with the wheel's liveness watchdog.
- The phase-D cutover's `systemctl start omnigent-canary.service`
  against the rebuilt unit template (useful as a smoke test of
  the rendering path before the real cutover).

## When the canary unit is NOT required

If the operator only needs to confirm "the rebuild wheel boots
and serves traffic on a temp port", the foreground process
under `bash` is sufficient and is the recommended default. Skip
this directory.

## Hard isolation invariants

The canary unit **must**:

1. Have the file name `omnigent-canary.service` (not
   `omnigent.service`).
2. Bind `127.0.0.1` on a **temporary port** (the production
   port is bound by the live service and is never reused by
   the canary).
3. Set `OMNIGENT_DATA_DIR` to a fresh temp dir.
4. Source an `EnvironmentFile=` pointing at the canary's env
   file (not the production `/etc/omnigent/env.conf`).
5. Use `--agent <CANARY_DATA_DIR>/agents/...` for the canary's
   local agent bundles (not the production
   `/srv/omnigent/agents/...`).
6. Be **removed** (`rm -f /etc/systemd/system/omnigent-
   canary.service && systemctl daemon-reload`) at the end of
   Phase D. The canary is never left installed between
   releases.

The canary unit **must NOT**:

1. Modify, replace, or be linked to the production
   `omnigent.service`.
2. Stop, restart, or `Requires=`/`BindsTo=`/`PartOf=` the
   production `omnigent.service`.
3. Bind the production port.
4. Read or write the production `chat.db` (live
   `/var/lib/omnigent/chat.db` or
   `/home/<user>/.omnigent/chat.db`).
5. Install the rebuild wheel over the production installation.
6. Be enabled for boot-time start (`WantedBy=` is empty in the
   template for that reason).

## Rendering the template

The template uses bash-style `<PLACEHOLDER>` markers (NOT
systemd's `%`-specifiers, which collide with unit directives).
The operator renders the template with `envsubst` or with a
small helper:

```sh
sed \
  -e 's|<OMNI_BIN_PATH>|/opt/canary-bin/omni|g' \
  -e 's|<CANARY_PORT>|17670|g' \
  -e 's|<CANARY_DATA_DIR>|/home/hermes/.canary-omnigent-run-1|g' \
  -e 's|<SERVICE_USER>|hermes|g' \
  -e 's|<CANARY_ENV_FILE>|/etc/omnigent-canary-run-1/env.conf|g' \
  omnigent-canary.service.template \
  > /etc/systemd/system/omnigent-canary.service
```

After rendering, the operator should diff the result against
the template to confirm every placeholder was substituted and
no `<...>` markers remain.

## Run / stop / remove

```sh
# Run.
sudo systemctl daemon-reload
sudo systemctl start omnigent-canary.service
journalctl -u omnigent-canary.service -f

# Stop.
sudo systemctl stop omnigent-canary.service

# Remove.
sudo rm -f /etc/systemd/system/omnigent-canary.service
sudo systemctl daemon-reload
```

## Verification at the end of Phase D

After removing the canary unit, confirm the production unit
is untouched:

```sh
# Production unit must still exist and be unmodified.
sudo systemctl status omnigent.service   # or:
sudo systemctl cat omnigent.service

# Production port must still be bound by the production service.
ss -tlnH "sport = :$PROD_PORT"

# Production chat.db must be untouched (mtime unchanged).
stat -c '%y' /var/lib/omnigent/chat.db    # or:
stat -c '%y' /home/$SERVICE_USER/.omnigent/chat.db
```

## See also

- `INSTALL.md` — installing the production unit
  (`omnigent.service.template`) at Phase E cutover.
- `omnigent.service.template` — the production unit template.
- `docs/rebuild/PHASED_IMPLEMENTATION_PLAN.md` §D.0a / §D.0b /
  §D.5 — Phase D isolation rules.
- `docs/rebuild/RISKS_AND_UNRESOLVED.md` §"Things to validate
  during the canary" — what the canary must demonstrate.
