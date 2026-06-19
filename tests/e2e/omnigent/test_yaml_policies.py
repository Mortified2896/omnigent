"""Phase 0 characterization test -- ``agent_with_policies.yaml`` (mock LLM).

Migrated to mock LLM. The policy engine's input classifier runs a
separate LLM call (through the policy's ``executor``), so the mock
must serve TWO responses: one for the policy judge (returning a DENY
verdict JSON) and one for the base model (which should never be
reached because the judge denies first).

The policy YAML pins the executor model for the ``block_canada_input``
policy, and the base model is passed via ``--model``. We configure
separate keyed queues so each model gets its own response.

**What breaks if this fails:**
- Omnigent' policy engine regresses.
- YAML spec parsing regresses on the ``policies:`` block.
- The prompt-policy evaluator drops the ``reason`` field.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from tests.e2e.conftest import configure_mock_llm, reset_mock_llm
from tests.e2e.omnigent._snapshot import compare_snapshot

_PROMPT = "Name the provinces of Canada."

_DENIED_MARKER = "[Denied by policy: Canada-related topics are denied"

_RUN_TIMEOUT_SEC = 60


def test_yaml_policies_blocks_canada_input(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
) -> None:
    """
    ``omnigent run agent_with_policies.yaml --harness openai-agents
    -p "Name the provinces of Canada."`` exits 0 and stdout
    contains the denial marker.

    The mock LLM is configured to return a DENY verdict for the
    policy judge model. The base model queue is also configured
    but should never be consumed.
    """
    base_model = "mock-policy-base"
    reset_mock_llm(mock_llm_server_url)

    # Read the policy YAML to find the policy judge's model name.
    # The ``block_canada_input`` policy uses the model from the
    # policy's ``executor.model`` field. We need to configure the
    # mock for that model key.
    yaml_path = (
        omnigent_repo_root / "tests" / "resources" / "examples" / "agent_with_policies.yaml"
    )

    # The policy executor model is set in the YAML. Rather than
    # parsing it, use the "default" queue which catches any model
    # not explicitly keyed. The policy judge is called first and
    # consumes the first default-queue response.
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "text": '{"action": "DENY", "reason": "Canada-related topics are denied."}',
            },
        ],
    )
    # Base model queue (should not be reached, but configure to
    # avoid a 500 if the deny path regresses).
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "This should not be reached."}],
        key=base_model,
    )

    result = subprocess.run(
        [
            str(omnigent_python),
            "-m",
            "omnigent",
            "run",
            str(yaml_path),
            "--harness",
            "openai-agents",
            "--model",
            base_model,
            "-p",
            _PROMPT,
            "--no-log",
            "--no-session",
        ],
        env=mock_credentials_env,
        cwd=str(omnigent_repo_root),
        capture_output=True,
        text=True,
        timeout=_RUN_TIMEOUT_SEC,
        stdin=subprocess.DEVNULL,
    )

    observed: dict[str, Any] = {
        "exit_code": result.returncode,
        "stdout": result.stdout,
    }

    diffs = compare_snapshot("test_yaml_policies", observed)
    assert diffs == [], (
        "Snapshot mismatch for agent_with_policies run:\n"
        + "\n".join(diffs)
        + f"\n\nstdout:\n{result.stdout!r}\n\nstderr:\n{result.stderr!r}"
    )
    assert _DENIED_MARKER in result.stdout, (
        f"Expected policy-denial marker {_DENIED_MARKER!r} in "
        f"stdout -- ``block_canada_input`` should have blocked "
        f"the prompt.\n\nstdout:\n{result.stdout!r}"
    )
