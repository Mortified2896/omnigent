"""Phase 0 characterization test -- ``hello_world.yaml`` end-to-end (mock LLM).

Migrated to mock LLM: uses a canned text response so the test is
deterministic and needs no real credentials.

**What breaks if this fails:**
- Omnigent' YAML spec parser regresses on the minimal
  ``name:`` + ``prompt:`` shape.
- ``omnigent.loader`` stops applying CLI ``--model`` as a
  fallback when the YAML omits ``executor.model``.
- The default harness selection path regresses.
- ``omnigent.cli._run_agent`` for the ``-p`` one-shot path
  stops printing the assistant text on turn complete.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from tests.e2e.conftest import configure_mock_llm, reset_mock_llm
from tests.e2e.omnigent._snapshot import compare_snapshot

_PROMPT = "say hi in 5 words"

_MIN_ASSISTANT_CHARS = 4

_RUN_TIMEOUT_SEC = 60


def test_yaml_hello_world_real(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
) -> None:
    """
    ``omnigent run hello_world.yaml --harness openai-agents --model
    <model> -p <prompt>`` exits 0 and emits a non-trivial
    assistant reply using the mock LLM.
    """
    model = "mock-hello-world-model"
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "Hello there nice to meet!"}],
        key=model,
    )

    yaml_path = omnigent_repo_root / "tests" / "resources" / "examples" / "hello_world.yaml"

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
        "stderr_is_clean": result.stderr.strip() == "",
        "assistant_text": result.stdout.strip(),
    }

    diffs = compare_snapshot("test_yaml_hello_world_real", observed)
    assert diffs == [], (
        "Snapshot mismatch for hello_world.yaml run:\n"
        + "\n".join(diffs)
        + f"\n\nstdout:\n{result.stdout!r}\n\nstderr:\n{result.stderr!r}"
    )
    assert len(observed["assistant_text"]) >= _MIN_ASSISTANT_CHARS, (
        f"hello_world assistant text shorter than "
        f"{_MIN_ASSISTANT_CHARS} chars; got "
        f"{observed['assistant_text']!r}"
    )
