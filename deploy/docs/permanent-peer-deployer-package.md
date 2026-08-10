# Permanent peer-deployer bootstrap package contract

This document pins the **canonical** layout of the ONE-TIME peer-deployer
bootstrap handoff directory handed to the operator as
`sudo bash <dir>/bootstrap-installer.sh`.

It is the single source of truth for the contract between the
deterministic builder (`deploy/scripts/build_control_room_peer_deployer_bootstrap.py`)
and the wrapper (`deploy/scripts/bootstrap-installer.sh`).

## 2026-08-10 incident

The first handoff produced under this contract put
`bootstrap-manifest.json` **inside** the tarball but **not** at the
outer canonical path the wrapper expects. The wrapper refused to
proceed and the operator saw a hard `missing manifest` error with no
persistent state mutated. The contract was pinned after this
incident so the failure mode can never recur.

## Canonical layout

```
<handoff-dir>/
    bootstrap-installer.sh                          (regular file, executable)
    peer-deployer-package/
        peer-deployer-package.tar.gz                (regular file)
        bootstrap-manifest.json                     (regular file, schema v2)
        PACKAGE.json                                (regular file, schema v1)
        SHA256SUMS                                  (regular file)
```

The wrapper does **not** tolerate any of:

- directory missing
- any of the five files missing
- any of the five files being a symlink
- the wrapper itself being a symlink

## Two integrity layers

The handoff has two integrity layers, explicitly distinct:

### A. Outer handoff/package integrity (wrapper preflight)

The wrapper verifies, before any privileged mutation:

1. **PACKAGE.json** parses and matches `control-room-peer-deployer.bootstrap-package.v1`.
2. `manifest_schema` field is `control-room-peer-deployer.bootstrap-manifest.v2`.
3. `build_id` is a 40-char hex SHA.
4. `expected_outer_paths` lists every required outer file.
5. **SHA256SUMS** verifies the tarball and the outer manifest byte-for-byte.
6. The tarball has no absolute paths, no `..` traversal, no device nodes,
   no symlinks, no FIFOs, and a sane top-level structure (single `./`
   directory entry followed by `./bootstrap-manifest.json`).
7. The outer `bootstrap-manifest.json` parses as schema v2 with a
   matching `build_id` and a `file_count` equal to `len(hashes)`.
8. The inner manifest embedded in the tarball is **byte-equal** to the
   outer manifest. This is the ortho-checksum that prevents the
   "outer ≠ inner" mismatch.

### B. Inner source payload integrity (committed bootstrap module)

After the wrapper preflight passes, the wrapper invokes the committed
bootstrap module (`deploy/scripts/control_room_peer_deployer_bootstrap.py`)
from the unpacked tarball. The module verifies the extracted payload
against the same `bootstrap-manifest.json` (now under
`./bootstrap-manifest.json` inside the unpacked tree).

The two layers are deliberately independent. The wrapper proves that
the package as handed to the operator is internally consistent; the
bootstrap module proves that the verified payload is intact after
extraction.

## Path resolution

The wrapper resolves its own directory via `BASH_SOURCE[0]`. It does
**not** depend on the operator's cwd, `$HOME`, `$PWD`, or any
environment variable. The wrapper assumes only that the operator is
running it as `sudo bash <absolute-path>/bootstrap-installer.sh` from
any directory.

## `--verify-only`

The wrapper accepts:

```
bash /absolute/path/to/bootstrap-installer.sh --verify-only
bash /absolute/path/to/bootstrap-installer.sh -V
```

`--verify-only` runs the layer A (outer) checks, extracts the tarball
to a disposable tmp directory for the inner payload verification, then
**stops before any persistent mutation**. It does not create
`/opt/control-room-peer-deployer`, `/var/lib/control-room-peer-deployer`,
or `/run/control-room-peer-deployer`. It does not install a systemd
unit. It does not reload systemd. It does not restart anything.

Always run `--verify-only` against the **exact** final handoff
directory before handing the package to the operator.

## Deterministic builder

The handoff directory is produced by:

```
python deploy/scripts/build_control_room_peer_deployer_bootstrap.py \
    --source /path/to/clean-checkout \
    --build-id 1ff00da515f35abb82bd0a6ff671bd49459d4297 \
    --output /var/lib/omnigent-production/permanent-peer-deployer-bootstrap-<stamp>
```

The source checkout must be symlink-free (the builder refuses any
symlink). A clean copy via `rsync -a --no-links` is the simplest way
to produce a symlink-free source.

The builder refuses to overwrite an existing `--output` unless `--force`
is passed.

## Operator command

```
sudo bash /var/lib/omnigent-production/permanent-peer-deployer-bootstrap-<stamp>/bootstrap-installer.sh
```

The wrapper performs the preflight and then hands off to the bootstrap
module. The privileged bootstrap runs only when the preflight passes.

## Disallowed

The operator must not:

- copy the wrapper out of the handoff directory
- move files inside the handoff directory
- edit any file inside the handoff directory
- hand the wrapper a relative path or a `cd` followed by a `bash`
- re-run the wrapper if it failed verification (the wrapper is
  fail-closed; the operator must rebuild the handoff from the builder)
