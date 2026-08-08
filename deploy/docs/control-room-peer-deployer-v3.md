# Control Room peer deployer v3

`peer_promote_o1_v3.py` is the approved host-side O2 -> O1 promotion orchestrator for the Control Room 0.9 rollout.

It supersedes `peer_promote_o1_v2.sh` for live promotion. The old v2 implementation remains available in Git history for incident analysis, but the live `peer_promote_o1_v2.sh` path is now a hard refusal shim that exits non-zero and points operators to v3. This prevents accidental reuse of the unsafe path.

The v2 implementation was rejected because its rollback selected `venv.legacy-*` globally and its shell control flow did not guarantee paired rollback for every post-mutation failure.

## Direction invariant

- Target O2 -> supervisor O1.
- Target O1 -> supervisor O2.
- Target and supervisor must never be the same instance.

For the current 0.9 handoff:

- supervisor: O2
- target: O1
- accepted runtime: `541c9a3180b81bfb2fc450b3ef5f8648691b359d`
- accepted version: `0.9.0.dev0`
- O1 pre-promotion runtime: `e5f4249667a1602916d44ac62d10b921a299f05d` / `0.8.1`
- O1 pre-promotion DB schema: `c4d5e6f7a8b9`
- O1 target DB schema: `f7a8b9c0d1e2`

## Safety properties

The v3 orchestrator:

1. runs only as root on the host, outside both Omnigent service sandboxes;
2. hard-codes O1 as target and O2 as supervisor for this handoff;
3. proves O2 health/runtime identity and snapshots O2 before touching O1;
4. verifies the exact accepted wheel hashes from O2;
5. proves O1 is the expected healthy 0.8.1 starting state;
6. creates a transaction record and verified SQLite backup;
7. stages under `/opt/omnigent/staging/<TX_ID>` using the frozen O2 dependency closure;
8. crosses the mutation boundary only after the staged runtime is complete and verified;
9. stops O1 before renaming/switching runtime paths;
10. records the exact transaction-specific old-runtime rollback path (`venv.legacy-<TX_ID>` for a directory-based old runtime);
11. catches every Python failure/interrupt after mutation and attempts paired runtime + DB rollback;
12. never chooses an old runtime by wildcard/glob;
13. never marks rollback complete until the restored runtime, DB schema, DB integrity, services, and health endpoint are verified;
14. never deletes the failed final candidate during rollback; evidence is preserved;
15. compares the O2 guard snapshot after the operation and refuses to commit if O2 drifted.

## Host usage

Run a non-mutating staged preflight first from a clean checkout/worktree at the reviewed hardening commit:

```bash
sudo -E /opt/omnigent-production/current/venv/bin/python \
  /path/to/hardened-checkout/deploy/scripts/peer_promote_o1_v3.py \
  --preflight-only
```

Only after that passes, run the real promotion from the same exact Git commit:

```bash
sudo -E /opt/omnigent-production/current/venv/bin/python \
  /path/to/hardened-checkout/deploy/scripts/peer_promote_o1_v3.py \
  --promote
```

Do not copy/paste the Python file out of Git and do not run the v2 path. The v2 path is intentionally non-operational.

## Failure contract

Before mutation: failure leaves O1 untouched; only that transaction's staging directory may be cleaned automatically.

After mutation: failure triggers paired rollback of the exact transaction-specific old runtime and the verified DB backup. If rollback itself cannot prove or restore the expected state, the tool returns a CRITICAL verdict and preserves all evidence instead of guessing.
