# Shared Mac Codex telemetry

Portable tooling for [issue 153](https://github.com/Mortified2896/omnigent/issues/153).
Runtime acceptance is tracked separately in the companion private operations issue.
This directory does not instrument the Omnigent application or deploy server services.

## Provenance and installation boundary

`migration-source.json` records the exact historical source commit and original
SHA-256 for each relocated file. Historical PRs #5 and #7 in
`Mortified2896/control-room-standalone` explain the original privacy and audit work.
That source tree had no LICENSE, COPYING or NOTICE file; this owner-authorized
migration retains its provenance without asserting a new license.

The old installer and destructive uninstaller are deliberately not migrated.
`adopt_macos.py` only updates an **existing** installation with an existing capture
identity, validates against its installed Collector, verifies the canonical source
location and origin, and backs up replaced files with an executable rollback.
It never downloads a binary, creates an archive, launches a service, changes Codex
configuration, or restarts an application. A remote URL is a source-selection guard,
not cryptographic proof of trust. Its manifest records actual file hashes and whether
the source was dirty; a commit hash alone does not identify uncommitted bytes.

```sh
python3 deploy/control-room/otel/adopt_macos.py --home "$OTEL_DIRECTORY" --plist "$COLLECTOR_PLIST"
# After review and an idle-window plan, add --apply to stage the validated files.
```

Inspect the returned rollback path before activation. Run that executable to restore
backed-up files, then reload only the Collector in an approved idle window. Existing
archive data, capture identity, Collector binary and LaunchAgent wiring stay in place.
The installed helper and audit run from the installation's `bin` directory; no retired
checkout is an executable/configuration fallback.

## Configuration and identity

The supplied-file preflight remains deliberately limited:

```sh
python3 deploy/control-room/otel/preflight_codex_config.py \
  --desktop-config "$DESKTOP_CONFIG" --omnigent-config "$SESSION_CONFIG" \
  --collector-origin http://127.0.0.1:4318
```

It does not resolve effective profiles, command-line overrides, version support, or
capture. Its `runtime_verified=false` is intentional. Resolve actual binary/config
sources first. The [official Codex configuration reference](https://developers.openai.com/codex/config-reference/)
documents `otel.exporter`, `otel.trace_exporter`, `otel.metrics_exporter`,
`otel.environment` and `otel.log_user_prompt`. Keep prompt logging explicitly false.
Minimal Omnigent homes now preserve the supplied `otel` table alongside provider
routing, without loading plugins or MCP servers. Disabled or absent settings stay
as supplied and remain visible through preflight; this is not an implicit opt-in.

The Collector preserves native service identity and native IDs. It adds the
low-cardinality `codex.producer` attribute only to records with explicit native
`originator` or `app_server.client_name` evidence. Desktop's known names and
Omnigent's client namespace are recognized; unmatched records remain unclassified.
Trace correlation can connect classified spans to other spans without timestamp
guesses. Metrics receive no new producer/session metric labels. This means not every
item is individually attributable, and conflicting or missing identity needs review.

A reported `model` is distinct from `actual_model`; routing aliases do not prove the
actual provider/model. Missing fields remain unavailable. Native usage, retry, error
category/status, reasoning, latency and tool identity are retained where emitted.

## Privacy and storage

Lean logs drop prompts, arguments/results, account/email and free-text error fields;
free-text log bodies are cleared. Lean spans/events also drop these fields and
working-directory/remote values, and clear span status messages. Existing installed
secret-pattern scrubbing is retained, including the separate forensic trace pipeline.
Forensic content is private, short-lived, and is not claimed to be lean-safe.
Historical data is not rewritten or manually cleaned.

One aggregate archive target is **50,000,000,000 bytes**. Lean age is 60 days;
forensic age is 3 days and its 4,000,000,000-byte sub-budget is **included** in the
aggregate. LaunchAgent retention cadence is 300 seconds. This is periodic retention,
not a filesystem quota. Active files and unsafe/unrelated paths are protected;
nonconvergence is reported when protected bytes prevent reaching a target. Collector
log files, provenance databases, binaries and rollback backups are outside that
archive budget and must be inventoried separately; no new ancillary writer is added.

The historical `status`/`audit` commands can scan many files. Use a deliberate time
boundary and bounded targeted readback for runtime acceptance; do not dump archives.

## Verification

```sh
python3 -m unittest discover -s deploy/control-room/otel -p 'test_*.py'
# PyYAML is a test-only dependency; fixture uses file receivers and no listeners.
python3 deploy/control-room/otel/test_filter_fixture.py "$COLLECTOR_BINARY"
uv run pytest tests/inner/test_codex_executor.py -k populate_codex_home_config
uv run pytest tests/test_codex_native.py
uv run --no-sync pyrefly check
```

The Collector fixture loads the actual candidate processors, verifies explicit and
unknown producer identity, native IDs and lean privacy canaries. Retention uses tiny
temporary files for age/size boundaries, combined accounting, oldest-first deletion,
active protection, symlink safety and idempotency. Synthetic fixtures do not replace
fresh actual Desktop and Omnigent turns in the same archive. Restart, actual producer
capture, duplicate ingestion, live privacy and login persistence remain separate gates.

### Native acceptance and installed-source compatibility

Record the Collector activation boundary, then use a fresh Desktop turn and a fresh
turn through the installed Omnigent native runner. A documented Omnigent session
event may submit the latter; verify its completed transcript in the installed app.
For a bounded tool check, ask it to discover a read-only file tool and read this
README's first line. Verify the actual tool call, not only the assistant's reply.
Correlate each native thread/turn ID with lean records after activation, retaining
only counts and field availability in shared evidence. An existing thread can run
a fresh turn; old records in that thread are not new acceptance evidence.

Check the current process start time, executable, private home, profile and command
overrides against the source config. Export proves the observed destination; it
does not by itself prove prompt logging is disabled or every config layer resolved.
Keep process/config reload and actual login persistence as distinct checks.

Before adopting the minimal-home change, compare the installed executor with this
branch's executor. If the installed app has newer changes, prepare a patch containing
only the `otel` addition to the minimal-config allowlist. Validate that patch against
a copy of the installed source and run the config-home tests. Do not replace the
installed checkout or app with this branch's older baseline. An applicable patch is
preparation evidence; the installed minimal-home path still needs runtime acceptance
after an authorized compatible integration and safe process reload.

The local host can fork new runners from a preloaded runner process. A new runner
PID alone therefore does not prove updated Python code was loaded. When that host
predates adoption, check its sessions are idle, use the supported local host stop
and app reconnect flow, then verify the fresh runner and its actual minimal-home
child. Do not restart a shared host while another session is active. Preserve the
backend and Collector when only the host's cached code needs refreshing.
