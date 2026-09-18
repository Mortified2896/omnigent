# Control Room forward-port compatibility assessment

> **Historical source-integration assessment.** The identities, revisions and
> validation status below describe the recorded integration checkpoint, not the
> current development environment or live acceptance. For current selection use
> the [HomeLab environment guide](https://github.com/Mortified2896/HomeLab/tree/main/docs/environments).

Status: source integration in progress; no deployment or routing acceptance.

## Verified source identities

- Development host: ai-control-hub, repository owner hermes.
- Canonical repository: /home/hermes/workspace/repos/omnigent.
- Fetch/push origin: https://github.com/Mortified2896/omnigent.git.
- Original integration ref: codex/rtx-o1-integration at ec53fdba8f857def7823bf3c11573646ed023876.
- Capture PR #159: codex/harbor-benchmark-capture at 64e13c93d56d6c89d32ff3793dee02f620bed9e2.
- HomeLab PR #45: codex/harbor-benchmark-capture-docs at 9c160f95c701cb1adf60e718180d90d0bc110cf7.
- Upstream main observed: 7c0279c1a142c70ead241b6202203d1a374c25af.
- Trial integration base: stable v0.13.0, eebef804e1fe4beddc61ea232b951cddddd7f890.
- Common ancestor: 43762a989235599f2ec63f270f1f0b32ae7d3e7a.
- Original capture lineage has 107 commits outside upstream main; upstream main has 1,244 commits outside it.

Stable v0.13.0 is the trial base to avoid combining the migration with the newer unreleased main changes. It is not an accepted release yet. Both stable and main were assessed with git merge-tree: stable has 55 conflicted paths and main has 56. The new worktree preserves the original references and unrelated worktrees.

## Compatibility matrix

| Area | Classification | Resolution / outstanding work |
| --- | --- | --- |
| Benchmark capture, Git snapshots, offline export | Custom still required | Upstream does not supply this capture contract. Retain opt-in behavior and native trajectory preservation. |
| Benchmark environment variables | Custom still required, adapted | Retain two exact host allowlist entries; add them to upstream Codex launch-variable filtering. No broad capture prefix. |
| Harness environment filtering | Upstream supersedes local filtering implementation | Use shared clean_agent_env and retain explicit provider credential exceptions. |
| Daemon/runner environment configuration | Upstream foundation retained | Keep upstream passthrough, provider-config credential discovery, proxy boundaries, and dispatch trace-context removal from persistent daemon state. |
| Native Codex module layout | Adaptation required | Move capture/provenance integrations and imports into upstream harnesses/codex_native package. |
| Native Codex launch provenance | Custom still required | Preserve explicit access lane, requested model/effort and immutable launch provenance. These are launch facts, not provider-execution proof. |
| Native terminal boundary handling | Adaptation required | Retain capture finalization while preserving upstream control-state reconciliation before network output flush. |
| Background Codex threads | Overlapping fixes | Preserve upstream ephemeral filtering and custom system-thread exclusion. |
| Native model catalog | Upstream supersedes endpoint-based picker discovery | Use upstream launch-shaped cached/probed catalogs for each explicitly resolved Control Room lane. Preserve lane identity and native-reported effort metadata. |
| Native reasoning effort clamp | Drop obsolete custom behavior | Accept upstream native full ladder rather than overwriting reported per-model metadata with a fixed GPT-5.6 ladder. |
| O3 review, policy locks and audit | Custom still required | Retain explicit routing policy, review authorization, proposal IDs, audit content and compression. Adapt typed server info and virtualized transcript placement. |
| Framework-owned instructions | Adaptation required | Compose custom capability-gated root instruction alongside upstream framework instructions, including nullable prompt path. |
| Standing O2 assumption in runtime prompt | Drop obsolete behavior | Single-primary releases require external control. Explicitly prohibit reviving retired O2 for a legacy runbook. |
| Pi direct subscription + gateway selection | Custom still required, adapted | Preserve explicit direct lane and qualified model selection; adopt upstream launch result and effort metadata. |
| O3 landing controller / dedicated Mac startup | Adaptation required | Retain the O3 landing controller; combine upstream Electron lifecycle, updater, partition hardening and startup logging with canonical local recovery. 413 Electron tests pass; visual acceptance remains required. |
| MLflow / OTel custom metadata | Retain pending evidence | Upstream tracing improvements do not prove OmniRoute attempt identity. Live cross-system correlation remains unproven. |
| Legacy peer deployer | Preserve inactive source | No peer deployment mechanism selected; no O2 activation. |

## Source validation evidence so far

- Latest combined capture / daemon / runner / actual spawned-child environment / framework-prompt tests: 69 passed.
- Native forwarder / effort / executor-adapter rerun: 233 passed.
- O3 review / O3 policy / audit / host / prompt run: 268 passed.
- Explicit lane / native-effort catalog regression: 1 passed.
- Electron tests after reconciling upstream entrypoint and custom recovery: 413 passed.
- Python static analysis: 0 errors (424 suppressed, 237 warnings).
- TypeScript project check passed.
- Four targeted frontend files initially: 749 passed, 14 failed.
- Exact original capture PR baseline: 271 passed, 5 failed in NewChatDialog.test.tsx.
- After fixing the nine introduced frontend failures: 271 passed, the same 5 baseline failures remain.
- Earlier broad native/host run: 488 passed, 15 failed. Subsequent focused runs fixed the circular import, helper rename, model normalization, and isolated ambient source metadata.
- Full pre-commit was executed and exposed existing lint failures in unchanged legacy deployment files. Those unrelated formatter changes were restored. All hooks on the staged changed files subsequently passed; final Electron-only hooks are being rechecked.

The environment regression invokes the real daemon and runner filters, the upstream Codex environment filter, and a real Python child process. It proves both capture variables survive with values 0 and 1, in local and remote daemon modes. A generic unrelated operator-secret sentinel is absent at every hop. The local daemon intentionally retains the OMNIGENT_ configuration namespace; an unrelated sentinel in that namespace is excluded from the runner, harness, and child. This test is not a live RTX/native-Codex canary.

## Proposed provenance contract and acceptance gates

One logical Omnigent turn can issue multiple OmniRoute HTTP requests; each request can have multiple attempts. Retain separate identities for session, logical turn, response, native Codex thread/turn, request, and attempt. Do not flatten multiple Codex model calls into one request.

Capture should reference privacy-safe request/attempt evidence by identifiers and digests. OTel trace IDs are optional join evidence, not the sole offline record. Requested aliases, configured provider names, and launch lanes must never populate actual-execution fields without gateway evidence.

The required live chain is still unproven:
Omnigent session/turn/response -> native Codex thread/turn -> OmniRoute requests -> ordered actual attempts -> optional OTel traces -> capture -> SelfBench -> Harbor.

No OmniRoute upgrade candidate has been selected. Its source/runtime version drift audit is gated on an accepted Omnigent candidate. No live canary, release switch, rollback, SelfBench construction, or Harbor solver run has occurred in this work. No production state, secrets, permissions, or capture defaults have been changed.

Do not enable ordinary-workload capture until both requested canaries pass. Existing historical canary outcomes remain user-provided context, not newly verified evidence.

## Refreshed runtime inventory

RTX package.json and CLI both report OmniRoute 3.8.50. The npm latest tag also resolves to 3.8.50; a newer gateway release is not currently established. The customizations repository still pins 3.8.43, so deployed patch reconciliation remains required.

The active Omnigent server and host process environments both select ec53fdba8f857def7823bf3c11573646ed023876. Both capture variables are absent from both processes. No live state was changed by this inventory.
