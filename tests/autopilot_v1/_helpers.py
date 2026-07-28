"""Helpers for the autopilot v1 backward-compat smoke test.

Kept tiny on purpose: snapshot a couple of stable module attributes so
the test can assert they are unchanged by importing the new autopilot
package. No I/O, no fixtures, no shared state.
"""

from __future__ import annotations


def snapshot_task_outcome_statuses() -> tuple[str, ...]:
    """Snapshot ``omnigent.entities.task_outcome.TASK_RUN_STATUSES``.

    The values are documented stable across releases; the autopilot v1
    package must not touch them.
    """
    from omnigent.entities import task_outcome

    return tuple(task_outcome.TASK_RUN_STATUSES)


def snapshot_config_module() -> dict[str, object]:
    """Snapshot the public surface of ``omnigent.config``.

    The autopilot v1 loader is intentionally a separate module, but
    importing it must not mutate the existing ``omnigent.config``
    globals.
    """
    try:
        import omnigent.config as cfg  # type: ignore[import-not-found]
    except ImportError:
        # No top-level ``omnigent.config`` exists in this checkout; that
        # itself is the snapshot — absence of the module is the contract.
        return {"present": False}
    out: dict[str, object] = {"present": True}
    if hasattr(cfg, "_load_global_config"):
        out["has_load_global_config"] = True
    if hasattr(cfg, "_load_local_config"):
        out["has_load_local_config"] = True
    if hasattr(cfg, "_load_effective_config"):
        out["has_load_effective_config"] = True
    return out


def snapshot_runtime_prompt() -> dict[str, object]:
    """Snapshot a small public surface of ``omnigent.runtime.prompt``.

    The autopilot package must not inject anything into prompt
    composition at import time.
    """
    try:
        import omnigent.runtime.prompt as prompt_mod  # type: ignore[import-not-found]
    except ImportError:
        return {"present": False}
    return {
        "present": True,
        "has_compose": hasattr(prompt_mod, "compose") or hasattr(prompt_mod, "build"),
    }
