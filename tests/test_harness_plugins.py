from __future__ import annotations

import importlib
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

import omnigent.harness_plugins as hp
from omnigent.harness_install_spec import HarnessInstallSpec


class _EntryPoint:
    def __init__(self, name: str, loader: Callable[[], hp.HarnessContribution]) -> None:
        self.name = name
        self._loader = loader

    def load(self) -> Callable[[], hp.HarnessContribution]:
        return self._loader


@pytest.fixture(autouse=True)
def _reset_plugin_state() -> Iterator[None]:
    hp.reset_plugin_state_for_tests()
    yield
    hp.reset_plugin_state_for_tests()


def _install_entry_points(
    monkeypatch: pytest.MonkeyPatch,
    *entry_points: _EntryPoint,
) -> None:
    monkeypatch.setattr(
        hp.importlib.metadata,
        "entry_points",
        lambda: {hp.COMMUNITY_ENTRY_POINT_GROUP: entry_points},
    )


def test_community_harness_contribution_is_merged(monkeypatch: pytest.MonkeyPatch) -> None:
    def _contribution() -> hp.HarnessContribution:
        return hp.HarnessContribution(
            name="omnigent-foo",
            valid_harnesses=frozenset({"foo"}),
            harness_modules={"foo": "omnigent.community.harness.foo.inner.foo_harness"},
            aliases={"foo-code": "foo"},
            model_env_keys={"foo": "HARNESS_FOO_MODEL"},
            spawn_env_builders={"foo": "omnigent.community.harness.foo.plugin:build_spawn_env"},
            harness_labels={"foo": "Foo"},
        )

    _install_entry_points(monkeypatch, _EntryPoint("foo", _contribution))

    assert "foo" in hp.valid_harnesses()
    assert hp.harness_aliases()["foo-code"] == "foo"
    assert hp.harness_modules()["foo-code"] == "omnigent.community.harness.foo.inner.foo_harness"
    assert hp.model_env_keys()["foo"] == "HARNESS_FOO_MODEL"
    assert (
        hp.spawn_env_builders()["foo"] == "omnigent.community.harness.foo.plugin:build_spawn_env"
    )
    assert {"id": "foo", "label": "Foo"} in hp.harness_catalog()


def test_community_harness_rejects_non_community_import_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _contribution() -> hp.HarnessContribution:
        return hp.HarnessContribution(
            name="omnigent-foo",
            valid_harnesses=frozenset({"foo"}),
            harness_modules={"foo": "omnigent_foo.inner.foo_harness"},
        )

    _install_entry_points(monkeypatch, _EntryPoint("foo", _contribution))

    state = hp.plugin_state()
    assert "foo" in state.load_errors
    assert "foo" not in hp.valid_harnesses()


def test_community_harness_rejects_builtin_collision(monkeypatch: pytest.MonkeyPatch) -> None:
    def _contribution() -> hp.HarnessContribution:
        return hp.HarnessContribution(
            name="omnigent-evil",
            valid_harnesses=frozenset({"claude-sdk"}),
            harness_modules={"claude-sdk": "omnigent.community.harness.evil.inner.evil_harness"},
        )

    _install_entry_points(monkeypatch, _EntryPoint("evil", _contribution))

    state = hp.plugin_state()
    assert "evil" in state.load_errors
    assert hp.harness_modules()["claude-sdk"] == "omnigent.inner.claude_sdk_harness"


def test_community_harness_rejects_alias_collision_with_builtin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _contribution() -> hp.HarnessContribution:
        return hp.HarnessContribution(
            name="omnigent-evil",
            valid_harnesses=frozenset({"foo"}),
            harness_modules={"foo": "omnigent.community.harness.evil.inner.foo_harness"},
            aliases={"claude-sdk": "foo"},
        )

    _install_entry_points(monkeypatch, _EntryPoint("evil", _contribution))

    state = hp.plugin_state()
    assert "evil" in state.load_errors
    assert "foo" not in hp.valid_harnesses()


def test_community_harness_rejects_community_collision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _first() -> hp.HarnessContribution:
        return hp.HarnessContribution(
            name="omnigent-foo",
            valid_harnesses=frozenset({"foo"}),
            harness_modules={"foo": "omnigent.community.harness.foo.inner.foo_harness"},
        )

    def _second() -> hp.HarnessContribution:
        return hp.HarnessContribution(
            name="omnigent-bar",
            valid_harnesses=frozenset({"foo"}),
            harness_modules={"foo": "omnigent.community.harness.bar.inner.foo_harness"},
        )

    _install_entry_points(
        monkeypatch,
        _EntryPoint("foo", _first),
        _EntryPoint("bar", _second),
    )

    state = hp.plugin_state()
    assert "bar" in state.load_errors
    assert hp.harness_modules()["foo"] == "omnigent.community.harness.foo.inner.foo_harness"


def test_community_harness_rejects_native_terminal_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _contribution() -> hp.HarnessContribution:
        return hp.HarnessContribution(
            name="omnigent-foo",
            valid_harnesses=frozenset({"foo-native"}),
            harness_modules={"foo-native": "omnigent.community.harness.foo.inner.foo_harness"},
            native_harnesses=frozenset({"foo-native"}),
        )

    _install_entry_points(monkeypatch, _EntryPoint("foo", _contribution))

    state = hp.plugin_state()
    assert "foo" in state.load_errors
    assert "foo-native" not in hp.valid_harnesses()


def test_community_harness_readiness_uses_install_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _contribution() -> hp.HarnessContribution:
        return hp.HarnessContribution(
            name="omnigent-foo",
            valid_harnesses=frozenset({"foo"}),
            harness_modules={"foo": "omnigent.community.harness.foo.inner.foo_harness"},
            aliases={"foo-code": "foo"},
            install_specs={
                "foo": HarnessInstallSpec(
                    "Foo",
                    "foo-cli",
                    package=None,
                    install_hint="install foo-cli",
                )
            },
            harness_install_keys={"foo": "foo", "foo-code": "foo"},
        )

    _install_entry_points(monkeypatch, _EntryPoint("foo", _contribution))

    from omnigent.onboarding import harness_readiness as readiness

    monkeypatch.setattr(readiness.shutil, "which", lambda _binary: None)
    assert readiness.harness_is_configured("foo") is False
    configured = readiness.configured_harness_map()
    assert configured["foo"] is False
    assert configured["foo-code"] is False

    monkeypatch.setattr(readiness.shutil, "which", lambda binary: f"/usr/bin/{binary}")
    assert readiness.harness_is_configured("foo") is True


def test_community_namespace_imports_external_harness_package(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    package_root = tmp_path / "plugin"
    package_dir = package_root / "omnigent" / "community" / "harness" / "foo"
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("VALUE = 'ok'\n", encoding="utf-8")

    monkeypatch.syspath_prepend(str(package_root))

    import omnigent.community as community
    import omnigent.community.harness as harnesses

    importlib.reload(community)
    importlib.reload(harnesses)
    sys.modules.pop("omnigent.community.harness.foo", None)

    module = importlib.import_module("omnigent.community.harness.foo")
    assert module.VALUE == "ok"


def test_opencode_backed_subscription_lanes_are_registered() -> None:
    """The OpenCode-backed MiniMax Token Plan and Codex Subscription lanes
    are registered as native harnesses alongside the OpenCode Free lane.

    The three lanes MUST stay distinct at every layer (harness id,
    wrapper label, native agent entry) so a stored model pick from
    one lane never carries into another lane's runner. A regression
    that drops one of the entries would silently route a
    subscription session through the OpenCode Free lane.
    """
    native_agents = hp.native_agents()
    by_key = {agent.key: agent for agent in native_agents}
    # All three OpenCode-backed lanes are present as native coding
    # agents.
    assert "opencode" in by_key
    assert "opencode-minimax-token-plan" in by_key
    assert "opencode-codex-subscription" in by_key
    # Harness ids are distinct.
    opencode = by_key["opencode"].harness
    minimax = by_key["opencode-minimax-token-plan"].harness
    codex_sub = by_key["opencode-codex-subscription"].harness
    assert opencode == "opencode-native"
    assert minimax == "opencode-native-minimax-token-plan"
    assert codex_sub == "opencode-native-codex-subscription"
    assert len({opencode, minimax, codex_sub}) == 3
    # Wrapper labels are distinct (ChatPage and resume dispatcher key
    # off these).
    opencode_wrapper = by_key["opencode"].wrapper_label
    minimax_wrapper = by_key["opencode-minimax-token-plan"].wrapper_label
    codex_sub_wrapper = by_key["opencode-codex-subscription"].wrapper_label
    assert len({opencode_wrapper, minimax_wrapper, codex_sub_wrapper}) == 3


def test_opencode_backed_subscription_lanes_have_module_paths() -> None:
    """Each OpenCode-backed lane is wired into ``harness_modules``.

    The runner launches the lane via the registered module's
    ``create_app`` factory, so a missing module would prevent the
    server from binding the harness.
    """
    modules = hp.harness_modules()
    assert "opencode-native" in modules
    assert "opencode-native-minimax-token-plan" in modules
    assert "opencode-native-codex-subscription" in modules
    # Each module path points at the canonical ``omnigent.inner``
    # namespace, so the runner's import path matches what the
    # bundled agent spec expects.
    assert modules["opencode-native"] == "omnigent.inner.opencode_native_harness"
    assert (
        modules["opencode-native-minimax-token-plan"]
        == "omnigent.inner.opencode_native_minimax_token_plan_harness"
    )
    assert (
        modules["opencode-native-codex-subscription"]
        == "omnigent.inner.opencode_native_codex_subscription_harness"
    )


def test_opencode_backed_subscription_lanes_are_listed_in_valid_harnesses() -> None:
    """The three OpenCode-backed lanes are all listed in ``valid_harnesses``.

    The server-side session-create validation consults this set to
    decide which harnesses a session can bind. A missing entry would
    silently reject the lane at session-create time.
    """
    valid = hp.valid_harnesses()
    assert "opencode-native" in valid
    assert "opencode-native-minimax-token-plan" in valid
    assert "opencode-native-codex-subscription" in valid


def test_opencode_backed_subscription_lanes_are_native() -> None:
    """The three OpenCode-backed lanes are recognised as native harnesses.

    The picker / runner uses ``native_harnesses()`` to distinguish the
    native-CLI harnesses from in-process SDK harnesses — a missing
    entry would route a native session through the wrong code path.
    """
    native = hp.native_harnesses()
    assert "opencode-native" in native
    assert "opencode-native-minimax-token-plan" in native
    assert "opencode-native-codex-subscription" in native
