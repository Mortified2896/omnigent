# Canary D.1 report — D2-20260802T095644Z

| Rebuild SHA | Run ID | Phase | Port | Prod port | Data dir | Auth header |
| --- | --- | --- | --- | --- | --- | --- |
| `113340be` | `D2-20260802T095644Z` | D.1 (initial) | `17670` | `4097` | `/tmp/canary-data` | `X-Forwarded-Email` |

## Summary

| # | Check | Status | Duration |
| --- | --- | --- | --- |
| 01 | `01_db_init` | **FAIL** | 0s |
| 02 | `02_web_health` | **FAIL** | 0s |
| 03 | `03_omniroute` | **FAIL** | 1s |
| 04 | `04_pi_repo_edit` | **FAIL** | 0s |
| 05 | `05_pi_commit` | **FAIL** | 0s |
| 06 | `06_pi_push` | **FAIL** | 0s |
| 07 | `07_opencode_repo_edit` | **FAIL** | 0s |
| 08 | `08_opencode_commit` | **FAIL** | 0s |
| 09 | `09_opencode_push` | **FAIL** | 0s |
| 10 | `10_langfuse` | **FAIL** | 0s |
| 11 | `11_verity_delegation` | **FAIL** | 0s |
| 12 | `12_worktree_isolation` | **FAIL** | 1s |

## Evidence

### `01_db_init` — FAIL

```
FAIL chat.db not present at /tmp/canary-data/chat.db (the wheel did not boot, or its first-boot migration did not fire)
```

### `02_web_health` — FAIL

```
canary_port=17670
prod_port=4097
health_url=http://127.0.0.1:17670/health
version_body={"version":"0.7.0"}
unit_name=omnigent-canary
run_id=D2-20260802T095644Z
PASS
```

### `03_omniroute` — FAIL

```
{"reason": "OmniRoute POST /routes:select returned status=404 body=b'<!DOCTYPE html><html lang=\"en\" dir=\"ltr\"><head><meta charSet=\"utf-8\"/><meta name=\"viewport\" content=\"width=device-width, initial-scale=1, viewport-fit=cover\"/><link rel=\"stylesheet\" href=\"/_next/stati'", "status": "FAIL"}
```

### `04_pi_repo_edit` — FAIL

```
Preparing worktree (new branch 'polly/canary-4-pi-D2-20260802T095644Z')
curl: (22) The requested URL returned error: 500
Traceback (most recent call last):
  File "<string>", line 3, in <module>
  File "/usr/lib/python3.11/json/__init__.py", line 293, in load
    return loads(fp.read(),
           ^^^^^^^^^^^^^^^^
  File "/usr/lib/python3.11/json/__init__.py", line 346, in loads
    return _default_decoder.decode(s)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/usr/lib/python3.11/json/decoder.py", line 337, in decode
    obj, end = self.raw_decode(s, idx=_w(s, 0).end())
               ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/usr/lib/python3.11/json/decoder.py", line 355, in raw_decode
    raise JSONDecodeError("Expecting value", s, err.value) from None
json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)
FAIL could not resolve agent_id for agent_name=pi-native-ui on port 17670
FAIL function bar not present in diff
```

### `05_pi_commit` — FAIL

_(no evidence captured)_

### `06_pi_push` — FAIL

_(no evidence captured)_

### `07_opencode_repo_edit` — FAIL

```
Preparing worktree (new branch 'polly/canary-7-opencode-D2-20260802T095644Z')
FAIL POST /v1/sessions failed; body: {
  "agent_id": "cf65137fc096a61a6434956c92093549",
  "purpose": "implement",
  "workspace": "/tmp/canary-fixtures/D2-20260802T095644Z/opencode/worktree",
  "prompt": "rename the function foo to bar. Run pytest, lint, and typecheck on what you changed. Push your branch when green. Open a PR with gh pr create if a remote is configured.",
  "title": "canary-opencode-native-ui-polly/canary-7-opencode-D2-20260802T095644Z"
}
FAIL function bar not present in diff
```

### `08_opencode_commit` — FAIL

_(no evidence captured)_

### `09_opencode_push` — FAIL

_(no evidence captured)_

### `10_langfuse` — FAIL

```
{"reason": "LANGFUSE_HOST / LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY must be set", "status": "FAIL"}
```

### `11_verity_delegation` — FAIL

```
Preparing worktree (new branch 'polly/canary-11-verity-D2-20260802T095644Z')
FAIL POST /v1/sessions returned HTTP 500; body: {"error":{"code":"internal_error","message":"An internal error occurred."}}
```

### `12_worktree_isolation` — FAIL

```
Preparing worktree (new branch 'polly/canary-12-pi-a-D2-20260802T095644Z')
Preparing worktree (new branch 'polly/canary-12-pi-b-D2-20260802T095644Z')
curl: (22) The requested URL returned error: 500
Traceback (most recent call last):
  File "<string>", line 3, in <module>
  File "/usr/lib/python3.11/json/__init__.py", line 293, in load
    return loads(fp.read(),
           ^^^^^^^^^^^^^^^^
  File "/usr/lib/python3.11/json/__init__.py", line 346, in loads
    return _default_decoder.decode(s)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/usr/lib/python3.11/json/decoder.py", line 337, in decode
    obj, end = self.raw_decode(s, idx=_w(s, 0).end())
               ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/usr/lib/python3.11/json/decoder.py", line 355, in raw_decode
    raise JSONDecodeError("Expecting value", s, err.value) from None
json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)
FAIL POST /v1/sessions failed; body: {
  "agent_id": "a1b7caa17404f6716180ba69aa37c592",
  "purpose": "implement",
  "workspace": "/tmp/canary-fixtures/D2-20260802T095644Z/parallel/wt-b",
  "prompt": "rename the function foo to bar. Run pytest, lint, and typecheck on what you changed. Push your branch when green. Open a PR with gh pr create if a remote is configured.",
  "title": "canary-pi-native-ui-polly/canary-12-pi-b-D2-20260802T095644Z"
}
```

## Per-run artifacts

Full per-check stdout+stderr captured under `/home/hermes/workspace/repos/omnigent-eval-rebuild-upstream-0.7/docs/rebuild/canary-runs/D2-20260802T095644Z/log/*.log`.
TSV results in `/home/hermes/workspace/repos/omnigent-eval-rebuild-upstream-0.7/docs/rebuild/canary-runs/D2-20260802T095644Z/results.tsv`.
Env snapshot in `/home/hermes/workspace/repos/omnigent-eval-rebuild-upstream-0.7/docs/rebuild/canary-runs/D2-20260802T095644Z/canary.env`.
