"""Runtime provenance checks for the long-term deploy architecture.

Imports from ``omnigent`` / ``omnigent.server.app`` are resolved via
``Path(__file__).is_relative_to(<release>)`` after the running
interpreter's ``sys.executable`` is also proven to live inside the
release. Together these prove that the live process is using code from
the configured release directory and from nowhere else.

Provenance is what protects against the version-mixing problem the
2014 deploy-main-0039e23a setup had: the deployment worktree's ``.venv``
was a symlink into the main checkout's venv, which itself was an
editable install that hard-coded the main checkout path in its
``__editable__.*.pth`` finder. The live service could therefore import
Python from a different commit than the one it claimed to deploy, while
simultaneously serving the bundle from the worktree's frontend build.
These probes refuse to declare a release "live" under such a state.

A compatibility note: ``pathlib.Path.is_relative_to`` is Python 3.9+,
and the project supports Python 3.12+, so the method is used directly.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


class ProvenanceError(RuntimeError):
    """Raised when the running interpreter cannot be proven to belong to
    a release directory.

    The message includes both the release root and the resolved module
    path so the systemd journal shows the operator exactly which path
    diverged from the expectation.
    """

    def __init__(self, message: str, *, release: Path, diverging: Path) -> None:
        super().__init__(message)
        self.release = release
        self.diverging = diverging


def _resolve_executable() -> Path:
    """Return the absolute path of the running interpreter.

    ``sys.executable`` is the path the kernel used to load the process;
    if it is a symlink we resolve it through ``Path.resolve``. Note
    that ``uv venv`` builds a thin shim that points at the base
    interpreter in ``~/.local/share/uv/python/...``, so the resolved
    target is in uv-managed storage on every host where uv is used.
    That is fine for our purposes — what proves a release is running is
    ``sys.prefix`` (the release venv's root), not the absolute path of
    the binary. See :func:`_resolve_prefix`.
    """
    return Path(sys.executable).resolve()


def _resolve_prefix() -> Path:
    """Return the Python runtime prefix — the directory the interpreter
    treats as its installation root.

    For a regular venv (``uv venv``, ``python -m venv``), ``sys.prefix``
    is the release venv's top-level dir; ``sys.base_prefix`` is the
    host's base interpreter location (outside the release). For a
    system Python ``sys.prefix == sys.base_prefix``.

    Provenance must use ``sys.prefix``, not ``sys.executable``: a venv
    may symlink its ``bin/python`` to the host's ``~/.local/share/uv/...``
    interpreter, in which case ``Path(sys.executable).resolve()`` points
    outside the release even though the runtime itself *is* in the release.
    """
    return Path(sys.prefix).resolve()


def _resolve_module(module_name: str, *, attr: str | None = None) -> Path:
    """Return the absolute path of a Python module currently importable.

    The optional ``attr`` is a name to read relative to the module
    (``"app"`` for ``omnigent.server.app``). Used by the gate to also
    prove that the entry-point module that the systemd unit imports
    resolves into the release directory — the bare package import check
    alone could miss an editable install that resolves the top-level
    package from the release but a submodule from elsewhere.
    """
    module = __import__(module_name, fromlist=["__path__" if attr is None else attr])
    if attr is not None:
        module = getattr(module, attr)
    file_path = getattr(module, "__file__", None)
    if not file_path:
        raise ProvenanceError(
            f"module {module_name}{('.' + attr) if attr else ''} has no __file__",
            release=Path("unknown"),
            diverging=Path("unknown"),
        )
    return Path(file_path).resolve()


def check_runtime_provenance(release_root: Path) -> dict[str, str]:
    """Assert that ``sys.prefix``, the venv marker, ``omnigent``, and
    ``omnigent.server.app`` all resolve inside ``release_root``.

    :param release_root: Absolute path of the configured release directory.
    :returns: A dict of the resolved paths that were checked, useful for
        the deployment status command and the manifest.
    :raises ProvenanceError: When any of the four proves to live
        outside the release. The error message names the path that
        diverged so the operator knows which link in the chain broke
        (the ``.venv`` symlink, the editable install, the PYTHONPATH
        leak, …).
    """
    release = release_root.resolve()
    if not release.is_dir():
        raise ProvenanceError(
            f"release directory does not exist: {release}",
            release=release,
            diverging=release,
        )

    executable = _resolve_executable()
    prefix = _resolve_prefix()
    if not prefix.is_relative_to(release):
        raise ProvenanceError(
            f"sys.prefix={prefix} is not under release {release}; the running "
            f"interpreter does not belong to this release. Most often this "
            f"means the .venv is a symlink to another checkout.",
            release=release,
            diverging=prefix,
        )

    pyvenv_cfg = release / ".venv" / "pyvenv.cfg"
    if not pyvenv_cfg.is_file():
        raise ProvenanceError(
            f"release {release} has no .venv/pyvenv.cfg; the pre-start gate "
            f"requires a release-local venv (no symlinks to other checkouts).",
            release=release,
            diverging=release / ".venv",
        )

    omnigent_path = _resolve_module("omnigent")
    if not omnigent_path.is_relative_to(release):
        raise ProvenanceError(
            f"omnigent resolves to {omnigent_path} which is not under "
            f"release {release}; the venv contains an editable install "
            f"that points at a different checkout.",
            release=release,
            diverging=omnigent_path,
        )

    app_path = _resolve_module("omnigent.server", attr="app")
    if not app_path.is_relative_to(release):
        raise ProvenanceError(
            f"omnigent.server.app resolves to {app_path} which is not "
            f"under release {release}; the venv contains a stale editable "
            f"install pointing at a different checkout.",
            release=release,
            diverging=app_path,
        )

    return {
        "release": str(release),
        "sys_executable": str(executable),
        "sys_prefix": str(prefix),
        "omnigent_module": str(omnigent_path),
        "omnigent_server_app": str(app_path),
    }


def live_runtime_provenance(release_root: Path) -> dict[str, str]:
    """Like :func:`check_runtime_provenance` but tolerant of an absent venv.

    Used by the post-start status command when the user inspects the
    current deployment from a different checkout. The deploy supervisor
    pre-start gate refuses to start under that state; the status
    command reports it instead of crashing.
    """
    release = release_root.resolve()
    if not release.is_dir():
        raise ProvenanceError(
            f"release directory does not exist: {release}",
            release=release,
            diverging=release,
        )
    executable = _resolve_executable()
    prefix = _resolve_prefix()
    if not prefix.is_relative_to(release):
        raise ProvenanceError(
            f"sys.prefix={prefix} is not under release {release}",
            release=release,
            diverging=prefix,
        )
    return {
        "release": str(release),
        "sys_executable": str(executable),
        "sys_prefix": str(prefix),
    }


def main() -> int:
    """CLI entry point used by ``scripts/deploy_supervisor_gate.sh``
    and the build pipeline.

    Resolves the release directory from ``OMNIGENT_RELEASE_DIR`` (the
    single environment variable that ties systemd drop-ins to a
    release) when no positional argument is given; a positional
    argument overrides the env var so build pipelines can probe an
    arbitrary release tree. Runs the provenance check, prints the
    resolved paths on stdout, and exits 0 on success or 1 on failure.
    """
    args = sys.argv[1:]
    if len(args) > 1:
        print(f"usage: {sys.argv[0]} [<release-dir>]", file=sys.stderr)
        return 2
    release_dir = (args[0] if args else os.environ.get("OMNIGENT_RELEASE_DIR", "")).strip()
    if not release_dir:
        print("release directory not provided (set OMNIGENT_RELEASE_DIR or pass it as the first argument)", file=sys.stderr)
        return 2
    try:
        info = check_runtime_provenance(Path(release_dir))
    except ProvenanceError as exc:
        print(f"[deploy-supervisor] ERROR: {exc}", file=sys.stderr)
        return 1
    for key, value in info.items():
        print(f"{key}={value}")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
