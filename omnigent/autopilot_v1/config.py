"""Oversight Autopilot v1 — typed configuration and validation.

The v1 contract is conservative on purpose: every feature is opt-in, every
forbidden flag is enforced by validation, and the defaults leave the
existing omnigent runtime completely unaffected. Loading a partial or
empty mapping is safe and yields the disabled defaults.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, model_validator

# ── Error type ───────────────────────────────────────────────────────────


class AutopilotV1ConfigError(ValueError):
    """Aggregated configuration validation failure.

    Raised by :func:`load_autopilot_v1_config` when the input mapping
    produces one or more pydantic validation errors. The message lists
    every failure so the operator sees them all in one go instead of
    fixing them one at a time.
    """

    def __init__(self, errors: list[str]) -> None:
        self.errors = list(errors)
        joined = "\n  - ".join(self.errors)
        super().__init__(
            f"AutopilotV1Config validation failed ({len(self.errors)} error(s)):\n  - {joined}"
        )


# ── Shared regex / helpers ───────────────────────────────────────────────

# GitHub owner / repo name pattern. Allows alphanumerics, ``.``, ``_``,
# ``-``; no leading ``.``/``-``; bounded length to match GitHub's rules.
_GITHUB_NAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")
# GitHub names cannot start with ``.`` or ``-`` (defensive — GitHub
# rejects them).
_GITHUB_NAME_LEADING = re.compile(r"^[.-]")


def _validate_github_name(value: str, *, field: str) -> str:
    """Validate a GitHub owner or repo name. Returns the value unchanged."""
    if not value:
        raise ValueError(f"{field} must be non-empty")
    if _GITHUB_NAME_LEADING.match(value):
        raise ValueError(f"{field}={value!r} must not start with '.' or '-'; GitHub rejects these")
    if not _GITHUB_NAME_PATTERN.match(value):
        raise ValueError(f"{field}={value!r} contains characters outside [A-Za-z0-9._-]")
    if len(value) > 100:
        raise ValueError(f"{field}={value!r} exceeds GitHub's 100-character limit")
    return value


# ── Sub-models ───────────────────────────────────────────────────────────


class AutopilotRepositoryConfig(BaseModel):
    """One allowlisted repository for v1.

    v1 restricts the autopilot to a single repository so the operator
    can keep blast radius tight. The validator enforces the naming
    rules used by GitHub.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    owner: str
    repo: str
    base_branch: str = "main"

    @model_validator(mode="after")
    def _validate_names(self) -> AutopilotRepositoryConfig:
        # Use object.__setattr__ to mutate frozen fields during validation.
        errors: list[str] = []
        try:
            owner = _validate_github_name(self.owner, field="owner")
        except ValueError as exc:
            errors.append(str(exc))
            owner = self.owner
        try:
            repo = _validate_github_name(self.repo, field="repo")
        except ValueError as exc:
            errors.append(str(exc))
            repo = self.repo
        if not self.base_branch:
            errors.append("base_branch must be non-empty")
        if errors:
            raise ValueError(" | ".join(errors))
        if owner != self.owner or repo != self.repo:
            object.__setattr__(self, "owner", owner)
            object.__setattr__(self, "repo", repo)
        return self


class AutopilotLimitsConfig(BaseModel):
    """Bounded run limits enforced by the controller (issue #22)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_retries: int = 3
    max_review_cycles: int = 2
    max_runtime_seconds: int = 7200  # 2 hours
    max_cost_usd: float = 25.0

    @model_validator(mode="after")
    def _validate_limits(self) -> AutopilotLimitsConfig:
        errors: list[str] = []
        if self.max_retries < 0:
            errors.append(f"limits.max_retries={self.max_retries} must be >= 0")
        if self.max_review_cycles < 0:
            errors.append(f"limits.max_review_cycles={self.max_review_cycles} must be >= 0")
        if self.max_runtime_seconds < 60:
            errors.append(
                f"limits.max_runtime_seconds={self.max_runtime_seconds} "
                "must be >= 60 (controller refuses sub-minute runs)"
            )
        if self.max_cost_usd < 0:
            errors.append(f"limits.max_cost_usd={self.max_cost_usd} must be >= 0")
        if errors:
            raise ValueError(" | ".join(errors))
        return self


class AutopilotHumanApprovalConfig(BaseModel):
    """Toggles for which approvals a human must give before advancing.

    v1 forces every entry on by default — disabling any of these is not
    yet supported and would be unsafe. The defaults are exposed as fields
    so the future controller can read them; flipping any to ``False``
    in v1 is reserved for a future release.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    require_pr_ready_human_merge: bool = True
    require_implementer_independent_review: bool = True
    require_post_publish_human_confirmation: bool = True


class AutopilotWorkerAuthorityConfig(BaseModel):
    """Authority flags for workers, controller, and auto-merge.

    v1 forbids workers from pushing, forbids the controller from pushing
    to ``main``, and forbids auto-merge. These are enforced as validation
    errors rather than silent ignores — flipping any to ``True`` is
    invalid in v1 and would let an operator unknowingly bypass the
    human-in-the-loop guarantees.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    workers_may_push: bool = False
    controller_may_push_to_main: bool = False
    auto_merge_enabled: bool = False

    @model_validator(mode="after")
    def _enforce_v1_forbidden_flags(self) -> AutopilotWorkerAuthorityConfig:
        if self.workers_may_push is True:
            raise ValueError(
                "worker_authority.workers_may_push=True is forbidden in v1; "
                "workers may only commit locally and create branches. "
                "Pushing is reserved for the controller or humans."
            )
        if self.controller_may_push_to_main is True:
            raise ValueError(
                "worker_authority.controller_may_push_to_main=True is forbidden "
                "in v1; only humans may push to a base branch."
            )
        if self.auto_merge_enabled is True:
            raise ValueError(
                "worker_authority.auto_merge_enabled=True is forbidden in v1; "
                "auto-merge bypasses the human merge review required by v1."
            )
        return self


# ── Top-level config ─────────────────────────────────────────────────────


class AutopilotV1Config(BaseModel):
    """Typed configuration for the Oversight Autopilot v1.

    Defaults make the feature completely inert: ``enabled=False``, no
    repositories, no active issues, no worker push, no controller push
    to ``main``, no auto-merge. A future operator who wants the feature
    must explicitly opt in.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    publication_enabled: bool = False
    allowlisted_repositories: list[AutopilotRepositoryConfig] = []
    active_issue_limit: int = 1
    base_branch: str = "main"
    limits: AutopilotLimitsConfig = AutopilotLimitsConfig()
    human_approval: AutopilotHumanApprovalConfig = AutopilotHumanApprovalConfig()
    worker_authority: AutopilotWorkerAuthorityConfig = AutopilotWorkerAuthorityConfig()

    @model_validator(mode="after")
    def _validate_v1_constraints(self) -> AutopilotV1Config:
        if len(self.allowlisted_repositories) > 1:
            raise ValueError(
                f"allowlisted_repositories has {len(self.allowlisted_repositories)} "
                "entries; v1 supports at most 1 repository. Drop the extras or "
                "wait for the v2 multi-repo release."
            )
        if self.active_issue_limit != 1:
            raise ValueError(
                f"active_issue_limit={self.active_issue_limit} is invalid in v1; "
                "v1 supports exactly 1 active issue (active_issue_limit=1). "
                "Larger fleets are reserved for a future release."
            )
        if self.publication_enabled and not self.enabled:
            raise ValueError(
                "publication_enabled=True requires enabled=True; the autopilot "
                "feature must be enabled before publication can be enabled."
            )
        if self.enabled and len(self.allowlisted_repositories) == 0:
            raise ValueError(
                "enabled=True requires allowlisted_repositories to contain "
                "exactly one entry; the v1 contract refuses to start the "
                "controller with zero allowlisted repositories."
            )
        if not self.base_branch:
            raise ValueError("base_branch must be non-empty")
        return self


# ── Loader ───────────────────────────────────────────────────────────────


def load_autopilot_v1_config(
    mapping: dict | None = None,
) -> AutopilotV1Config:
    """Load and validate an :class:`AutopilotV1Config`.

    Accepts a raw mapping (e.g. parsed from YAML). Returns the defaults
    when ``mapping`` is ``None`` or empty so callers can use this as
    ``load_autopilot_v1_config(global_cfg.get("autopilot_v1"))``.

    Aggregates every pydantic validation error into a single
    :class:`AutopilotV1ConfigError` so the operator sees all failures
    at once.

    :raises AutopilotV1ConfigError: When the mapping produces one or
        more validation errors. The exception's ``errors`` attribute
        exposes the underlying list of human-readable messages.
    """
    payload = mapping if mapping is not None else {}
    try:
        return AutopilotV1Config.model_validate(payload)
    except Exception as exc:
        errors = _extract_validation_messages(exc)
        if not errors:
            errors = [str(exc)]
        raise AutopilotV1ConfigError(errors) from exc


def _extract_validation_messages(exc: Exception) -> list[str]:
    """Pull every error message out of a pydantic ValidationError.

    pydantic exposes ``errors()`` returning a list of dicts with a
    ``msg`` key. Nested errors share the same interface. Walk them
    flat so the aggregated message is linear.
    """
    try:
        raw_errors = exc.errors()  # type: ignore[attr-defined]
    except AttributeError:
        return []
    out: list[str] = []
    for entry in raw_errors:
        loc = entry.get("loc") or ()
        msg = entry.get("msg") or "<unknown pydantic error>"
        if loc:
            loc_str = ".".join(str(part) for part in loc)
            out.append(f"{loc_str}: {msg}")
        else:
            out.append(msg)
    return out
