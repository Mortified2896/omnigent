"""Runtime provenance checks for the long-term deploy architecture.

Proves that the running interpreter is the configured release's
``.venv/bin/python`` *and* that ``omnigent`` resolves from that
release's ``site-packages`` directory — never from the repository
checkout, the bare release source root, another worktree, or another
virtual environment. Together these are what block the
version-mixing bug the 2014 ``deploy-main-0039e23a`` setup had: the
deployment worktree's ``.venv`` was a symlink into the main checkout's
venv, which itself was an editable install that hard-coded the main
checkout path in its ``__editable__.*.pth`` finder. The live service
could therefore import Python from a different commit than the one it
claimed to deploy while serving the bundle from the worktree's
frontend build. These probes refuse to declare a release "live" under
that state.

A note on Python internals: we deliberately use ``sysconfig``,
``site``, ``sys.prefix``, and resolved paths instead of hard-coding
``lib/python3.12/site-packages`` so the check survives Python minor
version changes (3.12 → 3.13 → 3.14) without modification. The
``-P`` flag (``--no-site`` is unrelated; ``-P`` is
``--no-path`` / "don't prepend a potentially unsafe path to
``sys.path``", see ``python --help``) prevents Python from injecting
the current working directory into ``sys.path[0]`` — that is the
specific knob that lets ``cd /path/to/checkout`` shadow a clean
site-packages install.

A compatibility note: ``pathlib.Path.is_relative_to`` is Python 3.9+,
and the project supports Python 3.12+, so the method is used directly.
"""

from __future__ import annotations

import importlib
import os
import re
import site
import sys
import sysconfig
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


def _release_site_packages(release: Path) -> Path:
    """Return the canonical ``site-packages`` directory owned by the
    release's ``.venv``.

    ``uv venv`` and ``python -m venv`` both create
    ``<venv>/lib/pythonX.Y/site-packages`` where ``X.Y`` is the
    runtime's major.minor version. We resolve that path from
    ``sysconfig.get_python_version()`` rather than hardcoding
    ``3.12`` so the check survives Python minor version changes
    without modification.

    The pyvenv.cfg ``home`` directive is intentionally *not* used:
    it points at the base interpreter's ``bin/`` directory (the
    shim that ``bin/python`` symlinks to), which is outside the
    release and on a uv-managed base — its ``lib/pythonX.Y``
    is the *base* interpreter's lib, not the venv's lib. Reading
    it would point us at the wrong site-packages and false-pass
    provenance on every host that uses uv.
    """
    version = sysconfig.get_python_version()
    return (release / ".venv" / "lib" / f"python{version}" / "site-packages").resolve()


def _iter_pth_files(prefix: Path) -> list[Path]:
    """Return every ``.pth`` file under the release venv's site-packages.

    Editable installs leave a ``__editable__*.pth`` entry here that
    adds a foreign checkout to ``sys.path``. ``site.addpackage`` walks
    these files at interpreter startup; we walk the same set so we can
    reject finders that point outside the release's site-packages.
    """
    site_packages = _release_site_packages(prefix)
    if not site_packages.is_dir():
        return []
    return sorted(site_packages.glob("*.pth"))


_EDITABLE_PTH = re.compile(r"_editable[_-]?(impl|finder)?[_-]?\w*", re.IGNORECASE)


def _find_editable_finder_modules(site_packages: Path) -> list[tuple[str, Path]]:
    """Return ``(module_name, source_file)`` for every loaded
    ``__editable__*`` finder that lives inside ``site_packages``.

    The metadata-backed editable install pattern (used by ``uv pip
    install -e`` and ``pip install -e``) writes a
    ``__editable__<name>_<version>_finder.py`` module to the
    target venv's site-packages; the module registers a
    ``MetaPathFinder`` whose ``MAPPING`` is the foreign checkout
    paths. We must refuse to start the service when any such finder
    is loaded from the *release's* venv — even if it does not happen
    to win the import for ``omnigent`` today, it can shadow another
    module tomorrow, and the venv is supposed to be immutable.

    The site-packages filter is critical: a test process running an
    editable install for its own convenience (the repo checkout
    itself uses an editable install) must not be confused with the
    release venv being editable. Only finders whose source file
    lives inside the release's site-packages trigger the rejection.
    """
    found: list[tuple[str, Path]] = []
    for name in sorted(sys.modules):
        if not _EDITABLE_PTH.search(name):
            continue
        module = sys.modules[name]
        origin = getattr(module, "__file__", None)
        if origin is None:
            continue
        resolved = Path(origin).resolve()
        try:
            if not resolved.is_relative_to(site_packages):
                continue
        except OSError:
            continue
        found.append((name, resolved))
    return found


def _check_pth_no_foreign_paths(prefix: Path, release: Path) -> None:
    """Verify every ``.pth`` file inside the release's site-packages
    maps only to directories *inside* that site-packages tree.

    A venv that's been touched by ``pip install -e /path/to/checkout``
    or a sibling-package editable install will leave ``.pth`` entries
    whose values point at the source tree. Those entries bypass the
    immutable site-packages guarantee; provenance must reject them
    even if they don't currently affect ``omnigent``.
    """
    site_packages = _release_site_packages(prefix)
    if not site_packages.is_dir():
        return
    offenders: list[str] = []
    for pth in _iter_pth_files(prefix):
        try:
            text = pth.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or line.startswith("import "):
                continue
            # ``.pth`` files use ``/absolute/path`` or ``/abs/path`` to
            # inject a directory into ``sys.path``. Relative paths
            # resolve against the directory holding the .pth file —
            # i.e. site-packages — and are safe.
            if not os.path.isabs(line):
                continue
            try:
                target = Path(line).resolve()
            except OSError:
                continue
            if target.is_relative_to(site_packages):
                continue
            offenders.append(f"{pth.name}: {line} -> {target}")
    if offenders:
        raise ProvenanceError(
            "release site-packages contains .pth entries that point outside "
            "site-packages (suggests editable installs left over from a prior "
            "shared-venv era): " + "; ".join(offenders),
            release=release,
            diverging=site_packages,
        )


def _check_no_editable_finder(release: Path) -> None:
    """Refuse to start if a ``__editable__*`` finder is loaded into
    the release's venv.

    Such finders are how ``pip install -e <checkout>`` (or ``uv pip
    install -e <checkout>``) injects a foreign checkout into the
    release's import path. Their presence means the release venv is
    not actually immutable — a sibling-package editable install that
    the V1 release carried forward is exactly the kind of artifact
    that allowed the ``omnigent`` package to be shadowed from outside
    the release.
    """
    site_packages = _release_site_packages(release)
    if not site_packages.is_dir():
        return
    found = _find_editable_finder_modules(site_packages)
    if not found:
        return
    listed = ", ".join(f"{name}@{path}" for name, path in found)
    raise ProvenanceError(
        "release site-packages contains active editable-install finders (the "
        "venv is not actually immutable): " + listed + ". Rebuild the release "
        "with scripts/promote_release.sh; never `uv pip install -e .` "
        "against the release venv.",
        release=release,
        diverging=found[0][1],
    )


def _check_module_inside_site_packages(
    *, module_name: str, attr: str | None, resolved: Path, release: Path
) -> None:
    """Refuse if a module path resolves outside the release's
    site-packages tree.

    The earlier version of this check only verified the module path
    was somewhere under ``release``. That is too loose: running the
    probe from the repository checkout makes Python import
    ``<release>/omnigent/__init__.py`` (which lives at the release
    root and *is* under ``release``) even when no wheel is installed
    in site-packages at all. The check now demands the path lives
    under ``<release>/.venv/lib/pythonX.Y/site-packages`` and refuses
    any of these forbidden sources:

    * the repository checkout (``REPO_ROOT`` env var or the canonical
      ``/home/hermes/workspace/repos/omnigent-eval`` default);
    * the bare release source root (``<release>/omnigent``) — the
      canonical pre-edit install that the systemd unit used to fall
      back to;
    * another worktree (any path with the worktree marker);
    * another virtual environment (any path containing ``/.venv/`` or
      ``/site-packages/`` whose ``site-packages`` does not belong to
      this release).
    """
    site_packages = _release_site_packages(release)
    if not site_packages.is_dir():
        raise ProvenanceError(
            f"release {release} has no site-packages directory under "
            f".venv/lib/python{sysconfig.get_python_version()}/site-packages; "
            f"the release was not built via `uv pip install`.",
            release=release,
            diverging=resolved,
        )
    if not resolved.is_relative_to(site_packages):
        raise ProvenanceError(
            f"{module_name}{('.' + attr) if attr else ''} resolves to "
            f"{resolved} which is not under the release's site-packages "
            f"{site_packages}. The interpreter is loading code from "
            f"outside the installed wheel (editable install, repo "
            f"checkout shadowing, or PYTHONPATH leak). Rebuild the "
            f"release with scripts/promote_release.sh so the wheel is "
            f"installed non-editably inside the release venv.",
            release=release,
            diverging=resolved,
        )

    # Cross-check: the resolved path must not coincide with any
    # canonical *forbidden* source even if it accidentally lands
    # inside site-packages (e.g. a stale .egg-info symlink to the
    # repo checkout). The forbidden sources are the repository
    # checkout, the release source root, and any other worktree
    # marker.
    forbidden_prefixes: list[tuple[str, Path]] = []
    repo_root_env = os.environ.get("REPO_ROOT", "").strip()
    if repo_root_env:
        forbidden_prefixes.append(("REPO_ROOT", Path(repo_root_env).resolve()))
    # The release source root (not site-packages): running from the
    # release directory makes Python prefer ``<release>/omnigent``
    # over the installed wheel. The check below would already reject
    # it (it is not under site-packages), but we surface a specific
    # reason so the operator can identify the leak immediately.
    forbidden_prefixes.append(("release_source_root", release))

    for label, prefix in forbidden_prefixes:
        if resolved.is_relative_to(prefix) and not resolved.is_relative_to(site_packages):
            # Already caught above; keep for symmetry with old behaviour.
            continue

    # The release *source root* is intentionally also a forbidden
    # location: even though it satisfies ``is_relative_to(release)``,
    # a healthy immutable release only imports from site-packages.
    if resolved.is_relative_to(release / "omnigent") and not resolved.is_relative_to(site_packages):
        raise ProvenanceError(
            f"{module_name}{('.' + attr) if attr else ''} resolves to "
            f"{resolved}, which lives at the release source root "
            f"<{release}/omnigent> instead of inside site-packages. "
            f"The interpreter is loading the in-tree source rather than "
            f"the installed wheel. Run the provenance check from a "
            f"neutral directory (e.g. /tmp) and never insert the release "
            f"or repository root into PYTHONPATH.",
            release=release,
            diverging=resolved,
        )


def check_runtime_provenance(release_root: Path) -> dict[str, str]:
    """Assert that ``sys.prefix``, the venv marker, ``omnigent``, and
    ``omnigent.server.app`` all resolve inside ``release_root`` AND
    inside ``release_root/.venv/lib/pythonX.Y/site-packages``.

    :param release_root: Absolute path of the configured release directory.
    :returns: A dict of the resolved paths that were checked, useful for
        the deployment status command and the manifest.
    :raises ProvenanceError: When any of the four proves to live
        outside the release or outside the release's site-packages.
        The error message names the path that diverged so the
        operator knows which link in the chain broke (the ``.venv``
        symlink, the editable install, the PYTHONPATH leak, …).
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

    # Reject active editable finders before checking individual module
    # paths so a sibling editable install (e.g. an ``omnigent_client``
    # editable) can't slip past module-level checks.
    _check_no_editable_finder(release)

    # Reject .pth entries that inject foreign paths into the release
    # venv. ``site.addpackage`` walks these at interpreter start; a
    # foreign entry is a back door the provenance must close.
    _check_pth_no_foreign_paths(prefix, release)

    omnigent_path = _resolve_module("omnigent")
    _check_module_inside_site_packages(
        module_name="omnigent", attr=None, resolved=omnigent_path, release=release,
    )

    app_path = _resolve_module("omnigent.server", attr="app")
    _check_module_inside_site_packages(
        module_name="omnigent.server", attr="app", resolved=app_path, release=release,
    )

    site_packages = _release_site_packages(release)
    return {
        "release": str(release),
        "sys_executable": str(executable),
        "sys_prefix": str(prefix),
        "site_packages": str(site_packages),
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
    site_packages = _release_site_packages(release)
    return {
        "release": str(release),
        "sys_executable": str(executable),
        "sys_prefix": str(prefix),
        "site_packages": str(site_packages),
    }


def main() -> int:
    """CLI entry point used by ``scripts/promote_release.sh`` and the
    build pipeline.

    Resolves the release directory from ``OMNIGENT_RELEASE_DIR`` (the
    single environment variable that ties systemd drop-ins to a
    release) when no positional argument is given; a positional
    argument overrides the env var so build pipelines can probe an
    arbitrary release tree. Runs the provenance check, prints the
    resolved paths on stdout, and exits 0 on success or 1 on failure.

    The CLI never uses ``-P`` itself — that flag is the caller's
    responsibility (``promote_release.sh`` and ``deploy_status.sh``
    wrap the invocation). The CLI does, however, refuse to run when
    ``PYTHONPATH`` is inherited, because an inherited PYTHONPATH that
    points at the repo checkout would shadow the installed wheel and
    defeat the very check we are running.
    """
    args = sys.argv[1:]
    if len(args) > 1:
        print(f"usage: {sys.argv[0]} [<release-dir>]", file=sys.stderr)
        return 2
    # Check PYTHONPATH BEFORE looking up the release dir so the
    # unsafe-environment refusal wins over usage-style failures (the
    # caller is running in an unsafe environment regardless of which
    # directory they want to probe).
    release_dir = (args[0] if args else os.environ.get("OMNIGENT_RELEASE_DIR", "")).strip()
    if not release_dir:
        print("release directory not provided (set OMNIGENT_RELEASE_DIR or pass it as the first argument)", file=sys.stderr)
        return 2
    if "PYTHONPATH" in os.environ:
        print(
            "[deploy-supervisor] ERROR: PYTHONPATH is set in the environment "
            "of the provenance probe (value: {!r}); unset it before invoking "
            "this check so the release venv's site-packages is what gets "
            "inspected.".format(os.environ["PYTHONPATH"]),
            file=sys.stderr,
        )
        return 1
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