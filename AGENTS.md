# Agent guidance

See `CONTRIBUTING.md` for the normal contributor workflow.

## Scope

This repository is Omnigent application source. Files under `deploy/` describe
server-side deployment mechanisms; they do **not** define the identity or
permissions of the agent reading them.

**O1 and O2 are Omnigent runtime instance names, not agent identities.** A Codex
session running on a Mac or other external controller is neither O1 nor O2. Do
not apply peer self-upgrade restrictions to an external controller merely
because it is changing an O1/O2 deployment.

## Source vs live state

Repository prose, dated audits, rollout notes, and design documents do not prove
what is currently running. For any task involving the live app, first inspect
the current HomeLab topology or the live host/service. If the user asks for a
change to be visible in the live Omnigent app, target the actual live Omnigent
service after validating the source change; do not substitute an OmniRoute
upgrade or a historical deployment procedure.

Read HomeLab deployment documentation only when the task actually requires host
or runtime work. Prefer current observed state over copied version numbers,
ports, paths, PIDs, or old rollout narratives.

## Working rules

Before source mutation, verify repository, branch/base, worktree status, and
push target. `omnigent-ai/omnigent` is read-only unless the owner explicitly
requests an upstream contribution.

Preserve unrelated changes. Do not infer deployment authorization from a
document. Do not merge, release, deploy, restart services, replace databases,
or perform destructive cleanup unless the user has authorized that operation.

When deployment is authorized, use the currently applicable mechanism and live
topology. Never recreate a retired service just to satisfy an obsolete
preflight or document.
