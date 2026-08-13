"""Create immutable acceptance from observed candidate behavior."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from urllib.request import urlopen

from . import acceptance, identity


class _Assets(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.paths: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "script" and values.get("src"):
            self.paths.add(str(values["src"]))
        elif (
            tag == "link"
            and values.get("href")
            and (values.get("rel") == "stylesheet" or str(values["href"]).endswith(".css"))
        ):
            self.paths.add(str(values["href"]))


def _json_url(url: str) -> dict[str, object]:
    with urlopen(url, timeout=5) as response:
        value = json.load(response)
    if not isinstance(value, dict):
        raise acceptance.AcceptanceError(f"endpoint did not return an object: {url}")
    return value


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def create_from_candidate(
    *,
    release_root: Path,
    source_sha: str,
    wheels: dict[str, Path],
    frontend_root: Path,
    target_db_schema: str,
    builder_identity: str,
    operator_identity: str,
    acceptance_root: Path = acceptance.DEFAULT_ACCEPTANCE_ROOT,
) -> Path:
    """Probe a candidate and exclusive-create its canonical acceptance record."""
    if os.geteuid() != 0:
        raise acceptance.AcceptanceError("acceptance creation must run as root")
    release_root = release_root.resolve()
    if release_root.name != source_sha:
        raise acceptance.AcceptanceError("candidate release basename must equal source SHA")
    venv = release_root / "venv"
    python = venv / "bin" / "python"
    runtime = identity.runtime_identity(python)
    if runtime.get("commit_sha") != source_sha:
        raise acceptance.AcceptanceError("candidate embedded SHA differs from source SHA")
    package_version = str(runtime.get("version", ""))
    installed = acceptance._installed_package_probe(python)
    installed_packages = tuple(
        acceptance.InstalledPackage(name, str(value["version"]), str(value["path"]))
        for name, value in installed.items()
    )
    uv = shutil.which("uv")
    if uv is None:
        raise acceptance.AcceptanceError("uv executable missing")
    checked = subprocess.run(
        [uv, "pip", "check", "--python", str(python)],
        cwd="/tmp",
        env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": "", "PYTHONNOUSERSITE": "1"},
        capture_output=True,
        text=True,
        check=False,
    )
    if checked.returncode:
        raise acceptance.AcceptanceError(f"uv pip check failed: {checked.stderr.strip()}")
    port = _free_port()
    with tempfile.TemporaryDirectory(prefix="omnigent-accept-") as temporary:
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": temporary,
            "PYTHONPATH": "",
            "PYTHONNOUSERSITE": "1",
            "OMNIGENT_CONFIG_HOME": temporary,
            "OMNIGENT_DATA_DIR": temporary,
            "OMNIGENT_AUTH_PROVIDER": "header",
            "OMNIGENT_WEB_UI_DIST": str(release_root / frontend_root),
        }
        process = subprocess.Popen(
            [
                str(python),
                "-m",
                "omnigent.cli",
                "server",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--no-open",
            ],
            cwd="/tmp",
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        base = f"http://127.0.0.1:{port}/"
        try:
            deadline = time.monotonic() + 90
            health: dict[str, object] | None = None
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    stderr = process.stderr.read()[-1500:] if process.stderr else ""
                    raise acceptance.AcceptanceError(f"candidate boot failed: {stderr}")
                try:
                    health = _json_url(urljoin(base, "health"))
                    break
                except Exception:  # noqa: BLE001 - retry temporary boot
                    time.sleep(0.25)
            if health is None or health.get("status") != "ok":
                raise acceptance.AcceptanceError("candidate health timeout")
            info = _json_url(urljoin(base, "v1/info"))
            with urlopen(base, timeout=5) as response:
                html = response.read().decode()
            parser = _Assets()
            parser.feed(html)
            if not parser.paths:
                raise acceptance.AcceptanceError("candidate HTML references no JS/CSS assets")
            for path in parser.paths:
                asset = urljoin(base, path)
                parsed = urlsplit(asset)
                if parsed.hostname != "127.0.0.1" or parsed.port != port:
                    raise acceptance.AcceptanceError(
                        f"candidate references external asset: {path}"
                    )
                with urlopen(asset, timeout=5) as response:
                    response.read(1)
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
    record = acceptance.CandidateAcceptance.create(
        source_sha=source_sha,
        package_version=package_version,
        wheels=tuple(
            acceptance.AcceptedWheel(role, path.name, acceptance.sha256_file(path))
            for role, path in wheels.items()
        ),
        frontend_root=frontend_root.as_posix(),
        frontend_tree_sha256=acceptance.tree_sha256(release_root / frontend_root),
        immutable_release_root=str(release_root),
        runtime_venv_path=str(venv),
        installed_packages=installed_packages,
        uv_pip_check_success=True,
        embedded_build_sha=str(runtime["commit_sha"]),
        boot_command_classification=acceptance.TEMPORARY_PORT_BOOT_CLASSIFICATION,
        temporary_port=port,
        health_ok=True,
        health_status=str(health["status"]),
        info_ok=True,
        info_server_version=str(info.get("server_version", "")),
        info_build_sha=str(info.get("build_sha", "")),
        html_assets_ok=True,
        html_asset_count=len(parser.paths),
        disk_headroom_bytes=shutil.disk_usage(release_root).free,
        accepted_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        builder_identity=builder_identity,
        operator_identity=operator_identity,
        target_db_schema=target_db_schema,
    )
    return acceptance.write_immutable(record, root=acceptance_root)


__all__ = ["create_from_candidate"]
