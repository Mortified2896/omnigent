"""Release-directory supervisor for the omnigent-eval-web systemd service.

The web service is started by systemd with a ``WorkingDirectory`` and
``ExecStart`` written to a host-side release directory
(``/home/hermes/workspace/deployments/omnigent/releases/<sha>/``). The
pre-start gate this package exposes is invoked from ``ExecStartPre=``
and refuses to start the unit unless five invariants hold:

1. The configured release directory exists and contains an embedded
   ``.venv/`` (not a symlink to another directory).
2. The release directory is the working directory of the systemd unit.
3. ``$RELEASE/.venv/bin/python`` resolves ``omnigent`` and
   ``omnigent.server.app`` to file paths under the same release.
4. The release is at the expected commit (``OMNIGENT_RELEASE_EXPECTED_SHA``).
5. Either the web UI bundle preflight passes, or the deployment is
   marked explicit API-only via ``OMNIGENT_SKIP_WEB_UI=true``.

Failing any of those gates aborts systemd startup with a runbook-quality
message in the journal. The web service is not a silent fallback; an
accidental API-only deployment of a UI service now exits non-zero, not
"serves the API-only landing page and hopes nobody notices".

The gate is independent of the promotion script so that an LLM agent or
operator that hand-writes a drop-in cannot bypass it: the supervisor
reads the drop-in's ``OMNIGENT_RELEASE_DIR`` and runs the checks on the
configured release, not on whatever worktree an agent happened to
create. The promotion script itself is just a convenience that calls
the same checks during candidate validation.
"""

# Lazy exports so ``from omnigent.deploy.supervisor import run_gate``
# works for tests but ``python -m omnigent.deploy.supervisor.gate`` and
# the runpy-driven ``-m omnigent.deploy.supervisor.canary`` entrypoints
# do not eagerly import the whole supervisor tree at startup.
from typing import TYPE_CHECKING

__all__ = [
    "CanaryError",
    "GateError",
    "ManifestError",
    "ProvenanceError",
    "ReleaseManifest",
    "manifest_path_for",
    "run_canary",
    "run_gate",
    "verify_manifest_commit",
    "write_manifest",
    "load_manifest",
]


if TYPE_CHECKING:  # pragma: no cover - re-export shim
    from omnigent.deploy.supervisor.canary import CanaryError, run_canary
    from omnigent.deploy.supervisor.gate import GateError, run_gate
    from omnigent.deploy.supervisor.manifest import (
        ManifestError,
        ReleaseManifest,
        load_manifest,
        manifest_path_for,
        verify_manifest_commit,
        write_manifest,
    )
    from omnigent.deploy.supervisor.provenance import ProvenanceError


_LAZY_MAP = {
    "CanaryError": "omnigent.deploy.supervisor.canary",
    "run_canary": "omnigent.deploy.supervisor.canary",
    "GateError": "omnigent.deploy.supervisor.gate",
    "run_gate": "omnigent.deploy.supervisor.gate",
    "ManifestError": "omnigent.deploy.supervisor.manifest",
    "ReleaseManifest": "omnigent.deploy.supervisor.manifest",
    "load_manifest": "omnigent.deploy.supervisor.manifest",
    "manifest_path_for": "omnigent.deploy.supervisor.manifest",
    "verify_manifest_commit": "omnigent.deploy.supervisor.manifest",
    "write_manifest": "omnigent.deploy.supervisor.manifest",
    "ProvenanceError": "omnigent.deploy.supervisor.provenance",
}


def __getattr__(name: str):
    module_name = _LAZY_MAP.get(name)
    if module_name is None:
        raise AttributeError(f"module 'omnigent.deploy.supervisor' has no attribute {name!r}")
    import importlib

    module = importlib.import_module(module_name)
    value = getattr(module, name)
    globals()[name] = value
    return value
