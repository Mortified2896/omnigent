"""Helpers used by ``scripts/promote_release.sh``,
``scripts/deploy_status.sh``, and ``scripts/rollback_release.sh``.

The shell scripts are the canonical command surface; this package
contains the Python functions that are easier to read and test in
isolation. Each module is small and single-purpose:

* :mod:`omnigent.deploy.ops.layout` defines ``deploy_root``, the
  canonical host path, and helpers to resolve ``current``,
  ``previous``, ``releases/<sha>/``, ``manifests/<sha>.json`` etc.
* :mod:`omnigent.deploy.ops.release_id` parses and validates commit
  SHAs/short SHAs/refs into the canonical full SHA used for release
  directories.
* :mod:`omnigent.deploy.ops.systemd` writes the small set of
  config fragments the long-term deploy needs.

These helpers do not shell out to ``systemctl`` — that is the job of
the scripts. The Python layer deals in files and in-process checks so
it can be unit-tested without touching the live systemd state.
"""
