#!/usr/bin/env python3
"""Artifact and installed-release integrity checks for local deployments."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import subprocess
import sys
import zipfile
from email.parser import Parser
from pathlib import Path, PurePosixPath

SHA_RE = re.compile(r"[0-9a-f]{40}")
_REQUIRED_SPA_FILES = ("index.html", "version.json", "manifest.webmanifest")


class PreflightError(RuntimeError):
    """The candidate cannot be safely promoted."""


def require_sha(value: str) -> str:
    if not SHA_RE.fullmatch(value):
        raise PreflightError(f"expected an exact 40-character lowercase commit SHA, got {value!r}")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _assignment(source: str, name: str) -> str:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise PreflightError(f"invalid Python build metadata: {exc}") from exc
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target, value = node.target.id, node.value
        elif (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            target, value = node.targets[0].id, node.value
        else:
            continue
        if target == name and isinstance(value, ast.Constant) and isinstance(value.value, str):
            return value.value
    raise PreflightError(f"build metadata does not define a literal {name}")


def _validate_spa(names: set[str], read_text, prefix: str) -> None:
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
    if "OMNIGENT_SKIP_WEB_UI" in index:
        raise PreflightError("SPA index is the API-only fallback page")
    references = re.findall(r"(?:src|href)=[\"']([^\"']+)[\"']", index)
    local_refs: list[str] = []
    for reference in references:
        if reference.startswith(("http://", "https://", "data:", "#")):
            continue
        path = reference.split("?", 1)[0].split("#", 1)[0].lstrip("/")
        if path:
            local_refs.append(path)
    missing_refs = sorted(path for path in local_refs if f"{prefix}{path}" not in names)
    if missing_refs:
        raise PreflightError(f"SPA index references missing files: {', '.join(missing_refs)}")
    if not any(name.startswith(f"{prefix}assets/") and name.endswith(".js") for name in names):
        raise PreflightError("SPA bundle has no JavaScript asset")


def inspect_wheel(wheel: Path, expected_sha: str) -> dict[str, str]:
    expected_sha = require_sha(expected_sha)
    if not wheel.is_file() or wheel.suffix != ".whl":
        raise PreflightError(f"wheel not found: {wheel}")
    try:
        with zipfile.ZipFile(wheel) as archive:
            bad_member = archive.testzip()
            if bad_member:
                raise PreflightError(f"wheel has a corrupt member: {bad_member}")
            names = set(archive.namelist())
            build_info = "omnigent/_build_info.py"
            if build_info not in names:
                raise PreflightError("wheel is missing omnigent/_build_info.py")
            built_sha = _assignment(archive.read(build_info).decode(), "COMMIT_SHA")
            if built_sha != expected_sha:
                raise PreflightError(
                    f"wheel commit SHA {built_sha!r} does not match requested SHA {expected_sha}"
                )
            metadata_names = sorted(
                name for name in names if re.fullmatch(r"omnigent-[^/]+\.dist-info/METADATA", name)
            )
            if len(metadata_names) != 1:
                raise PreflightError("wheel must contain exactly one omnigent dist-info METADATA")
            metadata = Parser().parsestr(archive.read(metadata_names[0]).decode())
            if metadata.get("Name", "").lower() != "omnigent" or not metadata.get("Version"):
                raise PreflightError(
                    "wheel package metadata is not for a versioned omnigent distribution"
                )
            prefix = "omnigent/server/static/web-ui/"
            _validate_spa(names, lambda name: archive.read(name).decode(), prefix)
            return {
                "sha": expected_sha,
                "wheel_sha256": sha256_file(wheel),
                "package_version": metadata["Version"],
                "wheel_filename": wheel.name,
            }
    except zipfile.BadZipFile as exc:
        raise PreflightError(f"invalid wheel zip: {wheel}") from exc


def read_provenance(path: Path) -> dict[str, str]:
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


def inspect_release(
    release: Path, expected_sha: str, expected_wheel_sha: str = ""
) -> dict[str, str]:
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

    site_packages = sorted((release / "venv" / "lib").glob("python*/site-packages"))
    package_dirs = [path / "omnigent" for path in site_packages if (path / "omnigent").is_dir()]
    if len(package_dirs) != 1:
        raise PreflightError("release must contain exactly one installed omnigent package")
    package = package_dirs[0]
    if not package.resolve().is_relative_to((release / "venv").resolve()):
        raise PreflightError("installed omnigent package escapes the release venv")
    installed_sha = _assignment((package / "_build_info.py").read_text(), "COMMIT_SHA")
    if installed_sha != expected_sha:
        raise PreflightError("installed package commit SHA does not match the release")
    spa = package / "server" / "static" / "web-ui"
    names = {
        path.relative_to(spa).as_posix()
        for path in spa.rglob("*")
        if path.is_file()
    }
    _validate_spa(names, lambda name: (spa / name).read_text(), "")
    python = release / "venv" / "bin" / "python"
    cli = release / "venv" / "bin" / "omnigent"
    if not python.is_file() or not cli.is_file():
        raise PreflightError("release venv is missing python or omnigent")
    check = subprocess.run(
        [
            str(python),
            "-c",
            "from omnigent.runtime import telemetry; "
            "assert callable(telemetry._fastapi_instrumentation_enabled)",
        ],
        capture_output=True,
        text=True,
    )
    if check.returncode:
        raise PreflightError(f"installed import check failed: {check.stderr.strip()}")
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
    except (OSError, PreflightError) as exc:
        print(f"release preflight failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
