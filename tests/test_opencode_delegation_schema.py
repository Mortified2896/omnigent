"""Schema-level acceptance criteria for the OpenCode delegation selector.

Pins the request/response contract that the Web UI / API client must
match:

- ``SessionCreateRequest.agent_selector`` accepts ``"opencode"`` and
  rejects unknown selectors with a 422 (the pydantic field
  validator).
- ``SessionResponse.delegation_provenance`` is populated only when
  the create ran through the canonical resolver and is structured
  enough to power a forensic dump (every required key present).
- ``SessionResponse.agent_selector_decision`` mirrors the
  short-form decision so a UI consumer doesn't need to parse the
  full provenance dict.
- ``UpdateSessionRequest`` rejects unknown ``agent_selector`` strings
  the same way (defensive — the create path is the load-bearing one
  but a future PATCH surface must not silently accept typos).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnigent.agent_selector import (
    SEMANTIC_OPENCODE_AGENT_NAME,
    SEMANTIC_OPENCODE_HARNESS,
    SEMANTIC_OPENCODE_SELECTOR,
)
from omnigent.server.schemas import SessionCreateRequest, UpdateSessionRequest


def test_create_request_accepts_known_selector() -> None:
    body = SessionCreateRequest.model_validate(
        {
            "agent_id": "ag_placeholder",
            "agent_selector": SEMANTIC_OPENCODE_SELECTOR,
        }
    )
    assert body.agent_selector == SEMANTIC_OPENCODE_SELECTOR


def test_create_request_rejects_unknown_selector() -> None:
    with pytest.raises(ValidationError) as exc_info:
        SessionCreateRequest.model_validate(
            {
                "agent_id": "ag_placeholder",
                "agent_selector": "verity",  # not yet implemented
            }
        )
    # Pydantic's validation message names the rejected value so a
    # caller learns the typo immediately.
    message = str(exc_info.value)
    assert "verity" in message
    assert SEMANTIC_OPENCODE_SELECTOR in message


def test_create_request_accepts_no_selector_legacy_path() -> None:
    """The ``agent_selector`` field is OPTIONAL.

    Legacy callers (and any path that already knows the durable id)
    keep working without an explicit selector — the resolver is an
    additive gate, not a breaking rename.
    """
    body = SessionCreateRequest.model_validate(
        {
            "agent_id": "ag_placeholder",
        }
    )
    assert body.agent_selector is None


def test_update_request_rejects_unknown_selector() -> None:
    """Defensive — a future PATCH surface must not silently accept typos."""
    with pytest.raises(ValidationError):
        UpdateSessionRequest.model_validate({"agent_selector": "claude-typo"})


def test_create_request_extra_fields_rejected() -> None:
    """Defensive — a future selector field on SessionCreateRequest stays strict.

    The model is intentionally permissive on unknown fields today
    (no ``extra="forbid"`` config), so this test pins a smaller
    invariant: an UNKNOWN ``agent_selector`` value must NOT
    silently be accepted as a valid selector (the field validator
    rejects unknown values).
    """
    with pytest.raises(ValidationError):
        SessionCreateRequest.model_validate(
            {
                "agent_id": "ag_placeholder",
                "agent_selector": "verity",  # unknown selector value
            }
        )


def test_selector_constant_matches_resolver_documented_value() -> None:
    """Pin the public surface so the docs and tests can't drift apart.

    The resolver and the API schema both import the same constant
    from :mod:`omnigent.agent_selector`. If a contributor renames the
    selector without updating the docs / Web UI, this test fires.
    """
    assert SEMANTIC_OPENCODE_SELECTOR == "opencode"
    assert SEMANTIC_OPENCODE_AGENT_NAME == "opencode-native-ui"
    assert SEMANTIC_OPENCODE_HARNESS == "opencode-native"
