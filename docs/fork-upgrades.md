# Maintaining the customized Omnigent fork

The 0.18 integration merges official tag `v0.18.0`
(`a8676d23e88df7918f4f8ef1c7c4e3186e710354`) into fork main based on
`63145fc940e6d4c34035e9edc63f9e750aaf63f4`. Keep both merge parents: the next
release then compares against 0.18 instead of replaying the customization
history. Do not squash release integrations or resolve conflicts by replacing
entire fork files with upstream versions.

## Customization boundaries

Keep feature logic in these modules, with small calls in upstream pages and
routes. Extend the existing tests when upstream APIs or creation paths change.

| Custom behavior | Feature modules | Integration and regression checks |
| --- | --- | --- |
| Visible official version and private revision; click to copy | `web/src/shell/ApplicationVersions.tsx` | Sidebar branding plus a separate Mac desktop row outside the hidden brand/header; component, sidebar/CSS tests and rendered browser, desktop-shell and mobile checks |
| Advisor, Jev, exact model/connection/effort, task tags | `omnigent/model_advisor/`, `web/src/model-advisor/` | Session creation/events and host launch; `tests/model_advisor/`, route `test_*model_advisor.py`, exact native-selection tests |
| Feedback, discussions, answer comparison, scoring isolation | Feedback/comparison server routes and stores; `ResponseFeedbackActions`, feedback helpers | Transcript bubbles and side-chat pane; feedback/comparison route and component tests, task-scoring tests |
| Reports, search, narrated audio | `omnigent/server/scheduled/`, generated-audio modules, `web/src/components/scheduled/` | Scheduler routes/store and inbox; scheduled fire/routes, report settings/actions, audio-player/timing tests |
| Reports settings entry | `ReportAutomationSettings.tsx` | SettingsPage mounts one component; keep card copy/layout out of upstream settings groups |
| Separate harness/model/reasoning controls, mobile composer | `web/src/components/composer/`, shared composer styles | NewChatDialog and ChatPage compose controls; composer tests and desktop/mobile browser checks |
| Full reasoning initially visible, manual collapse | ReasoningView, reasoning primitive, BlockRenderer | Work segments containing reasoning start open; tool-only segments retain ordinary folding; renderer/reasoning tests |
| Review-queue pins, unread state, stable sidebar on unpin | `omnigent/server/session_pins.py`, read-state helpers | Every create path must seed caller-scoped pins, including reused uploaded agents; auto-pin/read-state routes and Sidebar.rowActions tests |
| Explicit Direct recovery and provenance | Codex native bridge/app-server, route policy, DirectFallbackNotice | Resume carries resolved provider and model; native Codex and exact-selection/fallback tests |
| Direct OpenTelemetry and privacy filtering | `omnigent/runtime/telemetry.py`, tracing helpers | Retain upstream active-skill spans alongside fork exporters/filtering; never restore retired MLflow wiring during reconciliation |
| RTX identity/authentication/rollback | `deploy/scripts/peer_deployer/`, auth/identity helpers | HomeLab owns installed services/network; immutable release hashes, DB identity and candidate acceptance stay mandatory |

Omnigent 0.18's web extension API supports separate pages and navigation. It does
not expose all chat/composer slots or server route hooks used here. A nominal
plugin would still need core patches. Retain these module boundaries until
suitable supported hooks exist; extract components/helpers incrementally when
that actually removes conflict-prone code from an upstream file.

## Repeatable upgrade procedure

1. Fetch fork main and upstream tags. Create an isolated task worktree from
   current fork main, verify remotes and merge the exact release tag. Record
   both SHAs above. Preserve the official merge ancestry.
2. Reconcile the features in the table, including automatically merged code.
   Verify each shell layout: a component retained in source can still be hidden
   by platform CSS. In particular, the Mac desktop shell hides the branding
   block, so application versions must also render outside that block.
   New entry points must propagate caller identity, labels, conditional-update
   guards and exact transport. In 0.18 this includes
   `update_conversation_with_changes` and the reused-upload creation path.
   Uploaded agents now outlive sessions; conditional test cleanup must retain
   them, like ordinary deletion.
3. Use locked dependencies, run the focused checks below and all applicable
   pre-commit hooks. `OMNIGENT_SKIP_WEB_UI=true` is only for preparing a test
   environment; the accepted release must contain the compiled web UI.
4. Compare migration files to the previous upstream release and accepted fork.
   Preserve custom revisions; never rewrite applied migrations. Rehearse exact
   candidate startup against a consistent database backup. Verify integrity,
   Alembic head, instance binding, counts and existing content, then test backup
   restoration. A successful source merge does not prove data compatibility.
5. Commit with both parents, publish to the fork and build from the final SHA.
   Record wheel/frontend hashes. Start an isolated RTX candidate and check
   ordinary authentication, saved data, custom UI and a marked native test chat.
   Follow [test-session hygiene](task-scoring-and-test-sessions.md).
6. Follow HomeLab's current release architecture for owner inspection and
   exact-artifact promotion, with a stopped-state backup and retained previous
   release. WireGuard is primary; stopped Mac Tailscale is expected. HomeLab
   `docs/wireguard-homelab.md` documents access and Preview limitations.

## Focused compatibility checks

Run from the task worktree with installed locked dev/tracing dependencies.
These do not replace migration rehearsal, a real release build or browser and
native acceptance. Use `--reruns 0` so integration failures remain visible.

```sh
.venv/bin/python -m pytest tests/model_advisor tests/server/scheduled \
  tests/server/integration/test_scheduled_tasks_routes.py \
  tests/server/routes/test_model_advisor.py \
  tests/server/routes/test_current_model_advisor.py \
  tests/server/routes/test_jev_model_advisor.py \
  tests/server/routes/test_response_feedback.py \
  tests/server/routes/test_feedback_discussion.py \
  tests/server/routes/test_session_auto_pin.py \
  tests/server/routes/test_sessions_read_state.py \
  tests/server/routes/test_generated_response_audio.py \
  tests/stores/test_conversation_store.py tests/runtime/test_pending_inputs.py \
  tests/harnesses/codex_native/test_codex_native.py \
  tests/runner/test_codex_advisor_exact_selection.py \
  tests/server/test_task_scoring.py tests/util/test_test_session_policy.py \
  tests/server/routes/test_session_conditional_delete.py --reruns 0 -q

pnpm --dir web test src/model-advisor src/components/scheduled \
  src/components/ResponseFeedbackActions.test.tsx \
  src/components/chat/GeneratedResponseAudioPlayer.test.tsx \
  src/components/chat/DirectFallbackNotice.test.tsx \
  src/components/chat/SideChatPane.test.tsx \
  src/components/blocks/BlockRenderer.test.tsx \
  src/components/blocks/ReasoningView.test.tsx \
  src/pages/ChatPage.composer.test.tsx src/pages/InboxPage.test.tsx \
  src/pages/SettingsPage.test.tsx src/shell/NewChatDialog.test.tsx \
  src/shell/Sidebar.rowActions.test.tsx src/store/chatStore.test.ts

.venv/bin/pyrefly check
pnpm --dir web type-check
pnpm --dir web build
```

Broaden checks to affected upstream suites for authentication, schema or
serialization changes. Keep Python, client/UI/Slack packages and lockfile
workspace versions aligned.
