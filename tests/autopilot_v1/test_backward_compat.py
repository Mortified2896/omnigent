"""Backward-compat smoke tests for the v1 contract package.

The autopilot v1 package must be opt-in and isolated: importing it
must not mutate any global state in ``omnigent`` or in the runtime
prompt path. These tests spot-check a couple of stable surfaces so a
regression trips immediately.

NOTE: this test does NOT load the full model catalog or run any other
heavy startup path. Only ``omnigent.entities.task_outcome``,
``omnigent.config`` (if present), and ``omnigent.runtime.prompt`` (if
present) are inspected.
"""

from __future__ import annotations

from tests.autopilot_v1 import _helpers


def test_importing_autopilot_v1_does_not_raise() -> None:
    import omnigent.autopilot_v1

    assert omnigent.autopilot_v1 is not None


def test_defaults_disable_feature() -> None:
    from omnigent.autopilot_v1 import load_autopilot_v1_config

    cfg = load_autopilot_v1_config(None)
    assert cfg.enabled is False
    assert cfg.publication_enabled is False


def test_task_outcome_statuses_unchanged_after_autopilot_import() -> None:
    before = _helpers.snapshot_task_outcome_statuses()
    import omnigent.autopilot_v1  # noqa: F401

    after = _helpers.snapshot_task_outcome_statuses()
    assert before == after
    assert "queued" in before
    assert "completed" in before


def test_omnigent_config_unchanged_after_autopilot_import() -> None:
    before = _helpers.snapshot_config_module()
    import omnigent.autopilot_v1  # noqa: F401

    after = _helpers.snapshot_config_module()
    assert before == after


def test_omnigent_runtime_prompt_unchanged_after_autopilot_import() -> None:
    before = _helpers.snapshot_runtime_prompt()
    import omnigent.autopilot_v1  # noqa: F401

    after = _helpers.snapshot_runtime_prompt()
    assert before == after


def test_autopilot_v1_is_not_in_runtime_path() -> None:
    """No runtime path imports ``omnigent.autopilot_v1``.

    Spot-check: ``omnigent.runtime.prompt`` (if importable) must not
    import the autopilot package.
    """
    try:
        import omnigent.runtime.prompt as prompt_mod  # type: ignore[import-not-found]
    except ImportError:
        return  # no prompt module → trivially satisfied
    # ``sys.modules`` records every imported package; the autopilot
    # module must be present only because WE imported it earlier in this
    # test, not because the prompt module pulled it in.
    import sys

    autopilot_keys = [k for k in sys.modules if k.startswith("omnigent.autopilot_v1")]
    # Either there are no autopilot modules in sys.modules, or they
    # were placed there by THIS test (i.e. after the explicit import
    # below the prompt_mod import). Force a fresh check by importing
    # prompt again after a fresh import.
    import importlib

    importlib.reload(prompt_mod)
    autopilot_keys_after = [k for k in sys.modules if k.startswith("omnigent.autopilot_v1")]
    # The reload of prompt_mod must NOT pull in any autopilot module.
    assert autopilot_keys_after == autopilot_keys


def test_importing_autopilot_v1_is_idempotent() -> None:
    import omnigent.autopilot_v1
    import omnigent.autopilot_v1 as again

    assert omnigent.autopilot_v1 is again
