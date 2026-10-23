# OmniRoute availability client

OmniRoute owns model scans, probe history, provider policy/quota/pricing, the
TB4 registry and route eligibility. Omnigent's Preview **Available models** page
reads that service and can start the same free/included scan. Detailed scope,
metered budgets and scan cancellation live in the linked OmniRoute dashboard.

`server/tb4_routing/availability.py` is an authenticated client of the version1
`/api/model-availability` protocol. No local scanner, benchmark copy or probe
database is active. Gateway unavailability fails closed; the client does not
start a fallback scanner or choose an alternative model.

Omnigent still owns task constraints and immutable route/effort receipts. It
checks the policy echoed by the gateway and revalidates the exact route before
native launch. Existing protections against direct-Codex fallback, model resets
and per-turn overrides remain in place.

Generic Responses/function probes are labeled separately from actual client
observations. `trace_store.harness_evidence` exports only bounded metadata for
exact observed routes on the OmniRoute lane, matched by source/session/response
ID. A completed assistant message is a response observation, not task success
or independent upstream attribution. Tool completion requires matching outputs.
Human ratings, user content and tool output are never exported to this API.

The historical SQLite probe adapter remains available for retained data. New
hourly snapshots use `model_probe_snapshot` with the same stable event IDs, so
moving ownership does not duplicate existing analysis events. Host transport,
timers and encrypted, restore-verified Backblaze backups remain in HomeLab.

Full dashboard: <https://sg-apps.taile0361b.ts.net:9443/availability>.
Application ownership and protocol details are in the
[OmniRoute customization contract](https://github.com/Mortified2896/omniroute-customizations/blob/main/docs/model-availability.md).
