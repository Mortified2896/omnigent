"""Oversight Autopilot v1 — public API.

The package is opt-in. Importing it does NOT mutate any global state in
``omnigent``; the feature is disabled by default and no runtime path
references this module. Future controller / persistence / publication
layers (#18–#24) will compose these types without changing them.
"""

from omnigent.autopilot_v1.config import (
    AutopilotHumanApprovalConfig,
    AutopilotLimitsConfig,
    AutopilotRepositoryConfig,
    AutopilotV1Config,
    AutopilotV1ConfigError,
    AutopilotWorkerAuthorityConfig,
    load_autopilot_v1_config,
)
from omnigent.autopilot_v1.contracts import (
    AUTOPILOT_LIMITS_MAX_RETRIES_DEFAULT,
    AUTOPILOT_LIMITS_MAX_REVIEW_CYCLES_DEFAULT,
    ClarificationRequest,
    CompletionKind,
    Eligibility,
    IssueIdentifier,
    IssueRunStatus,
    classify_completion,
    is_eligible_for_run,
    requires_clarification,
)
from omnigent.autopilot_v1.states import (
    BLOCKED_STATES,
    COMPLETION_STATES,
    HUMAN_INTERVENTION_STATES,
    NON_RETRYABLE_FAILURE_STATES,
    NON_TERMINAL_STATES,
    RETRYABLE_FAILURE_STATES,
    TERMINAL_STATES,
    OversightAutopilotState,
    is_blocked,
    is_completion,
    is_retryable_failure,
    is_terminal,
)
from omnigent.autopilot_v1.states import (
    requires_human_intervention as state_requires_human_intervention,
)
from omnigent.autopilot_v1.transitions import (
    LEGAL_TRANSITIONS,
    IllegalTransitionError,
    assert_legal_transition,
    is_legal_transition,
    legal_next_states,
)
from omnigent.autopilot_v1.writes import (
    WRITE_AUTHORITY,
    AuthorityDecision,
    GitHubWriteKind,
    GitWriteKind,
    Principal,
    WriteNotAllowedError,
    assert_write_allowed,
    classify_write,
)

# The state module exports a function called ``requires_human_intervention``;
# alias it under ``state_requires_human_intervention`` for clarity at call
# sites and re-export the contracts module's name ``requires_human_intervention``
# separately. To avoid an alias collision, re-export both:

requires_human_intervention = state_requires_human_intervention

__all__ = [
    # contracts
    "AUTOPILOT_LIMITS_MAX_RETRIES_DEFAULT",
    "AUTOPILOT_LIMITS_MAX_REVIEW_CYCLES_DEFAULT",
    # states
    "BLOCKED_STATES",
    "COMPLETION_STATES",
    "HUMAN_INTERVENTION_STATES",
    # transitions
    "LEGAL_TRANSITIONS",
    "NON_RETRYABLE_FAILURE_STATES",
    "NON_TERMINAL_STATES",
    "RETRYABLE_FAILURE_STATES",
    "TERMINAL_STATES",
    "WRITE_AUTHORITY",
    # writes
    "AuthorityDecision",
    # config
    "AutopilotHumanApprovalConfig",
    "AutopilotLimitsConfig",
    "AutopilotRepositoryConfig",
    "AutopilotV1Config",
    "AutopilotV1ConfigError",
    "AutopilotWorkerAuthorityConfig",
    "ClarificationRequest",
    "CompletionKind",
    "Eligibility",
    "GitHubWriteKind",
    "GitWriteKind",
    "IllegalTransitionError",
    "IssueIdentifier",
    "IssueRunStatus",
    "OversightAutopilotState",
    "Principal",
    "WriteNotAllowedError",
    "assert_legal_transition",
    "assert_write_allowed",
    "classify_completion",
    "classify_write",
    "is_blocked",
    "is_completion",
    "is_eligible_for_run",
    "is_legal_transition",
    "is_retryable_failure",
    "is_terminal",
    "legal_next_states",
    "load_autopilot_v1_config",
    "requires_clarification",
    "requires_human_intervention",
    "state_requires_human_intervention",
]
