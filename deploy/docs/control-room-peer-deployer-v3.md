# Control Room peer deployer v3

`peer_promote_o1_v3.py` is the canonical host wrapper. Its historical filename
is retained, but it safely supports both directions:

- target O2, supervisor O1;
- target O1, supervisor O2.

Target and supervisor are always explicit and must differ. Artifact identity is
provided only by an immutable, versioned acceptance record; active deployer
source contains no release SHA/version/wheel constants.

## Modes

- `bootstrap-first-peer`: activate the already booted and accepted candidate at
  the target's immutable release path. The supervisor remains healthy but may
  run an older artifact. Bootstrap does not copy the old supervisor closure.
- `peer-copy`: require the supervisor to run the accepted SHA/version, then
  reproduce that exact accepted candidate on the target.

Both modes run common preflight checks, including the active/unlatched storage
guard. Mode-specific checks are separate. There is no force or skip option.

## Acceptance and transaction evidence

The canonical record is
`/var/lib/omnigent-control-room/accepted-artifacts/<sha>/acceptance.json`.
Its embedded digest covers canonical payload bytes excluding only the digest
field. It binds wheels, frontend tree, runtime and installed packages, `uv pip
check`, temporary-port boot probes, disk headroom, timestamp, and non-secret
builder/operator identities.

Transactions persist mode, acceptance path/hash, frontend hash, exact wheel
filenames/hashes, and the supervisor SHA/version/PID/active-timestamp baseline.
Pre-existing acceptance files and candidates are `referenced_resources`, never
rollback-owned. Only transaction-created paths enter `owned_resources`.

## Host usage

Run preflight from the reviewed checkout:

```bash
sudo -E /path/to/python deploy/scripts/peer_promote_o1_v3.py \
  --preflight-only \
  --target O1 --supervisor O2 \
  --mode bootstrap-first-peer \
  --acceptance-record /var/lib/omnigent-control-room/accepted-artifacts/<sha>/acceptance.json \
  --evidence-dir /var/lib/omnigent-control-room/evidence/<operation>
```

After it passes, use the same explicit arguments and replace
`--preflight-only` with `--promote`. For the next peer, reverse target and
supervisor and use `--mode peer-copy`.

## Failure contract

Before the active-runtime mutation boundary, failures cannot touch the active
runtime or DB; only transaction-owned staging may be removed. After mutation,
failures trigger paired restoration of the exact recorded runtime and verified
DB backup. Referenced acceptance/candidate resources are preserved. The
supervisor baseline is compared after acceptance and rollback; any SHA,
version, PID, or active-timestamp drift fails closed.
