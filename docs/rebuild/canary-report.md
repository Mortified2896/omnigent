# Canary D.1 report — D1-20260802T095003Z

| Rebuild SHA | Run ID | Phase | Port | Prod port | Data dir | Auth header |
| --- | --- | --- | --- | --- | --- | --- |
| `cc6c04fa` | `D1-20260802T095003Z` | D.1 (initial) | `17670` | `4097` | `/tmp/canary-data` | `X-Forwarded-Email` |

## Summary

| # | Check | Status | Duration |
| --- | --- | --- | --- |
| 01 | `01_db_init` | **FAIL** | 0s |
| 02 | `02_web_health` | **FAIL** | 0s |
| 03 | `03_omniroute` | **FAIL** | 1s |
| 04 | `04_pi_repo_edit` | **FAIL** | 0s |
| 05 | `05_pi_commit` | **FAIL** | 0s |
| 06 | `06_pi_push` | **FAIL** | 0s |
| 07 | `07_opencode_repo_edit` | **FAIL** | 1s |
| 08 | `08_opencode_commit` | **FAIL** | 0s |
| 09 | `09_opencode_push` | **FAIL** | 0s |
| 10 | `10_langfuse` | **FAIL** | 0s |
| 11 | `11_verity_delegation` | **FAIL** | 0s |
| 12 | `12_worktree_isolation` | **FAIL** | 0s |

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
run_id=D1-20260802T095003Z
PASS
```

### `03_omniroute` — FAIL

```
{"reason": "OmniRoute POST /routes:select returned status=404 body=b'<!DOCTYPE html><html lang=\"en\" dir=\"ltr\"><head><meta charSet=\"utf-8\"/><meta name=\"viewport\" content=\"width=device-width, initial-scale=1, viewport-fit=cover\"/><link rel=\"stylesheet\" href=\"/_next/stati'", "status": "FAIL"}
```

### `04_pi_repo_edit` — FAIL

```
Preparing worktree (new branch 'polly/canary-4-pi-D1-20260802T095003Z')
curl: (22) The requested URL returned error: 422
Traceback (most recent call last):
  File "<string>", line 1, in <module>
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
/home/hermes/workspace/repos/omnigent-eval-rebuild-upstream-0.7/deploy/rebuild/tests/checks/04_pi_repo_edit.sh: 91: SECONDS: parameter not set
```

### `05_pi_commit` — FAIL

```
Traceback (most recent call last):
  File "<stdin>", line 3, in <module>
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
```

### `06_pi_push` — FAIL

```
Traceback (most recent call last):
  File "<stdin>", line 3, in <module>
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
```

### `07_opencode_repo_edit` — FAIL

```
Preparing worktree (new branch 'polly/canary-7-opencode-D1-20260802T095003Z')
curl: (22) The requested URL returned error: 422
Traceback (most recent call last):
  File "<string>", line 1, in <module>
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
/home/hermes/workspace/repos/omnigent-eval-rebuild-upstream-0.7/deploy/rebuild/tests/checks/07_opencode_repo_edit.sh: 91: SECONDS: parameter not set
```

### `08_opencode_commit` — FAIL

```
Traceback (most recent call last):
  File "<stdin>", line 3, in <module>
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
```

### `09_opencode_push` — FAIL

```
Traceback (most recent call last):
  File "<stdin>", line 3, in <module>
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
```

### `10_langfuse` — FAIL

```
{"reason": "LANGFUSE_HOST / LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY must be set", "status": "FAIL"}
```

### `11_verity_delegation` — FAIL

```
Preparing worktree (new branch 'polly/canary-11-verity-D1-20260802T095003Z')
FAIL POST /v1/sessions returned HTTP 422; body: {"detail":[{"type":"missing","loc":["agent_id"],"msg":"Field required","input":{"agent_selector":"verity","workspace":"/tmp/canary-fixtures/D1-20260802T095003Z/verity/worktree","prompt":"rename foo to
```

### `12_worktree_isolation` — FAIL

```
Preparing worktree (new branch 'polly/canary-12-pi-a-D1-20260802T095003Z')
Preparing worktree (new branch 'polly/canary-12-pi-b-D1-20260802T095003Z')
curl: (22) The requested URL returned error: 422
curl: (22) The requested URL returned error: 422
```

## Per-run artifacts

Full per-check stdout+stderr captured under `/home/hermes/workspace/repos/omnigent-eval-rebuild-upstream-0.7/docs/rebuild/canary-runs/D1-20260802T095003Z/log/*.log`.
TSV results in `/home/hermes/workspace/repos/omnigent-eval-rebuild-upstream-0.7/docs/rebuild/canary-runs/D1-20260802T095003Z/results.tsv`.
Env snapshot in `/home/hermes/workspace/repos/omnigent-eval-rebuild-upstream-0.7/docs/rebuild/canary-runs/D1-20260802T095003Z/canary.env`.
