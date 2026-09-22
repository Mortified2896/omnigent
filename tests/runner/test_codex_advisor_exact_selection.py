"""Advisor rounds launch with an enforced exact-selection policy.

An advisor-assigned session (``omnigent.advisor.round_id`` label) must fail
its launch when the assigned pick is not in the lane/provider's live catalog.
Ordinary sessions keep the reset/fallback behavior; the same unavailable pick
on a session WITHOUT the advisor label must still reset — proving the policy
is scoped, not global.
"""

from __future__ import annotations

import pytest

from tests.runner.test_codex_model_pick_fallback import (
    _PROVIDER_DEFAULT,
    _RETIRED_PICK,
    _SESSION_ID,
    codex_launch_harness,
)

# Re-exporting keeps the imported fixture "used" for lint purposes while the
# wrapper fixture below gives tests a non-shadowing name.
__all__ = ("codex_launch_harness",)

_ADVISOR_LABEL = "omnigent.advisor.round_id"


@pytest.fixture
def advisor_harness(codex_launch_harness):
    """Local alias so test signatures do not shadow the imported fixture."""
    return codex_launch_harness


@pytest.mark.asyncio
@pytest.mark.parametrize("stale", [False, True])
async def test_advisor_round_fails_instead_of_resetting_an_unavailable_pick(
    stale: bool, advisor_harness
) -> None:
    """An advisor-assigned pick the catalog does not serve fails the assignment."""
    harness = advisor_harness
    harness.snapshot["model_override"] = _RETIRED_PICK
    harness.snapshot["labels"] = {_ADVISOR_LABEL: "adviseround-0f61c2"}
    harness.seed_catalog([{"id": _PROVIDER_DEFAULT, "isDefault": True}], stale=stale)

    with pytest.raises(RuntimeError, match="exact-selection"):
        await harness.launch()

    # No substitution of any kind: no fallback terminal was built and the
    # server was never asked to clear the assigned pick.
    assert harness.builds == []
    assert harness.resets == []
    assert harness.snapshot["model_override"] == _RETIRED_PICK


@pytest.mark.asyncio
async def test_advisor_round_with_available_pick_launches_unchanged(advisor_harness) -> None:
    """The policy only gates unavailability; a served pick launches as pinned."""
    harness = advisor_harness
    pick = "gpt-5.6-sol"
    harness.snapshot["model_override"] = pick
    harness.snapshot["labels"] = {_ADVISOR_LABEL: "adviseround-0f61c2"}
    harness.seed_catalog([{"id": pick, "isDefault": True}])

    resource = await harness.launch()

    assert resource.id == "terminal_codex_main"
    assert harness.builds[0]["model"] == pick
    assert harness.resets == []


@pytest.mark.asyncio
async def test_advisor_round_route_qualified_codex_pick_launches_native_slug(
    advisor_harness,
) -> None:
    """The route id stays persisted while native Codex receives its bare slug."""
    harness = advisor_harness
    harness.snapshot["model_override"] = "codex/gpt-5.6-sol"
    harness.snapshot["labels"] = {
        _ADVISOR_LABEL: "adviseround-route-qualified",
        "omnigent.access_lane": "codex-direct",
    }
    harness.seed_catalog([{"id": "gpt-5.6-sol", "isDefault": True}], access_lane="codex-direct")

    await harness.launch()

    assert harness.builds[0]["model"] == "gpt-5.6-sol"
    assert harness.snapshot["model_override"] == "codex/gpt-5.6-sol"
    assert harness.resets == []


@pytest.mark.asyncio
async def test_advisor_round_rejects_effort_alias_or_substitution(advisor_harness) -> None:
    """An unsupported requested effort fails instead of being aliased."""
    harness = advisor_harness
    pick = "gpt-5.6-sol"
    harness.snapshot["model_override"] = pick
    harness.snapshot["reasoning_effort"] = "max"
    harness.snapshot["labels"] = {_ADVISOR_LABEL: "adviseround-effort"}
    harness.seed_catalog(
        [
            {
                "id": pick,
                "isDefault": True,
                "supportedReasoningEfforts": [{"reasoningEffort": "xhigh"}],
            }
        ]
    )

    with pytest.raises(RuntimeError, match="exact-selection"):
        await harness.launch()

    assert harness.builds == []
    assert harness.resets == []


@pytest.mark.asyncio
async def test_ordinary_session_still_resets_the_same_unavailable_pick(advisor_harness) -> None:
    """No advisor label: the pre-existing lane/provider fallback is unchanged."""
    harness = advisor_harness
    harness.snapshot["model_override"] = _RETIRED_PICK
    harness.snapshot["labels"] = {}
    harness.seed_catalog([{"id": _PROVIDER_DEFAULT, "isDefault": True}])

    await harness.launch()

    assert harness.builds[0]["model"] == _PROVIDER_DEFAULT
    assert harness.resets == [{"expected_model_override": _RETIRED_PICK}]


@pytest.mark.asyncio
async def test_advisor_session_id_roundtrip(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The advisor label parses into the launch config's dedicated field."""
    from omnigent.runner.native.orchestration import _codex_native_launch_config

    class _FakeResponse:
        status_code = 200

        def json(self) -> dict:
            return {
                "model_override": "gpt-5.3-codex",
                "labels": {
                    "omnigent.access_lane": "codex-direct",
                    _ADVISOR_LABEL: "adviseround-77aa",
                },
            }

    class _FakeClient:
        async def get(self, *_args, **_kwargs) -> _FakeResponse:
            return _FakeResponse()

    monkeypatch.setenv("RUNNER_SERVER_URL", "http://runner.test")
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(tmp_path))
    config = await _codex_native_launch_config(session_id=_SESSION_ID, server_client=_FakeClient())
    assert config.advisor_round_id == "adviseround-77aa"
    assert config.access_lane == "codex-direct"
