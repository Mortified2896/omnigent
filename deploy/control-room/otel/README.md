# Codex telemetry preparation

This directory currently contains **read-only preparation**, not an installed
Collector or a completed migration. The active delivery is tracked in
[issue 153](https://github.com/Mortified2896/omnigent/issues/153).

## Compare supplied configuration files

After independently resolving the Desktop and Omnigent-launched Codex user-home
configuration paths and the actual Collector origin, run:

```sh
python3 deploy/control-room/otel/preflight_codex_config.py \
  --desktop-config /path/to/desktop/config.toml \
  --omnigent-config /path/to/session/config.toml \
  --collector-origin http://127.0.0.1:4318
```

The example origin is not autodiscovery. Substitute the verified local origin.
Python 3.11+ is required. No third-party packages, network request, Codex process,
Collector start, configuration write or archive read is performed.

The check requires an explicit privacy setting and all three OTLP/HTTP signals
pointing to their matching endpoint paths. JSON output includes categorical
findings and valid-file hashes, never config values, filenames, headers, raw
parser errors or endpoint URLs. Exit 0 means these supplied files pass; exit 1
means a finding; exit 2 means invalid arguments. gRPC is outside this deliberately
HTTP-specific check, not a statement that Codex cannot export over gRPC.

**This does not resolve Codex's effective configuration.** Profiles, managed
settings, project layers, CLI overrides and installed-version compatibility
must be inspected separately. Omitted `log_user_prompt` is flagged because this
preflight requires explicit `false`; it does not imply that Codex's default
captures prompts.

A passing result deliberately contains `runtime_verified: false`. It does not
prove that either application loaded the file, distinguish emitted producers,
verify collector health, identify an archive, enforce retention or validate a
privacy transform. Fresh native turns from both real clients and archive
readback are mandatory in the tracked delivery. Do not treat two matching
configuration files as two-source runtime acceptance.

## Offline fixtures

```sh
python3 -m unittest discover -s deploy/control-room/otel -p 'test_*.py' -v
```

Fixtures use temporary files only. No private machine state or telemetry is
included. Integration, installed-Mac, Collector and repository-wide checks are
separate gates; this helper does not replace them.

## Migration boundary

Reusable Codex tooling belongs in the active Omnigent source. Host-specific
configuration, private observations and rollback evidence stay with the
appropriate private operations owner. Preserve existing archive data and
capture identity; do not add another Collector, archive or database.

This preparation does not introduce an estimator schema, general application
instrumentation, a benchmark harness or a documentation/truth framework.
