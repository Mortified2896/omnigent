# Canary D.1 report — D7-20260802T100252Z

| Rebuild SHA | Run ID | Phase | Port | Prod port | Data dir | Auth header |
| --- | --- | --- | --- | --- | --- | --- |
| `113340bea2daa1ffca3a1e64fcd2dd85b534cf14` | `D7-20260802T100252Z` | D.1 (initial) | `17670` | `17670` | `/tmp/canary-data` | `X-Forwarded-Email` |

## Summary

| # | Check | Status | Duration |
| --- | --- | --- | --- |
| 01 | `01_db_init` | **FAIL** | 0s |
| 02 | `02_web_health` | **FAIL** | 0s |
| 03 | `03_omniroute` | **FAIL** | 0s |
| 04 | `04_pi_repo_edit` | **FAIL** | 0s |
| 05 | `05_pi_commit` | **FAIL** | 0s |
| 06 | `06_pi_push` | **FAIL** | 0s |
| 07 | `07_opencode_repo_edit` | **FAIL** | 1s |
| 08 | `08_opencode_commit` | **FAIL** | 0s |
| 09 | `09_opencode_push` | **FAIL** | 0s |
| 10 | `10_langfuse` | **FAIL** | 0s |
| 11 | `11_verity_delegation` | **FAIL** | 0s |
| 12 | `12_worktree_isolation` | **FAIL** | 1s |

## Evidence

### `01_db_init` — FAIL

```
FAIL expected table inbox missing from fresh schema; got: account_tokens agents alembic_version comments conversation_items conversation_items_fts conversation_items_fts_config conversation_items_fts_content conversation_items_fts_data conversation_items_fts_docsize conversation_items_fts_idx conversation_labels conversations device_grants files hosts omnigent_conversation_metadata policies projects scheduled_task_runs scheduled_tasks session_permissions user_daily_cost users
```

### `02_web_health` — FAIL

```
FAIL canary port 17670 equals production port 17670 — Phase D isolation broken
```

### `03_omniroute` — FAIL

```
{"reason": "OmniRoute POST /routes:select returned status=404 body=b'<!DOCTYPE html><html lang=\"en\" dir=\"ltr\"><head><meta charSet=\"utf-8\"/><meta name=\"viewport\" content=\"width=device-width, initial-scale=1, viewport-fit=cover\"/><link rel=\"stylesheet\" href=\"/_next/stati'", "status": "FAIL"}
```

### `04_pi_repo_edit` — FAIL

```
Preparing worktree (new branch 'polly/canary-4-pi-D7-20260802T100252Z')
/home/hermes/workspace/repos/omnigent-eval-rebuild-upstream-0.7/deploy/rebuild/tests/checks/04_pi_repo_edit.sh: 126: SECONDS: parameter not set
```

### `05_pi_commit` — FAIL

```
FAIL no git.commit.outcome event in session JSON at /tmp/canary-fixtures/D7-20260802T100252Z/pi/worktree/.session.json
```

### `06_pi_push` — FAIL

```
FAIL no git.commit.outcome event in session JSON
```

### `07_opencode_repo_edit` — FAIL

```
Preparing worktree (new branch 'polly/canary-7-opencode-D7-20260802T100252Z')
/home/hermes/workspace/repos/omnigent-eval-rebuild-upstream-0.7/deploy/rebuild/tests/checks/07_opencode_repo_edit.sh: 126: SECONDS: parameter not set
```

### `08_opencode_commit` — FAIL

```
FAIL no git.commit.outcome event in session JSON
```

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
Preparing worktree (new branch 'polly/canary-11-verity-D7-20260802T100252Z')
/home/hermes/workspace/repos/omnigent-eval-rebuild-upstream-0.7/deploy/rebuild/tests/checks/11_verity_delegation.sh: 128: SECONDS: parameter not set
```

### `12_worktree_isolation` — FAIL

```
Preparing worktree (new branch 'polly/canary-12-pi-a-D7-20260802T100252Z')
Preparing worktree (new branch 'polly/canary-12-pi-b-D7-20260802T100252Z')
/home/hermes/workspace/repos/omnigent-eval-rebuild-upstream-0.7/deploy/rebuild/tests/checks/12_worktree_isolation.sh: 126: SECONDS: parameter not set
/home/hermes/workspace/repos/omnigent-eval-rebuild-upstream-0.7/deploy/rebuild/tests/checks/12_worktree_isolation.sh: 126: SECONDS: parameter not set
FAIL no git.commit.outcome in session A
```

## Per-run artifacts

Full per-check stdout+stderr captured under `/home/hermes/workspace/repos/omnigent-eval-rebuild-upstream-0.7/docs/rebuild/canary-runs/D7-20260802T100252Z/log/*.log`.
TSV results in `/home/hermes/workspace/repos/omnigent-eval-rebuild-upstream-0.7/docs/rebuild/canary-runs/D7-20260802T100252Z/results.tsv`.
Env snapshot in `/home/hermes/workspace/repos/omnigent-eval-rebuild-upstream-0.7/docs/rebuild/canary-runs/D7-20260802T100252Z/canary.env`.
