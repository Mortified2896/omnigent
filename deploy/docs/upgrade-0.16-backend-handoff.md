# Upstream 0.16 integration: backend handoff

This branch contains integrated application source but is NOT deployment accepted.
Pushed at the owner's explicit request for inspection despite remaining failures.
Do not merge main or deploy until validation and migration rehearsals are complete.

Upstream merge parent: `82a74473ee4c01163c4269f4296b10a158f0e47e`.
Fork preparation parent: `647f4d28502b8c393d6cfca18ec293a53f344c5e`.
Merge base: `b058dd3280c7bc666e4db62a5bb8eaae093456d5`.
No unresolved conflicts. Both ancestries preserved.

Latest-per-test results across the interrupted full attempt, unexecuted-case
continuations and focused repair reruns: **33191 passed, 34 failed, 184 skipped,
5 existing xfailed**; 33414 selected identities covered, 15 Databricks-marked
cases deselected. This is NOT an uninterrupted passing full-suite run.
Earlier affected-file rerun: 59 failed / 1447 passed; after repairs those
21 files passed with 1513 passed. Earlier interrupted broad report had 92 failures.
No new skips/xfails or weakened assertions were added.
Ruff, Pyrefly (zero errors with inherited suppressions/warnings), and all-file
pre-commit checks passed before this handoff addition.

## Remaining environment investigation

Host: rtx-omnigent, hermes UID 1000, Python 3.12.3, Linux, /usr/bin/bwrap.
Direct probe `bwrap --ro-bind / / --unshare-user --uid 0 --gid 0 /usr/bin/true`
fails with `bwrap: setting up uid map: Permission denied`.
Kernel audit reports AppArmor profile `unprivileged_userns` denied writes to
`proc/<pid>/uid_map` (disconnected path, error -13).
Separately, egress tests report `bwrap: loopback: Failed RTM_NEWADDR: Operation
not permitted`. This network namespace failure has NOT been independently
root-caused; do not equate it automatically with UID mapping.
Default-security Docker probe also failed namespace creation. No host security,
permissions, sysctls, services, or privileged-container settings were changed.
Inspect actual bwrap argv, active AppArmor profile, namespace capability and
seccomp restrictions separately before selecting a narrow remedy. Downstream
missing exit_code, containment=false and filesystem 404 assertions remain
provisionally environment-related and must be rechecked in a functioning sandbox.
Do not add skips or weaken containment/security assertions.

## Repairs already made

- Restore upstream Codex gateway auth timeout (15000ms).
- Restore existing Pi managed-connect credential fallback, preserving explicit
  API-key precedence and fail-closed explicit managed selection.
- Preserve durable upstream harness wire IDs while using relocated imports.
- Retain newly written crash report when rotation timestamps tie.
- Use loaded conversation for custom snapshot ETag; read narrow next_position
  fingerprint rather than rehydrating full rows.
- Reconcile fixtures with upstream delayed locked janitor, authorized owned
  clones, query mutation fences, relay item APIs and auxiliary-thread contract.
- Keep trusted helper fixtures mode 0700, durable uninstall anchor and isolated
  CLI fixtures; update inference assertions for env_key transport.
- Isolate cold-import tests in subprocesses to prevent stale session-module
  identity leaks affecting audio, kickoff and detach tests.
- Scope inherited repeating faulthandler timer to inner tests. Worker core showed
  _Py_DumpTracebackThreads; final uninterrupted verification remains outstanding.
- Use private sqlite CLI for tests and TERM=xterm-256color with NO_COLOR unset.

Retain Model Advisor, feedback/outcomes/scoring exclusions, exact Advisor-response
association, provenance, Kokoro/read-along, Tailscale auth, O1/O2 isolation, OTEL
and deployment safety. O3 application is unnecessary; routing work is preserved
for later, disabled. Historical custom migrations remain unchanged; new merge
revision d016c91f6a2d joins ll1a2b3c4d5e and c91f6a2d7e40.
Snapshots remain temporary upstream baselines, not reviewed acceptance results.
No restoration rehearsal, immutable artifact acceptance, main merge or deployment.

## Exact remaining failures

Classification: ENVIRONMENT_FAILURE, provisional for downstream symptoms.
Each entry includes the actual captured exception/assertion. No private payloads,
database contents or credentials are included.

### `tests/inner/sandbox/test_egress_e2e.py::test_egress_allows_matching_https_get[linux_bwrap]`

```text
AssertionError: {'error': 'os_env helper failed: OS environment helper exited with code 1: bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted'}
assert 'error' not in {'error': 'os_env helper failed: OS environment helper exited with code 1: bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted'}
```

### `tests/inner/sandbox/test_egress_e2e.py::test_egress_denies_unmatched_https_get[linux_bwrap]`

```text
AssertionError: {'error': 'os_env helper failed: OS environment helper exited with code 1: bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted'}
assert 'error' not in {'error': 'os_env helper failed: OS environment helper exited with code 1: bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted'}
```

### `tests/inner/sandbox/test_egress_e2e.py::test_egress_direct_tcp_bypass_is_blocked[linux_bwrap]`

```text
AssertionError: {'error': 'os_env helper failed: OS environment helper exited with code 1: bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted'}
assert 'error' not in {'error': 'os_env helper failed: OS environment helper exited with code 1: bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted'}
```

### `tests/inner/sandbox/test_egress_e2e.py::test_egress_injects_ca_env_vars_at_same_bundle[linux_bwrap]`

```text
AssertionError: {'error': 'os_env helper failed: OS environment helper exited with code 1: bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted'}
assert 'error' not in {'error': 'os_env helper failed: OS environment helper exited with code 1: bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted'}
```

### `tests/inner/sandbox/test_egress_e2e.py::test_s2_egress_blocks_private_destination_by_default[linux_bwrap]`

```text
AssertionError: {'error': 'os_env helper failed: OS environment helper exited with code 1: bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted'}
assert 'error' not in {'error': 'os_env helper failed: OS environment helper exited with code 1: bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted'}
```

### `tests/inner/sandbox/test_egress_e2e.py::test_s2_egress_allows_private_destination_when_opt_in[linux_bwrap]`

```text
AssertionError: {'error': 'os_env helper failed: OS environment helper exited with code 1: bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted'}
assert 'error' not in {'error': 'os_env helper failed: OS environment helper exited with code 1: bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted'}
```

### `tests/inner/sandbox/test_egress_e2e.py::test_s4_same_uid_external_process_cannot_use_helper_relay[linux_bwrap]`

```text
AssertionError: Helper warmup failed: {'error': 'os_env helper failed: OS environment helper exited with code 1: bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted'}
assert None == 0
 +  where None = <built-in method get of dict object at 0x7a413287bf40>('exit_code')
 +    where <built-in method get of dict object at 0x7a413287bf40> = {'error': 'os_env helper failed: OS environment helper exited with code 1: bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted'}.get
```

### `tests/inner/sandbox/test_egress_e2e.py::test_credential_proxy_swap_on_access_injects_basic_without_sandbox_secret[linux_bwrap]`

```text
KeyError: 'exit_code'
```

### `tests/inner/sandbox/test_egress_e2e.py::test_credential_proxy_https_bearer_swaps_injected_env_token[linux_bwrap]`

```text
KeyError: 'exit_code'
```

### `tests/inner/sandbox/test_egress_e2e.py::test_credential_proxy_databricks_cli_materializes_cfg_and_swaps[linux_bwrap]`

```text
KeyError: 'exit_code'
```

### `tests/inner/sandbox/test_sandbox_behavior.py::test_sandbox_uses_private_desktop_runtime[linux_bwrap-helper]`

```text
KeyError: 'exit_code'
```

### `tests/inner/sandbox/test_sandbox_behavior.py::test_sandbox_uses_private_desktop_runtime[linux_bwrap-launcher]`

```text
AssertionError: [omnigent-sandbox] spawn-time wrap re-exec backend=linux_bwrap wrap_head=['bwrap', '--ro-bind-try', '/usr']
  bwrap: setting up uid map: Permission denied

assert 1 == 0
 +  where 1 = CompletedProcess(args=['/tmp/omnigent-sandbox-__c_vhkv.sh', '-c', "\nimport json, os, socket, stat\nfrom pathlib impor...exec backend=linux_bwrap wrap_head=['bwrap', '--ro-bind-try', '/usr']\nbwrap: setting up uid map: Permission denied\n").returncode
```

### `tests/inner/sandbox/test_sandbox_behavior.py::test_sandbox_blocks_shell_write_outside_cwd[linux_bwrap]`

```text
KeyError: 'exit_code'
```

### `tests/inner/sandbox/test_sandbox_behavior.py::test_sandbox_provides_writable_scratch_tmpdir[linux_bwrap]`

```text
KeyError: 'exit_code'
```

### `tests/inner/sandbox/test_sandbox_behavior.py::test_sandbox_empty_write_paths_blocks_cwd_writes_but_allows_tmpdir[linux_bwrap]`

```text
KeyError: 'exit_code'
```

### `tests/inner/sandbox/test_sandbox_behavior.py::test_sandbox_blocks_credential_dotfiles_under_granted_read_path[linux_bwrap]`

```text
AssertionError: Non-dotfile under the granted read_paths root was unreadable — the dotfile masker is too broad and masked code/app.py too. stdout=None stderr=None
assert 'OMNI_S5_NON_DOTFILE_OK=visible-control' in ''
 +  where '' = <built-in method get of dict object at 0x7a4184bbdec0>('stdout', '')
 +    where <built-in method get of dict object at 0x7a4184bbdec0> = {'error': 'os_env helper failed: OS environment helper exited with code None: bwrap: setting up uid map: Permission denied'}.get
```

### `tests/inner/sandbox/test_sandbox_behavior.py::test_sandbox_allows_dotfile_under_read_path_when_allowlisted[linux_bwrap]`

```text
AssertionError: .aws was in cwd_allow_hidden but the helper couldn't read .aws/credentials. The allowlist is not being applied to read_paths roots — operator opt-in is broken. stdout=None stderr=None
assert 'OMNI_S5_AWS_OPTIN_VISIBLE=allowlisted-content' in ''
 +  where '' = <built-in method get of dict object at 0x7a4187227d40>('stdout', '')
 +    where <built-in method get of dict object at 0x7a4187227d40> = {'error': 'os_env helper failed: OS environment helper exited with code None: bwrap: setting up uid map: Permission denied'}.get
```

### `tests/inner/sandbox/test_sandbox_behavior.py::test_sandbox_hides_user_dotfiles_in_cwd[linux_bwrap]`

```text
KeyError: 'exit_code'
```

### `tests/inner/sandbox/test_sandbox_behavior.py::test_sandbox_allow_network_false_blocks_outbound_connect[linux_bwrap]`

```text
KeyError: 'exit_code'
```

### `tests/inner/sandbox/test_sandbox_behavior.py::test_sandbox_helper_inherits_explicit_env_passthrough[linux_bwrap]`

```text
KeyError: 'exit_code'
```

### `tests/inner/sandbox/test_sandbox_behavior.py::test_sandbox_start_in_scratch_helper_starts_in_scratch_tmpdir[linux_bwrap]`

```text
KeyError: 'exit_code'
```

### `tests/inner/sandbox/test_sandbox_behavior.py::test_sandbox_start_in_scratch_workspace_remains_readable[linux_bwrap]`

```text
KeyError: 'exit_code'
```

### `tests/inner/test_bwrap_sandbox.py::test_overlapping_read_write_root_remains_writable`

```text
AssertionError: bwrap: setting up uid map: Permission denied

assert 1 == 0
 +  where 1 = ProbeResult(stdout='', stderr='bwrap: setting up uid map: Permission denied\n', exit_code=1).exit_code
```

### `tests/inner/test_bwrap_sandbox.py::test_read_only_root_rejects_writes`

```text
AssertionError: bwrap: setting up uid map: Permission denied

assert 1 == 0
 +  where 1 = ProbeResult(stdout='', stderr='bwrap: setting up uid map: Permission denied\n', exit_code=1).exit_code
```

### `tests/inner/test_bwrap_sandbox.py::test_nested_write_root_overlays_read_only_parent`

```text
AssertionError: bwrap: setting up uid map: Permission denied

assert 1 == 0
 +  where 1 = ProbeResult(stdout='', stderr='bwrap: setting up uid map: Permission denied\n', exit_code=1).exit_code
```

### `tests/inner/test_bwrap_sandbox.py::test_seccomp_blocks_dangerous_socket_families_inside_helper`

```text
AssertionError: Probe failed (rc=1). stderr='bwrap: setting up uid map: Permission denied\n'
assert 1 == 0
 +  where 1 = ProbeResult(stdout='', stderr='bwrap: setting up uid map: Permission denied\n', exit_code=1).exit_code
```

### `tests/inner/test_bwrap_sandbox.py::test_seccomp_blocks_unshare_and_setns_inside_helper`

```text
AssertionError: Probe failed (rc=1). stderr='bwrap: setting up uid map: Permission denied\n'
assert 1 == 0
 +  where 1 = ProbeResult(stdout='', stderr='bwrap: setting up uid map: Permission denied\n', exit_code=1).exit_code
```

### `tests/inner/test_bwrap_sandbox.py::test_seccomp_blocks_clone_with_namespace_flags_inside_helper`

```text
AssertionError: Probe failed (rc=1). stderr='bwrap: setting up uid map: Permission denied\n'
assert 1 == 0
 +  where 1 = ProbeResult(stdout='', stderr='bwrap: setting up uid map: Permission denied\n', exit_code=1).exit_code
```

### `tests/inner/test_bwrap_sandbox.py::test_interpreter_under_masked_dotdir_still_spawns`

```text
AssertionError: interpreter under masked .local failed to spawn (rc=1). stderr='bwrap: setting up uid map: Permission denied\n'
assert 1 == 0
 +  where 1 = CompletedProcess(args=['bwrap', '--ro-bind-try', '/usr', '/usr', '--ro-bind-try', '/lib', '/lib', '--ro-bind-try', '/l...else 'ok');sys.stdout.write('RAN')"], returncode=1, stdout='', stderr='bwrap: setting up uid map: Permission denied\n').returncode
```

### `tests/inner/test_bwrap_sandbox.py::test_run_launcher_spawn_wrap_private_tmpdir_boots_under_bwrap`

```text
AssertionError: wrapped launcher exited 1. stdout='' stderr="[omnigent-sandbox] spawn-time wrap re-exec backend=linux_bwrap wrap_head=['bwrap', '--ro-bind-try', '/usr']\nbwrap: setting up uid map: Permission denied\n"
assert 1 == 0
 +  where 1 = CompletedProcess(args=['/tmp/omnigent-sandbox-2a50w5ae.sh', '/tmp/pytest-of-hermes/pytest-24/popen-gw0/test_run_launch...exec backend=linux_bwrap wrap_head=['bwrap', '--ro-bind-try', '/usr']\nbwrap: setting up uid map: Permission denied\n").returncode
```

### `tests/inner/test_bwrap_symlink_mask_3265.py::test_skipping_symlink_masks_does_not_leak_the_target`

```text
AssertionError: namespace must assemble: bwrap: setting up uid map: Permission denied

assert 1 == 0
 +  where 1 = CompletedProcess(args=['/usr/bin/bwrap', '--ro-bind', '/usr', '/usr', '--ro-bind-try', '/bin', '/bin', '--ro-bind-try'...masks_do0/.env 2>/dev/null; exit 0'], returncode=1, stdout='', stderr='bwrap: setting up uid map: Permission denied\n').returncode
```

### `tests/inner/test_bwrap_symlink_mask_3265.py::test_bwrap_rejects_a_bind_onto_a_symlink`

```text
AssertionError: regular-file mask should work: bwrap: setting up uid map: Permission denied

assert 1 == 0
 +  where 1 = CompletedProcess(args=['/usr/bin/bwrap', '--ro-bind', '/usr', '/usr', '--ro-bind-try', '/bin', '/bin', '--ro-bind-try'..._bind_onto0/plain', '/usr/bin/true'], returncode=1, stdout='', stderr='bwrap: setting up uid map: Permission denied\n').returncode
```

### `tests/inner/test_codex_brokered_auth_e2e.py::test_linux_bwrap_real_signer_relay_with_deterministic_worker`

```text
AssertionError: assert False
 +  where False = <omnigent.inner.codex_executor._CodexAppServerSession object at 0x7f087efe7e00>._containment_confirmed
 +    where <omnigent.inner.codex_executor._CodexAppServerSession object at 0x7f087efe7e00> = _CodexSessionState(capture_proxy=None, app_session=<omnigent.inner.codex_executor._CodexAppServerSession object at 0x7....', '/tmp/pytest-of-hermes/pytest-37/popen-gw1/test_linux_bwrap_real_signer_r0', '[]'), closing=False, close_task=None).app_session
```

### `tests/runner/test_environment_filesystem.py::test_download_under_real_sandbox_refuses_masked_file[linux_bwrap]`

```text
assert 404 == 200
 +  where 404 = <Response [404 Not Found]>.status_code
```
