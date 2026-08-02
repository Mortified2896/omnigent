# Canary D.1 report — D-final3-20260802T102547Z

| Rebuild SHA | Run ID | Phase | Port | Prod port | Data dir | Auth header |
| --- | --- | --- | --- | --- | --- | --- |
| `11edcc0918ac71e97e7430822f0d9b01349bd923` | `D-final3-20260802T102547Z` | D.1 (initial) | `6767` | `6767` | `/tmp/canary-data` | `X-Forwarded-Email` |

## Summary

| # | Check | Status | Duration |
| --- | --- | --- | --- |
| 01 | `01_db_init` | **FAIL** | 1s |
| 02 | `02_web_health` | **FAIL** | 0s |
| 03 | `03_omniroute` | **FAIL** | 0s |
| 04 | `04_pi_repo_edit` | **FAIL** | 0s |
| 05 | `05_pi_commit` | **FAIL** | 0s |
| 06 | `06_pi_push` | **FAIL** | 0s |
| 07 | `07_opencode_repo_edit` | **FAIL** | 0s |
| 08 | `08_opencode_commit` | **FAIL** | 0s |
| 09 | `09_opencode_push` | **FAIL** | 0s |
| 10 | `10_langfuse` | **FAIL** | 0s |
| 11 | `11_verity_delegation` | **FAIL** | 0s |
| 12 | `12_worktree_isolation` | **FAIL** | 0s |

## Evidence

### `01_db_init` — FAIL

```
note: did not observe "Running database migrations" in journalctl (wheel may have skipped the log line; not a FAIL for the temp foreground launcher)
db_path=/tmp/canary-data/chat.db
alembic_head=b3c4d5e6f7a8
data_dir=/tmp/canary-data
tables=account_tokens agents alembic_version comments conversation_items conversation_items_fts conversation_items_fts_config conversation_items_fts_content conversation_items_fts_data conversation_items_fts_docsize conversation_items_fts_idx conversation_labels conversations device_grants files hosts omnigent_conversation_metadata policies projects scheduled_task_runs scheduled_tasks session_permissions user_daily_cost users
PASS
```

### `02_web_health` — FAIL

```
FAIL canary port 6767 equals production port 6767 — Phase D isolation broken
```

### `03_omniroute` — FAIL

```
{"reason": "OmniRoute POST /routes:select returned status=404 body=b'<!DOCTYPE html><html lang=\"en\" dir=\"ltr\"><head><meta charSet=\"utf-8\"/><meta name=\"viewport\" content=\"width=device-width, initial-scale=1, viewport-fit=cover\"/><link rel=\"stylesheet\" href=\"/_next/stati'", "status": "FAIL"}
```

### `04_pi_repo_edit` — FAIL

```
SKIPPED (harness binary missing: pi)
```

### `05_pi_commit` — FAIL

```
SKIPPED (harness binary missing: pi)
```

### `06_pi_push` — FAIL

```
SKIPPED (harness binary missing: pi)
```

### `07_opencode_repo_edit` — FAIL

```
Preparing worktree (new branch 'polly/canary-7-opencode-D-final3-20260802T102547Z')
curl: (7) Failed to connect to 127.0.0.1 port 6767 after 0 ms: Couldn't connect to server
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
FAIL could not resolve agent_id for agent_name=opencode-native-ui on port 6767
FAIL function bar not present in diff
```

### `08_opencode_commit` — FAIL

_(no evidence captured)_

### `09_opencode_push` — FAIL

```
FAIL no git.commit.outcome event in session JSON
```

### `10_langfuse` — FAIL

```
{"reason": "LANGFUSE_HOST / LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY must be set", "status": "FAIL"}
```

### `11_verity_delegation` — FAIL

```
Preparing worktree (new branch 'polly/canary-11-verity-D-final3-20260802T102547Z')
curl: (7) Failed to connect to 127.0.0.1 port 6767 after 0 ms: Couldn't connect to server
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
```

### `12_worktree_isolation` — FAIL

```
SKIPPED (harness binary missing: pi)
```

## Per-run artifacts

Full per-check stdout+stderr captured under `/home/hermes/workspace/repos/omnigent-eval-rebuild-upstream-0.7/docs/rebuild/canary-runs/D-final3-20260802T102547Z/log/*.log`.
TSV results in `/home/hermes/workspace/repos/omnigent-eval-rebuild-upstream-0.7/docs/rebuild/canary-runs/D-final3-20260802T102547Z/results.tsv`.
Env snapshot in `/home/hermes/workspace/repos/omnigent-eval-rebuild-upstream-0.7/docs/rebuild/canary-runs/D-final3-20260802T102547Z/canary.env`.
