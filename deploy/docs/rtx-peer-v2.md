# RTX peer deployment mechanism

This document describes the **server-side** O1/O2 deployment mechanism implemented
by `python -m peer_deployer.rtx`. It is not general agent guidance and it does
not identify the agent running a task.

O1 and O2 are runtime instances. A Mac/desktop Codex session or another external
operator is neither instance and may manage either one when the user has
authorized deployment.

## When this document applies

Use this mechanism only when all of the following are true:

- the task actually requires changing a live Omnigent runtime;
- current host inspection confirms the O1/O2 topology exists;
- the reviewed source contains the matching `peer_deployer.rtx` implementation;
- the user has authorized the deployment action.

Do not use this document to infer current ports, PIDs, release SHAs, service
names, or whether a peer exists. Those are live-state facts owned by the current
HomeLab configuration and host inspection.

## Stable safety contract

Each runtime has isolated writable state. Candidate artifacts are immutable and
accepted before promotion. Target and supervisor identities are explicit and
must be different when peer supervision is used. A promotion records its
transaction before mutating active state, preserves rollback evidence, validates
database integrity, and restores the target on failed startup.

The mechanism must fail closed when its topology assumptions are not satisfied.
A missing peer is not an instruction to create or revive one.

## Verification

Run the focused deployment tests when this mechanism changes:

```sh
.venv/bin/python -m pytest tests/deploy/test_rtx_peer_v2.py \
  tests/server/test_instance_cookie.py \
  tests/server/test_accounts.py \
  tests/server/test_host_registry.py
```

For current topology, deployment paths, and operational commands, use the
current HomeLab documentation and live host state. Git history retains prior
rollout evidence; it does not belong in this contract.
