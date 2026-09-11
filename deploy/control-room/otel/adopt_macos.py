#!/usr/bin/env python3
"""Stage a reversible update of the existing Mac Collector; never restart it."""

import argparse
import datetime as dt
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

FILES = {
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
    if home.is_symlink() or not (home / "state/capture_node_id").is_file():
        raise ValueError("existing capture identity required; new installations are unsupported")
    definition = plistlib.loads(plist.read_bytes())
    binary = home / "bin/otelcol-contrib"
    config = home / "config/otelcol-macos.yaml"
    if definition.get("ProgramArguments") != [str(binary), "--config", str(config)]:
        raise ValueError("LaunchAgent does not target the supplied installation")
    environment = {**os.environ, **definition.get("EnvironmentVariables", {})}
    subprocess.run(
        [str(binary), "validate", "--config", str(source / "config/otelcol-macos.yaml")],
        env=environment,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for relative in FILES.values():
        target = home / relative
        if target.is_symlink() or not target.parent.is_dir():
            raise ValueError("unsafe installation target")
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S%fZ")
    backup = home / "state" / ("rollback-" + stamp)
    backup.mkdir(mode=0o700)
    plan = []
    changed = {
        name: relative
        for name, relative in FILES.items()
        if not (home / relative).is_file() or digest(home / relative) != digest(source / name)
    }
    for relative in [*changed.values(), "state/source-manifest.json"]:
        target = home / relative
        old = backup / relative
        if target.exists():
            old.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, old)
            plan.append({"target": str(target), "backup": str(old)})
    (backup / "plan.json").write_text(json.dumps(plan))
    rollback = backup / "rollback.py"
    rollback.write_text("""#!/usr/bin/env python3
import json, pathlib, shutil
for item in json.loads(pathlib.Path(__file__).with_name("plan.json").read_text()):
    shutil.copy2(item["backup"], item["target"])
print("Files restored. Restart only the Collector in an approved idle window.")
""")
    rollback.chmod(0o700)
    provenance["capture_identity_sha256"] = digest(home / "state/capture_node_id")
    provenance["collector_sha256"] = digest(binary)
    provenance["runtime_verified"] = False
    # Leave identical files untouched in both adoption and rollback.
    for name, relative in changed.items():
        target = home / relative
        temporary = target.with_name(target.name + ".pending")
        if temporary.exists() or temporary.is_symlink():
            raise ValueError("pending update already exists")
        shutil.copy2(source / name, temporary)
        temporary.replace(target)
    (home / "state/source-manifest.json").write_text(json.dumps(provenance, indent=2) + "\n")
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


if __name__ == "__main__":
    sys.exit(main())
