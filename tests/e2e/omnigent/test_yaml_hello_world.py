"""Phase 0 characterization test — YAML-driven agent with tools (mock LLM).

Migrated to mock LLM: uses the session-scoped mock server instead of
a real Databricks gateway.  The openai-agents harness is the only one
tested (claude-sdk and codex require vendor binaries and their own
auth, which mock mode cannot provide).

**What breaks if this fails:**
- Omnigent' YAML spec parser regresses on ``tools.*`` entries
  (``function`` / ``cancellable_function`` types).
- The wrapped harness loses its MCP tool bridging or its
  prompt-construction path.
- Per-YAML defaults fail to pick up the ``callable:`` dotted
  paths via ``importlib.import_module`` -- the tool never gets
  registered and the agent can't invoke it.
- ``omnigent.cli`` one-shot path stops streaming tool-call
  lifecycle lines to stdout.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from tests.e2e.conftest import configure_mock_llm, reset_mock_llm
from tests.e2e.omnigent._snapshot import compare_snapshot

_PROMPT = "What is 3 + 4? Use the calculate tool."

# The mock LLM will be told to emit a tool call then a text reply.
_EXPECTED_TOOL_NAME = "calculate"

_MIN_ASSISTANT_CHARS = 3

_RUN_TIMEOUT_SEC = 60


def test_yaml_agent_with_tools(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
) -> None:
    """
    Running ``omnigent run agent_with_tools.yaml --harness
    openai-agents -p <calc-prompt>`` completes cleanly and the
    ``calculate`` tool appears in stdout.

    Uses the mock LLM server to provide a canned tool-call then
    text response so the test is deterministic.
    """
    model = "mock-calc-model"
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_calc_1",
                        "name": "calculate",
                        "arguments": '{"expression": "3 + 4"}',
                    }
                ],
            },
            {"text": "The answer is 7."},
        ],
        key=model,
    )

    yaml_path = omnigent_repo_root / "tests" / "resources" / "examples" / "agent_with_tools.yaml"

    result = subprocess.run(
        [
            str(omnigent_python),
            "-m",
            "omnigent",
            "run",
            str(yaml_path),
            "--model",
            model,
            "--harness",
            "openai-agents",
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
    )

    observed: dict[str, Any] = {
        "exit_code": result.returncode,
        "stdout": result.stdout,
        "stderr_is_clean": result.stderr.strip() == "",
    }

    diffs = compare_snapshot("test_yaml_hello_world", observed)
    assert diffs == [], (
        "Snapshot mismatch for agent_with_tools run:\n"
        + "\n".join(diffs)
        + f"\n\nstdout:\n{result.stdout!r}\n\nstderr:\n{result.stderr!r}"
    )

    stripped = _strip_tool_chatter(result.stdout)
    assert len(stripped) >= _MIN_ASSISTANT_CHARS, (
        f"Assistant text shorter than {_MIN_ASSISTANT_CHARS} "
        f"chars after stripping tool lifecycle lines; got "
        f"{stripped!r} (full stdout: {result.stdout!r})"
    )
    assert _EXPECTED_TOOL_NAME in result.stdout, (
        f"Expected tool name {_EXPECTED_TOOL_NAME!r} not found "
        f"in stdout; the harness did not invoke "
        f"the calculate tool.\n\nstdout:\n{result.stdout!r}"
    )


def _strip_tool_chatter(stdout: str) -> str:
    """Remove known tool-lifecycle marker lines from stdout."""
    kept: list[str] = []
    for line in stdout.splitlines():
        stripped_line = line.strip()
        if stripped_line.startswith(("\u25e6 ", "\u2022 ")):
            continue
        kept.append(line)
    return "\n".join(kept).strip()
