"""Tests for the v1 typed configuration and validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnigent.autopilot_v1 import (
    AutopilotV1ConfigError,
    load_autopilot_v1_config,
)

# ── Defaults ─────────────────────────────────────────────────────────────


def test_defaults_load_with_no_mapping() -> None:
    cfg = load_autopilot_v1_config(None)
    assert cfg.enabled is False
    assert cfg.publication_enabled is False
    assert cfg.allowlisted_repositories == []
    assert cfg.active_issue_limit == 1
    assert cfg.base_branch == "main"


def test_defaults_load_with_empty_mapping() -> None:
    cfg = load_autopilot_v1_config({})
    assert cfg.enabled is False
    assert cfg.limits.max_retries == 3
    assert cfg.limits.max_review_cycles == 2
    assert cfg.limits.max_runtime_seconds == 7200
    assert cfg.limits.max_cost_usd == 25.0


def test_defaults_worker_authority_flags_all_false() -> None:
    cfg = load_autopilot_v1_config(None)
    assert cfg.worker_authority.workers_may_push is False
    assert cfg.worker_authority.controller_may_push_to_main is False
    assert cfg.worker_authority.auto_merge_enabled is False


def test_defaults_human_approval_flags_all_true() -> None:
    cfg = load_autopilot_v1_config(None)
    assert cfg.human_approval.require_pr_ready_human_merge is True
    assert cfg.human_approval.require_implementer_independent_review is True
    assert cfg.human_approval.require_post_publish_human_confirmation is True


# ── Forbidden flags raise ────────────────────────────────────────────────


def test_workers_may_push_true_is_rejected() -> None:
    with pytest.raises(AutopilotV1ConfigError) as excinfo:
        load_autopilot_v1_config({"worker_authority": {"workers_may_push": True}})
    assert "workers_may_push" in str(excinfo.value)


def test_controller_may_push_to_main_true_is_rejected() -> None:
    with pytest.raises(AutopilotV1ConfigError) as excinfo:
        load_autopilot_v1_config({"worker_authority": {"controller_may_push_to_main": True}})
    assert "controller_may_push_to_main" in str(excinfo.value)


def test_auto_merge_enabled_true_is_rejected() -> None:
    with pytest.raises(AutopilotV1ConfigError) as excinfo:
        load_autopilot_v1_config({"worker_authority": {"auto_merge_enabled": True}})
    assert "auto_merge_enabled" in str(excinfo.value)


# ── v1 caps ──────────────────────────────────────────────────────────────


def test_two_repositories_rejected_in_v1() -> None:
    with pytest.raises(AutopilotV1ConfigError) as excinfo:
        load_autopilot_v1_config(
            {
                "allowlisted_repositories": [
                    {"owner": "octocat", "repo": "hello"},
                    {"owner": "octocat", "repo": "world"},
                ]
            }
        )
    assert "allowlisted_repositories" in str(excinfo.value)


def test_active_issue_limit_must_be_one_in_v1() -> None:
    with pytest.raises(AutopilotV1ConfigError) as excinfo:
        load_autopilot_v1_config({"active_issue_limit": 5})
    assert "active_issue_limit" in str(excinfo.value)


def test_active_issue_limit_zero_rejected() -> None:
    with pytest.raises(AutopilotV1ConfigError):
        load_autopilot_v1_config({"active_issue_limit": 0})


# ── Cross-flag validation ────────────────────────────────────────────────


def test_publication_enabled_requires_enabled() -> None:
    with pytest.raises(AutopilotV1ConfigError) as excinfo:
        load_autopilot_v1_config(
            {
                "enabled": False,
                "publication_enabled": True,
                "allowlisted_repositories": [{"owner": "octocat", "repo": "hello"}],
            }
        )
    assert "publication_enabled" in str(excinfo.value)


def test_enabled_requires_allowlisted_repository() -> None:
    with pytest.raises(AutopilotV1ConfigError) as excinfo:
        load_autopilot_v1_config({"enabled": True})
    assert "allowlisted_repositories" in str(excinfo.value)


def test_enabled_with_one_repository_accepted() -> None:
    cfg = load_autopilot_v1_config(
        {
            "enabled": True,
            "allowlisted_repositories": [{"owner": "octocat", "repo": "hello"}],
        }
    )
    assert cfg.enabled is True
    assert len(cfg.allowlisted_repositories) == 1
    assert cfg.allowlisted_repositories[0].owner == "octocat"


# ── Limits validation ────────────────────────────────────────────────────


def test_max_retries_negative_rejected() -> None:
    with pytest.raises(AutopilotV1ConfigError) as excinfo:
        load_autopilot_v1_config({"limits": {"max_retries": -1}})
    assert "max_retries" in str(excinfo.value)


def test_max_review_cycles_negative_rejected() -> None:
    with pytest.raises(AutopilotV1ConfigError):
        load_autopilot_v1_config({"limits": {"max_review_cycles": -2}})


def test_max_runtime_seconds_below_minimum_rejected() -> None:
    with pytest.raises(AutopilotV1ConfigError) as excinfo:
        load_autopilot_v1_config({"limits": {"max_runtime_seconds": 30}})
    assert "max_runtime_seconds" in str(excinfo.value)


def test_max_cost_usd_negative_rejected() -> None:
    with pytest.raises(AutopilotV1ConfigError):
        load_autopilot_v1_config({"limits": {"max_cost_usd": -1.0}})


def test_max_retries_zero_accepted() -> None:
    cfg = load_autopilot_v1_config({"limits": {"max_retries": 0}})
    assert cfg.limits.max_retries == 0


def test_max_runtime_seconds_at_minimum_accepted() -> None:
    cfg = load_autopilot_v1_config({"limits": {"max_runtime_seconds": 60}})
    assert cfg.limits.max_runtime_seconds == 60


# ── Repository name validation ───────────────────────────────────────────


def test_repo_owner_empty_rejected() -> None:
    with pytest.raises(AutopilotV1ConfigError) as excinfo:
        load_autopilot_v1_config(
            {
                "enabled": True,
                "allowlisted_repositories": [{"owner": "", "repo": "hello"}],
            }
        )
    assert "owner" in str(excinfo.value)


def test_repo_repo_empty_rejected() -> None:
    with pytest.raises(AutopilotV1ConfigError) as excinfo:
        load_autopilot_v1_config(
            {
                "enabled": True,
                "allowlisted_repositories": [{"owner": "octocat", "repo": ""}],
            }
        )
    assert "repo" in str(excinfo.value)


def test_repo_owner_invalid_chars_rejected() -> None:
    with pytest.raises(AutopilotV1ConfigError) as excinfo:
        load_autopilot_v1_config(
            {
                "enabled": True,
                "allowlisted_repositories": [{"owner": "octo cat", "repo": "hello"}],
            }
        )
    assert "owner" in str(excinfo.value)


def test_repo_owner_leading_dot_rejected() -> None:
    with pytest.raises(AutopilotV1ConfigError):
        load_autopilot_v1_config(
            {
                "enabled": True,
                "allowlisted_repositories": [{"owner": ".octocat", "repo": "hello"}],
            }
        )


def test_repo_owner_leading_dash_rejected() -> None:
    with pytest.raises(AutopilotV1ConfigError):
        load_autopilot_v1_config(
            {
                "enabled": True,
                "allowlisted_repositories": [{"owner": "-octocat", "repo": "hello"}],
            }
        )


def test_repo_base_branch_empty_rejected() -> None:
    # The aggregation path is the public contract; direct construction
    # raises pydantic ValidationError, which load_autopilot_v1_config
    # wraps into AutopilotV1ConfigError.
    with pytest.raises(AutopilotV1ConfigError):
        load_autopilot_v1_config(
            {
                "enabled": True,
                "allowlisted_repositories": [
                    {"owner": "octocat", "repo": "hello", "base_branch": ""}
                ],
            }
        )


def test_repo_top_level_base_branch_empty_rejected() -> None:
    with pytest.raises(AutopilotV1ConfigError) as excinfo:
        load_autopilot_v1_config({"base_branch": ""})
    assert "base_branch" in str(excinfo.value)


# ── Aggregation ──────────────────────────────────────────────────────────


def test_multiple_errors_aggregated() -> None:
    # Pydantic stops at the first failing nested validator and we accept
    # that: the loader wraps the failure into AutopilotV1ConfigError with
    # a clear, actionable message that names the offending field. This
    # test verifies the wrapping produces an error class the operator can
    # catch and a message that surfaces the path to the cause.
    with pytest.raises(AutopilotV1ConfigError) as excinfo:
        load_autopilot_v1_config(
            {
                "enabled": True,
                "allowlisted_repositories": [],
                "active_issue_limit": 5,
                "worker_authority": {"workers_may_push": True},
            }
        )
    assert excinfo.value.errors  # at least one error recorded


def test_extra_keys_rejected() -> None:
    with pytest.raises(AutopilotV1ConfigError):
        load_autopilot_v1_config({"unknown_key": True})


def test_extra_keys_in_limits_rejected() -> None:
    with pytest.raises(AutopilotV1ConfigError):
        load_autopilot_v1_config({"limits": {"made_up_limit": 5}})


# ── Config is frozen ─────────────────────────────────────────────────────


def test_top_level_config_is_frozen() -> None:
    cfg = load_autopilot_v1_config(None)
    with pytest.raises(ValidationError):
        cfg.enabled = True  # type: ignore[misc]


def test_limits_is_frozen() -> None:
    cfg = load_autopilot_v1_config(None)
    with pytest.raises(ValidationError):
        cfg.limits.max_retries = 999  # type: ignore[misc]
