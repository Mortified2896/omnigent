# `omnigent.service.template` — production systemd unit

This is the **scaffolding / documentation** for the rebuild's
production systemd unit. The live unit on the production host is
written by `scripts/cutover.sh` from this template at install
time.

## Why the placeholders

The template has four `<PLACEHOLDER>` substitutions that the
cutover script fills in:

| Placeholder | Source | Verified at install by |
| --- | --- | --- |
| `<OMNI_BIN_PATH>` | The actual absolute path of the `omni` shim installed by the chosen method (`uv tool install`, `pip install --user`, `pip install`, etc.) | `command -v omni && readlink -f $(command -v omni)` |
| `<SERVICE_USER>` | The user the unit runs as (typically `root` for a single-tenant deploy, or a dedicated `omnigent` user for isolation) | `id <SERVICE_USER>` and `stat -c %U $(dirname <OMNI_BIN_PATH>)` |
| `<PROD_PORT>` | The TCP port the production reverse proxy forwards to (e.g. `6767`) | The cutover script's caller passes it as an argument; verify with `ss -ltn 'sport = :<PROD_PORT>'` |
| `<PROD_HOSTNAME>` | The hostname the reverse proxy terminates (used for `OMNIGENT_AUTH_HEADER` validation only — the bind is `0.0.0.0`) | The cutover script's caller passes it |

## Why the executable path is resolved, not hard-coded

**The previous production deployment's installed `omni` shim lives
at one of several possible locations, depending on the chosen
installation method:**

| Install method | `omni` shim path (typical) |
| --- | --- |
| `uv tool install --force <wheel>` (upstream default; canonical) | `$(uv tool dir --bin)/omni` (Linux/macOS: `~/.local/bin/omni`; on a server, may be `~/.local/bin/omni` under the service user's HOME) |
| `pip install --force-reinstall --user <wheel>` | `$(python -m site --user-base)/bin/omni` (Linux/macOS: `~/.local/bin/omni`) |
| `pipx install --force <wheel>` | `$(pipx environment --value PIPX_BIN_DIR)/omni` (typically `~/.local/bin/omni`) |
| `pip install --force-reinstall <wheel>` (system, no `--user`) | `/usr/local/bin/omni` (Debian/Ubuntu) or `/usr/bin/omni` (Fedora/RHEL) |

The user's existing production install is one of the four; we do
not know which one. **The cutover script resolves the actual
absolute path and writes it into the unit's `ExecStart=`.**

## Verification commands

After install:

```sh
# Confirm the unit references the absolute shim path (not a bare
# `omni` that depends on PATH at exec time).
sudo systemctl show omnigent -p ExecStart | head -1

# Confirm EnvironmentFile is loaded.
sudo systemctl show omnigent -p EnvironmentFiles

# Confirm the resolved path actually exists and is executable.
sudo -u <SERVICE_USER> test -x "<OMNI_BIN_PATH>" && echo "ok"

# Confirm the resolved path is the omni CLI (not some other omni).
"<OMNI_BIN_PATH>" --version
```

Expected `ExecStart` output:
```
ExecStart={ path=/home/omnigent/.local/bin/omni ; ... ; argv[]=...
```

Expected `--version` output (after the wheel is installed):
```
omnigent, version 0.7.0+rebuild-<sha>
```

## What this unit deliberately does NOT do

- **It does not hard-code the executable path.** `<OMNI_BIN_PATH>`
  is filled in by the cutover script from the resolved shim.
- **It does not run `python -m omnigent` from a clone.** The
  rebuild installs a wheel (PyPI / private index / local path) and
  uses the wheel's console-script shim. The user's existing fork
  unit MAY have run from a clone at `/srv/omnigent/repo`; if so,
  the cutover script detects this and replaces the unit's
  `ExecStart=` accordingly.
- **It does not write the data dir from the unit.** `OMNIGENT_DATA_DIR`
  comes from the EnvironmentFile (`env.conf`); the unit never
  creates `/var/lib/omnigent`. The cutover script creates the dir
  and sets ownership to `<SERVICE_USER>` before `systemctl start`.
- **It does not run as root unless it must.** `<SERVICE_USER>`
  defaults to the production deploy's existing service user (which
  on the previous fork deploy was likely root). If the user
  prefers a dedicated `omnigent` user, the cutover script creates
  it and rewrites the unit.
- **It does not enable `WatchdogSec`.** omnigent-server does not
  currently implement `sd_notify(WATCHDOG=1)`; enabling the
  watchdog without that would kill the service on every budget.
  Re-enable when omnigent gains sd_notify support.