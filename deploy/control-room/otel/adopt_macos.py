#!/usr/bin/env python3
"""Stage a reversible update of the existing Mac Collector; never restart it."""

import argparse
import datetime as dt
import hashlib
import json
import os
import plistlib
import subprocess
import sys
from pathlib import Path

from managed_adoption import reserved_copies
from managed_budget import load_policy
from managed_diagnostics import collector_rotation_environment

FILES = {
    "managed_budget.py": "bin/managed_budget.py",
    "managed_storage_policy.json": "bin/managed_storage_policy.json",
    "managed_storage.py": "bin/managed_storage.py",
    "managed_diagnostics.py": "bin/managed_diagnostics.py",
    "control_room_otel.py": "bin/control_room_otel.py",
    "telemetry_audit.py": "bin/telemetry_audit.py",
    "config/otelcol-macos.yaml": "config/otelcol-macos.yaml",
}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_status(source):
    root = Path(
        subprocess.check_output(
            ["git", "-C", str(source), "rev-parse", "--show-toplevel"], text=True
        ).strip()
    ).resolve()
    if source.resolve() != root / "deploy/control-room/otel":
        raise ValueError("source is not the canonical Omnigent tooling directory")
    origin = subprocess.check_output(
        ["git", "-C", str(root), "remote", "get-url", "origin"], text=True
    ).strip()
    if origin not in {
        "git@github.com:Mortified2896/omnigent.git",
        "https://github.com/Mortified2896/omnigent.git",
    }:
        raise ValueError("unexpected source repository")
    return {
        "repository": "Mortified2896/omnigent",
        "commit": subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
        ).strip(),
        "files": {name: digest(source / name) for name in FILES},
        "dirty": bool(
            subprocess.check_output(
                [
                    "git",
                    "-C",
                    str(root),
                    "status",
                    "--porcelain",
                    "--",
                    "deploy/control-room/otel",
                ],
                text=True,
            ).strip()
        ),
    }


def adopt(source, home, plist):
    provenance = source_status(source)
    if provenance.get("dirty"):
        raise ValueError("commit the reviewed source before adoption")
    if home.is_symlink() or not (home / "state/capture_node_id").is_file():
        raise ValueError("existing capture identity required; new installations are unsupported")
    definition = plistlib.loads(plist.read_bytes())
    binary = home / "bin/otelcol-contrib"
    config = home / "config/otelcol-macos.yaml"
    child_args = [str(binary), "--config", str(config)]
    supervised_args = [
        "/usr/bin/python3",
        str(home / "bin/managed_diagnostics.py"),
        "--home",
        str(home),
        "--name",
        "collector",
        "--",
        *child_args,
    ]
    if definition.get("ProgramArguments") not in (child_args, supervised_args):
        raise ValueError("LaunchAgent does not target the supplied installation")
    environment = {
        **os.environ,
        **definition.get("EnvironmentVariables", {}),
        **collector_rotation_environment(load_policy()),
    }
    subprocess.run(
        [str(binary), "validate", "--config", str(source / "config/otelcol-macos.yaml")],
        env=environment,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    paths = [home / relative for relative in FILES.values()]
    paths.append(home / "state/source-manifest.json")
    with reserved_copies(
        [source / name for name in FILES], paths, home / "state", [home]
    ) as copies:
        copies.assert_hashes({source / name: value for name, value in provenance["files"].items()})
        for relative in FILES.values():
            target = home / relative
            if target.is_symlink() or not target.parent.is_dir():
                raise ValueError("unsafe installation target")
        for target in paths:
            pending = target.with_name(target.name + ".pending")
            if pending.exists() or pending.is_symlink():
                raise ValueError("pending update already exists")
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = home / "state" / ("rollback-" + stamp)
        backup.mkdir(mode=0o700)
        plan = []
        changed = {
            name: relative
            for name, relative in FILES.items()
            if copies.different(source / name, home / relative)
        }
        for relative in [*changed.values(), "state/source-manifest.json"]:
            target = home / relative
            old = backup / relative
            if copies.present(target):
                old.parent.mkdir(parents=True, exist_ok=True)
                copies.copy2(target, old)
                plan.append({"target": str(target), "backup": str(old)})
        copies.text(backup / "plan.json", json.dumps(plan))
        rollback = backup / "rollback.py"
        copies.text(
            rollback,
            """#!/usr/bin/env python3
import json, pathlib, shutil
for item in json.loads(pathlib.Path(__file__).with_name("plan.json").read_text()):
    shutil.copy2(item["backup"], item["target"])
print("Files restored. Restart only the Collector in an approved idle window.")
""",
        )
        rollback.chmod(0o700)
        provenance["capture_identity_sha256"] = digest(home / "state/capture_node_id")
        provenance["collector_sha256"] = digest(binary)
        provenance["runtime_verified"] = False
        # Leave identical files untouched in both adoption and rollback.
        copies.assert_unchanged()
        for name, relative in changed.items():
            target = home / relative
            temporary = target.with_name(target.name + ".pending")
            if temporary.exists() or temporary.is_symlink():
                raise ValueError("pending update already exists")
            copies.copy2(source / name, temporary)
            temporary.replace(target)
        copies.text(
            home / "state/source-manifest.json",
            json.dumps(provenance, indent=2) + "\n",
            replace=True,
        )
        return {"staged": True, "restarted": False, "rollback": str(rollback), **provenance}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--plist", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    source = Path(__file__).resolve().parent
    if args.apply:
        result = adopt(source, args.home, args.plist)
    else:
        result = source_status(source)
        result["installed_matches"] = {
            name: (args.home / relative).is_file()
            and digest(args.home / relative) == result["files"][name]
            for name, relative in FILES.items()
        }
        result["runtime_verified"] = False
    print(json.dumps(result, indent=2))
    return 0


def adopt_diagnostic_job(source, home, plist, expected_sha256, name):
    """Stage the existing job's pipe supervisor; activation stays explicit."""
    provenance = source_status(source)
    if provenance.get("dirty") or name not in {"collector", "retention"}:
        raise ValueError("committed reviewed diagnostic source required")
    if digest(plist) != expected_sha256 or plist.resolve() != plist:
        raise ValueError("job definition changed; review before replacing")
    definition = plistlib.loads(plist.read_bytes())
    child = (
        [str(home / "bin/otelcol-contrib"), "--config", str(home / "config/otelcol-macos.yaml")]
        if name == "collector"
        else ["/usr/bin/python3", str(home / "bin/control_room_otel.py"), "retain"]
    )
    if definition.get("ProgramArguments") != child:
        raise ValueError("unexpected existing job command")
    for name_in_source in (
        "managed_diagnostics.py",
        "managed_storage.py",
        "managed_budget.py",
        "managed_storage_policy.json",
    ):
        if digest(home / "bin" / name_in_source) != digest(source / name_in_source):
            raise ValueError("adopt and verify diagnostic dependencies first")
    definition["ProgramArguments"] = [
        "/usr/bin/python3",
        str(home / "bin/managed_diagnostics.py"),
        "--home",
        str(home),
        "--name",
        name,
        "--",
        *child,
    ]
    definition["StandardOutPath"] = "/dev/null"
    definition["StandardErrorPath"] = "/dev/null"
    with reserved_copies([], [plist], home / "state", [home, plist.parent]) as copies:
        copies.assert_hashes({plist: expected_sha256})
        backup = (
            home
            / "state"
            / (
                "diagnostic-job-"
                + name
                + "-"
                + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            )
        )
        backup.mkdir(mode=0o700)
        old = backup / plist.name
        copies.copy2(plist, old)
        pending = plist.with_name(plist.name + ".pending")
        copies.write(pending, plistlib.dumps(definition), 0o600)
        copies.assert_unchanged()
        pending.replace(plist)
        result = {
            "commit": provenance["commit"],
            "name": name,
            "before_sha256": expected_sha256,
            "installed_sha256": digest(plist),
            "backup": str(old),
            "activated": False,
        }
        copies.text(backup / "adoption.json", json.dumps(result, indent=2) + "\n")
        return result


PROVENANCE_FILES = (
    "managed_budget.py",
    "managed_storage_policy.json",
    "managed_storage.py",
    "managed_sqlite.py",
    "managed_metadata.py",
    "codex_otel_decisions.py",
)


def adopt_provenance(source, home, otel_home, expected_installed_sha256, expected_files=None):
    """Adopt only the short-lived sidecar; do not reload any native service."""
    provenance = source_status(source)
    if provenance["dirty"]:
        raise ValueError("commit the reviewed source before adoption")
    script = home / "bin/codex_otel_decisions.py"
    if script.resolve() != script or digest(script) != expected_installed_sha256:
        raise ValueError("installed source changed; preserve and review it first")
    if not (home / "provenance.sqlite3").is_file():
        raise ValueError("existing provenance database required")
    state = otel_home / "state"
    with reserved_copies(
        [source / name for name in PROVENANCE_FILES],
        [home / "bin" / name for name in PROVENANCE_FILES],
        state,
        [home, otel_home],
    ) as copies:
        copies.assert_hashes({script: expected_installed_sha256})
        if expected_files is not None:
            if set(expected_files) != set(PROVENANCE_FILES):
                raise ValueError("review every installed provenance module before update")
            copies.assert_hashes(
                {home / "bin" / name: value for name, value in expected_files.items()}
            )
        for name in PROVENANCE_FILES:
            target = home / "bin" / name
            if (
                target.resolve() != target
                or target.with_suffix(target.suffix + ".pending").exists()
            ):
                raise ValueError("unsafe or interrupted adoption target")
            if name != "codex_otel_decisions.py" and target.exists() and expected_files is None:
                raise ValueError("module already installed; review before replacing it")
        backup = state / (
            "provenance-rollback-" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        )
        backup.mkdir(mode=0o700)
        plan = []
        for name in PROVENANCE_FILES:
            target = home / "bin" / name
            old = backup / name
            if copies.present(target):
                copies.copy2(target, old)
            plan.append(
                {
                    "target": str(target),
                    "backup": str(old) if old.exists() else None,
                    "adopted_sha256": copies.digest(source / name),
                }
            )
        copies.text(backup / "plan.json", json.dumps(plan, indent=2) + "\n")
        rollback = backup / "rollback.py"
        copies.text(
            rollback,
            """#!/usr/bin/env python3
import hashlib, json, pathlib, shutil
plan = json.loads(pathlib.Path(__file__).with_name("plan.json").read_text())
for item in plan:
    target = pathlib.Path(item["target"])
    changed = hashlib.sha256(target.read_bytes()).hexdigest() != item["adopted_sha256"]
    if target.is_symlink() or changed:
        raise SystemExit("Installed source changed; stop and review before rollback")
for item in reversed(plan):
    target = pathlib.Path(item["target"])
    if item["backup"]:
        pending = target.with_name(target.name + ".rollback-pending")
        if pending.exists():
            raise SystemExit("Interrupted rollback requires review")
        shutil.copy2(item["backup"], pending)
        pending.replace(target)
    else:
        target.unlink()
print("Sidecar source restored. No database, capture, service or app was replaced.")
""",
        )
        rollback.chmod(0o700)
        # Dependencies first; atomically replace the hook entrypoint last.
        copies.assert_unchanged()
        for name in PROVENANCE_FILES:
            target = home / "bin" / name
            pending = target.with_suffix(target.suffix + ".pending")
            copies.copy2(source / name, pending)
            pending.replace(target)
        result = {
            "repository": provenance["repository"],
            "commit": provenance["commit"],
            "files": {name: digest(home / "bin" / name) for name in PROVENANCE_FILES},
            "rollback": str(rollback),
            "services_restarted": False,
            "total_enforcement_verified": False,
        }
        copies.text(backup / "adoption.json", json.dumps(result, indent=2) + "\n")
        return result


if __name__ == "__main__":
    sys.exit(main())
