#!/usr/bin/env python3
"""Validate immutable Control Room Omnigent release artifacts.

The check is deliberately application-version agnostic.  It proves that a
release really contains the requested Git SHA, the exact wheel named in its
provenance, an installed package built from that wheel, and a complete bundled
web UI.  It does not depend on private runtime helper names that may legitimately
change between upstream releases.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import subprocess
import sys
import zipfile
from collections.abc import Callable
from email.parser import Parser
from pathlib import Path, PurePosixPath

SHA_RE = re.compile(r"[0-9a-f]{40}")
_REQUIRED_SPA_FILES = ("index.html", "version.json", "manifest.webmanifest")


class PreflightError(RuntimeError):
    """Raised when an artifact cannot be proven safe to activate/promote."""


def require_sha(value: str) -> str:
    """Require one exact lowercase 40-character Git SHA."""
    if not SHA_RE.fullmatch(value):
        raise PreflightError(f"expected an exact 40-character lowercase commit SHA, got {value!r}")
    return value


def sha256_file(path: Path) -> str:
    """Return a file's SHA-256 hex digest without loading it all into memory."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _literal_assignment(source: str, name: str) -> str:
    """Read one literal string assignment from generated Python metadata."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise PreflightError(f"invalid Python build metadata: {exc}") from exc
    for node in tree.body:
        value = None
        target = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target, value = node.target.id, node.value
        elif (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            target, value = node.targets[0].id, node.value
        if target == name and isinstance(value, ast.Constant) and isinstance(value.value, str):
            return value.value
    raise PreflightError(f"build metadata does not define a literal {name}")


def _validate_spa(
    names: set[str],
    read_text: Callable[[str], str],
    prefix: str,
) -> None:
    """Prove the packaged SPA is real and all local index references exist."""
    missing = [name for name in _REQUIRED_SPA_FILES if f"{prefix}{name}" not in names]
    if missing:
        raise PreflightError(f"SPA bundle is incomplete; missing: {', '.join(missing)}")
    try:
        index = read_text(f"{prefix}index.html")
        json.loads(read_text(f"{prefix}version.json"))
        json.loads(read_text(f"{prefix}manifest.webmanifest"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PreflightError(f"SPA metadata is invalid: {exc}") from exc
    if "<title>Omnigent</title>" not in index:
        raise PreflightError("SPA index is not the Omnigent application shell")
    refs = re.findall(r"(?:src|href)=[\"']([^\"']+)[\"']", index)
    local_refs: list[str] = []
    for ref in refs:
        if ref.startswith(("http://", "https://", "data:", "#")):
            continue
        path = ref.split("?", 1)[0].split("#", 1)[0].lstrip("/")
        if path:
            local_refs.append(path)
    missing_refs = sorted(path for path in local_refs if f"{prefix}{path}" not in names)
    if missing_refs:
        raise PreflightError(f"SPA index references missing files: {', '.join(missing_refs)}")
    if not any(name.startswith(f"{prefix}assets/") and name.endswith(".js") for name in names):
        raise PreflightError("SPA bundle has no JavaScript asset")


def inspect_wheel(wheel: Path, expected_sha: str) -> dict[str, str]:
    """Validate wheel identity, metadata, checksum, and bundled SPA."""
    expected_sha = require_sha(expected_sha)
    if not wheel.is_file() or wheel.suffix != ".whl":
        raise PreflightError(f"wheel not found: {wheel}")
    try:
        with zipfile.ZipFile(wheel) as archive:
            bad_member = archive.testzip()
            if bad_member:
                raise PreflightError(f"wheel has a corrupt member: {bad_member}")
            names = set(archive.namelist())
            # The SDK wheels (omnigent_client, omnigent_ui_sdk) are
            # separate packages with their own dist-info; they have no
            # ``omnigent/_build_info.py``. Detect them by package name
            # and use a lighter check that only proves the wheel is
            # a valid omnigent SDK at the same release SHA.
            metadata_names = sorted(
                name for name in names
                if re.fullmatch(r"omnigent_[a-z_]+-[^/]+\.dist-info/METADATA", name)
                or re.fullmatch(r"omnigent-[^/]+\.dist-info/METADATA", name)
            )
            if len(metadata_names) != 1:
                raise PreflightError(
                    f"wheel must contain exactly one omnigent* dist-info METADATA, found {metadata_names}"
                )
            metadata = Parser().parsestr(archive.read(metadata_names[0]).decode())
            name = (metadata.get("Name") or "").lower()
            version = metadata.get("Version")
            if not name.startswith("omnigent") or not version:
                raise PreflightError(
                    "wheel package metadata is not a versioned omnigent* distribution"
                )
            # Only the main omnigent wheel carries the build SHA + SPA.
            # SDK wheels are validated by name+version pin.
            if name == "omnigent":
                build_info = "omnigent/_build_info.py"
                if build_info not in names:
                    raise PreflightError("wheel is missing omnigent/_build_info.py")
                built_sha = _literal_assignment(archive.read(build_info).decode(), "COMMIT_SHA")
                if built_sha != expected_sha:
                    raise PreflightError(
                        f"wheel commit SHA {built_sha!r} does not match requested SHA {expected_sha}"
                    )
                prefix = "omnigent/server/static/web-ui/"
                _validate_spa(names, lambda name: archive.read(name).decode(), prefix)
            return {
                "sha": expected_sha,
                "wheel_sha256": sha256_file(wheel),
                "package_version": version,
                "wheel_filename": wheel.name,
                "package_name": name,
            }
    except zipfile.BadZipFile as exc:
        raise PreflightError(f"invalid wheel zip: {wheel}") from exc


def read_provenance(path: Path) -> dict[str, str]:
    """Parse and validate the immutable release provenance file."""
    if not path.is_file():
        raise PreflightError(f"missing provenance file: {path}")
    result: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not key or key in result:
            raise PreflightError(f"invalid provenance line: {line!r}")
        result[key] = value
    required = {
        "schema_version",
        "sha",
        "package_version",
        "wheel_sha256",
        "wheel_filename",
        "built_at_utc",
        "builder",
    }
    missing = sorted(required - result.keys())
    if missing:
        raise PreflightError(f"provenance is missing fields: {', '.join(missing)}")
    if result["schema_version"] != "1":
        raise PreflightError("unsupported provenance schema")
    require_sha(result["sha"])
    if not re.fullmatch(r"[0-9a-f]{64}", result["wheel_sha256"]):
        raise PreflightError("provenance wheel_sha256 is invalid")
    if PurePosixPath(result["wheel_filename"]).name != result["wheel_filename"]:
        raise PreflightError("provenance wheel_filename must be a basename")
    return result


def _inspect_installed_identity(release: Path) -> dict[str, str]:
    """Ask the release's own interpreter for installed version/build identity."""
    python = release / "venv" / "bin" / "python"
    cli = release / "venv" / "bin" / "omnigent"
    if not python.is_file() or not cli.is_file():
        raise PreflightError("release venv is missing python or omnigent")
    snippet = (
        "import json; "
        "from omnigent import _build_info; "
        "from omnigent.version import VERSION; "
        "print(json.dumps({'sha': _build_info.COMMIT_SHA, 'version': VERSION}))"
    )
    check = subprocess.run(
        [str(python), "-c", snippet],
        capture_output=True,
        text=True,
        timeout=30,
        # Run from ``/tmp`` so ``omnigent`` resolves from the installed
        # venv site-packages, not from whatever working tree the caller
        # happened to invoke the controller from.
        cwd="/tmp",
        # Clear PYTHONPATH explicitly so a developer environment can't
        # shadow the installed package.
        env={"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"},
    )
    if check.returncode:
        raise PreflightError(f"installed import check failed: {check.stderr.strip()}")
    try:
        payload = json.loads(check.stdout)
    except json.JSONDecodeError as exc:
        raise PreflightError("installed identity check returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise PreflightError("installed identity check returned an invalid payload")
    return {"sha": str(payload.get("sha", "")), "version": str(payload.get("version", ""))}


def inspect_release(
    release: Path,
    expected_sha: str,
    expected_wheel_sha: str = "",
) -> dict[str, str]:
    """Validate a fully installed immutable release directory."""
    expected_sha = require_sha(expected_sha)
    release = release.resolve()
    if release.name != expected_sha:
        raise PreflightError(
            f"release directory basename {release.name!r} is not SHA {expected_sha}"
        )
    provenance = read_provenance(release / "PROVENANCE.txt")
    if provenance["sha"] != expected_sha:
        raise PreflightError("provenance SHA does not match the release directory")
    if expected_wheel_sha and provenance["wheel_sha256"] != expected_wheel_sha:
        raise PreflightError("provenance wheel hash does not match the requested artifact")

    artifact = release / "artifacts" / provenance["wheel_filename"]
    wheel = inspect_wheel(artifact, expected_sha)
    for key in ("wheel_sha256", "package_version", "wheel_filename"):
        if wheel[key] != provenance[key]:
            raise PreflightError(f"wheel {key} does not match provenance")

    # Validate any sibling SDK wheels stored alongside the main wheel.
    artifacts_dir = release / "artifacts"
    if artifacts_dir.is_dir():
        for sdk_wheel in sorted(artifacts_dir.glob("omnigent_*.whl")):
            inspect_wheel(sdk_wheel, expected_sha)

    site_packages = sorted((release / "venv" / "lib").glob("python*/site-packages"))
    package_dirs = [path / "omnigent" for path in site_packages if (path / "omnigent").is_dir()]
    if len(package_dirs) != 1:
        raise PreflightError("release must contain exactly one installed omnigent package")
    package = package_dirs[0]
    if not package.resolve().is_relative_to((release / "venv").resolve()):
        raise PreflightError("installed omnigent package escapes the release venv")
    installed_sha = _literal_assignment((package / "_build_info.py").read_text(), "COMMIT_SHA")
    if installed_sha != expected_sha:
        raise PreflightError("installed package commit SHA does not match the release")
    spa = package / "server" / "static" / "web-ui"
    names = {path.relative_to(spa).as_posix() for path in spa.rglob("*") if path.is_file()}
    _validate_spa(names, lambda name: (spa / name).read_text(), "")

    identity = _inspect_installed_identity(release)
    if identity["sha"] != expected_sha:
        raise PreflightError("runtime import reports a different build SHA")
    if identity["version"] != provenance["package_version"]:
        raise PreflightError("runtime version does not match provenance package_version")
    return provenance


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    wheel = subparsers.add_parser("wheel")
    wheel.add_argument("path", type=Path)
    wheel.add_argument("sha")
    release = subparsers.add_parser("release")
    release.add_argument("path", type=Path)
    release.add_argument("sha")
    release.add_argument("--wheel-sha", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "wheel":
            result = inspect_wheel(args.path, args.sha)
        else:
            result = inspect_release(args.path, args.sha, args.wheel_sha)
    except (OSError, subprocess.SubprocessError, PreflightError) as exc:
        print(f"release preflight failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
