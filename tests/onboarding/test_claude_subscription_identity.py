"""Subscription routing requires CLI-owned OAuth, not merely a working key."""

import json
from types import SimpleNamespace

import pytest

from omnigent.onboarding.harness_install import claude_subscription_identity


@pytest.mark.parametrize(
    "patch,qualified",
    [
        ({}, True),
        ({"authMethod": "apiKey"}, False),
        ({"loggedIn": False}, False),
        ({"apiProvider": "bedrock"}, False),
        ({"email": None, "orgId": None}, False),
    ],
)
def test_subscription_identity_is_account_bound_and_does_not_expose_the_account(
    monkeypatch, patch, qualified
):
    status = {
        "loggedIn": True,
        "authMethod": "oauth",
        "apiProvider": "firstParty",
        "email": "example@test",
        "orgId": "org-test",
        **patch,
    }
    monkeypatch.setattr(
        "omnigent.onboarding.harness_install.shutil.which", lambda _: "/bin/claude"
    )
    monkeypatch.setattr(
        "omnigent.onboarding.harness_install.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(stdout=json.dumps(status), returncode=0),
    )
    identity = claude_subscription_identity()
    assert bool(identity) == qualified
    if qualified:
        assert identity.startswith("claude-plan:")
        assert "example" not in identity and "org-test" not in identity
        status["orgId"] = "other-org"
        assert claude_subscription_identity() != identity
