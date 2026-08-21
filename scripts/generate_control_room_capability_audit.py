#!/usr/bin/env python3
# ruff: noqa: E501, PIE810
"""Generate the deterministic Control Room v0.10 capability audit."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs/control-room-v0.10-capability-audit.json"
MERGE_BASE = "43762a989235599f2ec63f270f1f0b32ae7d3e7a"
FORK_MAIN = "a512c01f952662d7baa95a5810a3c249f7d2ff80"
UPSTREAM_V010 = "40755dd8dddb07e1eb6e4055d1d9936e184ceb9b"
AUDIT_BASE = "be56d7cedcf439cc47728cfb4f4ee8a917cfccba"
ALLOWED = {
    "RETAIN/REPLAY",
    "PORT SEMANTICALLY",
    "SOLVED UPSTREAM",
    "SUPERSEDED/OBSOLETE",
    "BLOCKED — OWNER DECISION REQUIRED",
}


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True)


def commit_decision(sha: str, title: str, paths: list[str]) -> dict[str, object]:
    joined = "\n".join(paths)
    disposition = "BLOCKED — OWNER DECISION REQUIRED"
    capability = title
    consumer = "Unknown; read-only HomeLab #21 inventory is required"
    upstream = "No conclusive v0.10 equivalent recorded"
    architecture: bool | str = "unknown"
    destination = "#135 owner review; PR #137 remains blocked"
    validation = "Confirm current O1/O2 source/config dependence without runtime mutation"
    risk = "A relied-upon Control Room behavior could disappear silently"
    evidence = [f"fork commit {sha}", *paths]

    if sha == "1d94ba2db9f574b5f22666d8cdb960ad8d4cd68f":
        disposition = "RETAIN/REPLAY"
        capability = "Reject simultaneous live host owner/name collisions before ID rotation"
        consumer = "v0.10 host WebSocket admission and persistent HostStore"
        upstream = (
            "v0.10 retains the host tunnel/store architecture but lacked this collision check"
        )
        architecture = True
        destination = "PR #137 v0.10-native host admission port"
        validation = "Unit cases plus tunnel-level refusal and registration preservation"
        risk = "Two live O1/O2 daemons can replace one another and misroute control traffic"
        evidence.extend(
            ["omnigent/server/host_identity.py", "tests/server/test_host_identity_collision.py"]
        )
    elif (
        "peer_deployer" in joined
        or "peer_promote" in joined
        or "release_preflight" in joined
        or title.startswith("fix(deploy)")
        or title.startswith("feat(deploy)")
    ):
        capability = (
            "Peer-supervised immutable release promotion, transaction recovery, and rollback"
        )
        consumer = (
            "Live authority is disputed between installed peer-deployer and newer source tooling"
        )
        upstream = "Upstream v0.10 has no Control Room O1/O2 promotion authority"
        architecture = "source exists; authoritative live generation unknown"
        destination = "#122 after HomeLab #21/#23 inventory; required before PR #137 merge"
        validation = "#122 authority matrix: target != supervisor, SHA/provenance, backup/migration, health cutover, bounded rollback, reconciliation"
        risk = "Unsafe self-upgrade, wrong artifact, split transaction ownership, or unrecoverable cutover"
    elif (
        "trusted-root" in joined
        or "trusted root" in title.lower()
        or "trusted mode" in title.lower()
        or sha
        in {
            "2eec3c67faa9ea2f6730ec7e501f5d18797dee35",
            "7ebab01c1698bf6597c5177da86d78caf21eb07d",
            "49a38ecb74dabca4973870d60eaf0e8aef128e16",
        }
    ):
        disposition = "SUPERSEDED/OBSOLETE"
        capability = "Legacy unrestricted agent-root policy and model-visible trusted-root claim"
        consumer = "No valid O1/O2 consumer; human emergency root and privileged promotion are separate boundaries"
        upstream = "Replaced by HomeLab privilege-boundary source and #115 truthful capability reconciliation"
        architecture = False
        destination = (
            "HomeLab #25 evidence and Omnigent #115; never restore unrestricted agent sudo"
        )
        validation = (
            "#115 false/missing/malformed/conflict tests across host, runner, Codex, and prompt"
        )
        risk = "A stale true flag could misrepresent privileges; replaying policy would weaken isolation"
    elif "telemetry" in title or "trace" in title:
        capability = "Privacy-bounded telemetry and native model/provider/instance provenance"
        consumer = "Server-side O1/O2 observability; MLflow dependence unresolved"
        architecture = True
        if "MLflow" in title:
            disposition = "BLOCKED — OWNER DECISION REQUIRED"
            upstream = "v0.10 provides opt-in OTel/OTLP; MLflow-specific export is not equivalent"
            destination = (
                "HomeLab #5 KEEP-OPTIONAL versus RETIRE decision; required before PR #137 merge"
            )
        elif sha == "541c9a3180b81bfb2fc450b3ef5f8648691b359d":
            disposition = "SOLVED UPSTREAM"
            upstream = "v0.10 runtime telemetry is opt-in, OTLP-native, and disables content capture by default"
            destination = "v0.10 omnigent/runtime/telemetry.py"
        else:
            disposition = "PORT SEMANTICALLY"
            upstream = "v0.10 has general OTel spans but no proven equivalent for every fork native-turn provenance field"
            destination = "Focused telemetry/provenance stack under #135 after HomeLab #5 decision"
        validation = "Assert no prompt/tool/repository/token content and verify requested/effective model, provider, harness, instance, and terminal status"
        risk = "Operators lose safe attribution or accidentally export sensitive content"
    elif (
        "pi" in title.lower()
        or "codex" in title.lower()
        or "model picker" in title.lower()
        or "reasoning effort" in title.lower()
        or sha
        in {"3867c7b7d994850dfbe11aeccb9b2c536583ab76", "ccca33444c783a7eaa654292ff9538712d58e745"}
    ):
        capability = "Current Codex/Pi model, credential, routing, access-lane, and reasoning-effort behavior"
        consumer = "Potential O1/O2 session launch configuration; live dependence unverified"
        upstream = "v0.10 materially expands native Codex/Pi config, model overrides, eligibility, and effort handling"
        architecture = True
        if any(
            token in title
            for token in (
                "labels readable",
                "widen model selector",
                "visual baselines",
                "shared picker baselines",
            )
        ):
            disposition = "SUPERSEDED/OBSOLETE"
            destination = "v0.10 current UI architecture; new #124 product work remains parked"
            validation = "No port; review current responsive session controls"
            risk = "Low: obsolete UI presentation may otherwise conflict with v0.10"
        else:
            disposition = "BLOCKED — OWNER DECISION REQUIRED"
            destination = "HomeLab #21 read-only live config inventory, then focused #135 port if required; #124 only for enhancements"
            validation = "Exact approved GPT-5.6 spellings, requested/effective model, no silent fallback, harness eligibility, Pi config/credentials"
            risk = "A live route can vanish, silently fall back, or launch with the wrong effort/provider"
    elif sha == "64216aa3b08192fa1c83df99d787aef3cb3d5ef4":
        disposition = "SUPERSEDED/OBSOLETE"
        capability = "Upstream reconciliation operator record"
        consumer = "Source reviewers"
        upstream = "Replaced by the v0.10 lineage and capability audit manifests"
        architecture = False
        destination = "docs/control-room-v0.10-lineage.json and capability audit"
        validation = "Completeness contract"
        risk = "Keeping the 0.9 reconciliation document active would misstate the source baseline"
    elif title.startswith("fix(web): keep landing controls") or "mobile-omnigent-layout" in title:
        disposition = "SOLVED UPSTREAM"
        capability = "Responsive new-chat controls"
        consumer = "Web/mobile session creation"
        upstream = "v0.10 replaced the old dialog/layout and carries current responsive coverage"
        architecture = True
        destination = "v0.10 web session-creation UI"
        validation = "Existing v0.10 responsive UI tests"
        risk = "Replaying the old component patch would conflict with the replacement UI"
    elif sha == "b878d0c1c3ec5cdd3d0568ad83a699bd192ae41c":
        disposition = "SOLVED UPSTREAM"
        capability = "Prevent Control Room work from mutating the HomeLab repository implicitly"
        consumer = "Repository-aware agent mutation gates"
        upstream = (
            "#117 v0.10 repository verifier/sentinel and pre-mutation gates enforce the boundary"
        )
        architecture = True
        destination = "PR #137 repository-boundary suite"
        validation = "#117 focused repository/host/session/worktree tests"
        risk = "Cross-repository mutation without explicit authorization"
    elif title.startswith("Revert ") or not paths:
        disposition = "SUPERSEDED/OBSOLETE"
        capability = "Merge/revert bookkeeping with no surviving independent tree capability"
        consumer = "None"
        upstream = "No source behavior to port after the net revert/merge result"
        architecture = False
        destination = "Reachable lineage only"
        validation = "Verify empty or net-neutral diff and retain commit reachability"
        risk = "None beyond losing historical evidence, which the second parent preserves"

    return {
        "sha": sha,
        "title": title,
        "changed_paths": paths,
        "capability": capability,
        "current_consumer": consumer,
        "upstream_v0_10_implementation": upstream,
        "owning_architecture_exists": architecture,
        "disposition": disposition,
        "evidence": evidence,
        "destination": destination,
        "validation_required": validation,
        "risk_if_omitted": risk,
    }


def removed_path_decision(path: str) -> dict[str, object]:
    disposition = "BLOCKED — OWNER DECISION REQUIRED"
    replacement = None
    destination = "#135 owner review"
    evidence = f"present at fork main {FORK_MAIN}; absent from candidate tree"
    if (
        path.startswith("deploy/control-room/trusted-root/")
        or path == "tests/deploy/test_control_room_trusted_root_policy.py"
    ):
        disposition = "SUPERSEDED/OBSOLETE"
        replacement = (
            "HomeLab #25 privilege boundary plus Omnigent #115 truthful capability contract"
        )
        destination = "#115"
    elif (
        path.startswith("deploy/scripts/")
        or path.startswith("tests/deploy/test_peer")
        or path == "tests/deploy/test_control_room_release_preflight.py"
    ):
        destination = "#122 after HomeLab #21/#23; required before PR #137 merge"
    elif path == "deploy/docs/control-room-upstream-reconciliation.md":
        disposition = "SUPERSEDED/OBSOLETE"
        replacement = "docs/control-room-v0.10-lineage.json and docs/control-room-v0.10-capability-audit.json"
        destination = "PR #137"
    elif path.startswith("deploy/docs/control-room-"):
        destination = (
            "#122; keep operator contract available on fork main until replacement is reviewed"
        )
    elif path in {"deploy/docs/mlflow-tracing.md", "tests/runtime/test_telemetry_mlflow_fix.py"}:
        destination = "HomeLab #5 KEEP-OPTIONAL versus RETIRE"
    elif path in {
        "omnigent/server/host_identity.py",
        "tests/server/test_host_identity_collision.py",
        "tests/server/integration/test_host_identity_collision_route.py",
    }:
        disposition = "RETAIN/REPLAY"
        replacement = path
        destination = "PR #137"
    elif path == ".github/workflows/electron-build.yml":
        disposition = "SUPERSEDED/OBSOLETE"
        replacement = "upstream c8c4f8182 removed the redundant manual Electron workflow"
        destination = "v0.10 workflow graph"
    elif (
        path.startswith("web/src/components/pwa/")
        or path.startswith("web/public/pwa-")
        or path == "tests/e2e_ui/test_pwa_e2e.py"
    ):
        disposition = "SUPERSEDED/OBSOLETE"
        replacement = "upstream ef8aba3af retired the PWA service worker/update prompt"
        destination = "v0.10 web architecture"
    elif path in {
        "web/src/lib/buildVersion.ts",
        "web/src/lib/buildVersion.test.ts",
        "web/src/shell/TitleBarServerPicker.tsx",
    }:
        disposition = "SOLVED UPSTREAM"
        replacement = "upstream 6ac1c6045 moved server selection to the sidebar and added the current version manifest"
        destination = "v0.10 desktop/sidebar architecture"
    return {
        "path": path,
        "disposition": disposition,
        "replacement": replacement,
        "destination": destination,
        "evidence": evidence,
    }


def build() -> dict[str, object]:
    commits: list[dict[str, object]] = []
    for line in git(
        "log", "--reverse", "--format=%H%x09%s", f"{MERGE_BASE}..{FORK_MAIN}"
    ).splitlines():
        sha, title = line.split("\t", 1)
        paths = git("diff-tree", "--no-commit-id", "--name-only", "-r", sha).splitlines()
        commits.append(commit_decision(sha, title, paths))

    fork_paths = set(git("ls-tree", "-r", "--name-only", FORK_MAIN).splitlines())
    candidate_paths = set(git("ls-tree", "-r", "--name-only", AUDIT_BASE).splitlines())
    removed = [removed_path_decision(path) for path in sorted(fork_paths - candidate_paths)]

    substantial = []
    for line in git("diff", "--numstat", AUDIT_BASE, FORK_MAIN).splitlines():
        added, deleted, path = line.split("\t", 2)
        if added.isdigit() and deleted.isdigit() and int(added) + int(deleted) >= 200:
            substantial.append(
                {"path": path, "fork_lines": int(added), "candidate_lines": int(deleted)}
            )

    required_capabilities = [
        {
            "id": "repository-identity",
            "disposition": "RETAIN/REPLAY",
            "required_paths": ["omnigent/host/repository_identity.py"],
            "owner": "#117",
        },
        {
            "id": "publication-checkpoints",
            "disposition": "RETAIN/REPLAY",
            "required_paths": ["omnigent/publication_checkpoint.py"],
            "owner": "#118",
        },
        {
            "id": "host-name-collision",
            "disposition": "RETAIN/REPLAY",
            "required_paths": ["omnigent/server/host_identity.py"],
            "owner": "#135",
        },
        {
            "id": "stale-stream-recovery",
            "disposition": "SOLVED UPSTREAM",
            "required_paths": [
                "web/src/store/conversationRegistry.ts",
                "web/src/store/chatStore.ts",
            ],
            "owner": "#133",
        },
        {
            "id": "peer-deployment-recovery",
            "disposition": "BLOCKED — OWNER DECISION REQUIRED",
            "required_paths": [],
            "owner": "#122 + HomeLab #21/#23",
            "merge_blocker": True,
        },
        {
            "id": "truthful-root-capability",
            "disposition": "PORT SEMANTICALLY",
            "required_paths": [],
            "owner": "#115",
            "merge_blocker": True,
        },
        {
            "id": "codex-pi-live-routing",
            "disposition": "BLOCKED — OWNER DECISION REQUIRED",
            "required_paths": [],
            "owner": "HomeLab #21 then #135",
            "merge_blocker": True,
        },
        {
            "id": "server-telemetry-provenance",
            "disposition": "PORT SEMANTICALLY",
            "required_paths": [],
            "owner": "HomeLab #5 then #135",
            "merge_blocker": True,
        },
        {
            "id": "conversation-upgrade-continuity",
            "disposition": "BLOCKED — OWNER DECISION REQUIRED",
            "required_paths": [],
            "owner": "#126 + HomeLab #21",
            "merge_blocker": True,
        },
    ]

    return {
        "schema_version": 1,
        "generated_from": {
            "merge_base": MERGE_BASE,
            "fork_main": FORK_MAIN,
            "candidate_head": AUDIT_BASE,
            "upstream_v0_10": UPSTREAM_V010,
        },
        "allowed_dispositions": sorted(ALLOWED),
        "fork_only_commits": commits,
        "tree_completeness": {
            "fork_files_absent_from_candidate": removed,
            "substantial_semantic_divergence_threshold_lines": 200,
            "substantially_divergent_files": substantial,
            "executable_entrypoints_removed": [
                row for row in removed if str(row["path"]).startswith("deploy/scripts/")
            ],
            "systemd_deployment_paths_removed": [
                row
                for row in removed
                if "systemd/" in str(row["path"]) or str(row["path"]).startswith("deploy/scripts/")
            ],
            "migrations_removed_or_replaced": [],
            "tests_removed": [row for row in removed if str(row["path"]).startswith("tests/")],
            "runtime_configuration_removed": [
                row
                for row in removed
                if "/env/" in str(row["path"])
                or "/systemd/" in str(row["path"])
                or "/sudoers/" in str(row["path"])
            ],
            "documentation_runbooks_removed": [
                row for row in removed if str(row["path"]).endswith(".md")
            ],
            "feature_ownership_changes": [
                {
                    "feature": "peer deployment/recovery",
                    "from": "fork deploy/scripts/peer_deployer",
                    "to": "#122 decision after HomeLab inventory",
                    "status": "blocked",
                },
                {
                    "feature": "root privilege policy",
                    "from": "fork unrestricted-agent-root bundle",
                    "to": "HomeLab privilege boundary + #115 truthful flag",
                    "status": "old source obsolete; flag port required",
                },
                {
                    "feature": "PWA update",
                    "from": "fork service worker banner",
                    "to": "retired upstream",
                    "status": "obsolete",
                },
                {
                    "feature": "desktop server picker",
                    "from": "title bar",
                    "to": "v0.10 sidebar/version manifest",
                    "status": "solved upstream",
                },
                {
                    "feature": "MLflow tracing",
                    "from": "fork exporter",
                    "to": "HomeLab #5 decision; v0.10 OTel remains",
                    "status": "blocked",
                },
            ],
        },
        "required_capabilities": required_capabilities,
        "merge_readiness": {
            "ready": False,
            "reason": "Explicit required capabilities remain blocked or need semantic ports; PR #137 must stay draft and unmerged.",
        },
        "safety": {
            "source_only": True,
            "deployment_performed": False,
            "service_restart_performed": False,
            "production_database_mutated": False,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    rendered = json.dumps(build(), indent=2, sort_keys=False) + "\n"
    if args.check:
        return 0 if OUTPUT.read_text() == rendered else 1
    OUTPUT.write_text(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
