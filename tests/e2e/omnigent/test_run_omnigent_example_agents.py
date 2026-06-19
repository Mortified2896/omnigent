"""End-to-end: example YAMLs boot and execute under ``omnigent run`` (mock LLM).

Migrated to mock LLM: each parametrized case configures canned
responses so the test is deterministic.

**What breaks if a case here fails:**
- The adapter stops translating a previously-working concept.
- The Omnigent mode CLI shim loses a dispatch for a harness.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.e2e.conftest import configure_mock_llm, reset_mock_llm

_ONESHOT_TIMEOUT_SEC = 60


_CASES = [
    pytest.param(
        "tests/resources/examples/hello_world.yaml",
        "Reply with exactly the text 'hello_world_probe'.",
        ("hello_world_probe",),
        (),
        ("--model", "mock-example-hello", "--harness", "openai-agents"),
        [{"text": "hello_world_probe"}],
        "mock-example-hello",
        id="hello_world",
    ),
    pytest.param(
        "tests/resources/examples/agent_with_tools.yaml",
        (
            "Call the calculate tool to compute 6 * 9, then reply "
            "with exactly 'answer=54' and nothing else."
        ),
        ("answer=54", "answer = 54", "answer:54"),
        (),
        (),
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_calc_ex",
                        "name": "calculate",
                        "arguments": '{"expression": "6 * 9"}',
                    }
                ],
            },
            {"text": "answer=54"},
        ],
        # agent_with_tools.yaml has its own executor.model; use default queue
        None,
        id="agent_with_tools_calculate",
    ),
]


@pytest.mark.parametrize(
    "yaml_rel,prompt,success_markers,forbidden_markers,extra_args,mock_responses,mock_key",
    _CASES,
)
def test_run_omnigent_example_yaml(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
    yaml_rel: str,
    prompt: str,
    success_markers: tuple[str, ...],
    forbidden_markers: tuple[str, ...],
    extra_args: tuple[str, ...],
    mock_responses: list[dict],
    mock_key: str | None,
) -> None:
    """
    Drive one example YAML under ``omnigent run -p <prompt>``
    with mock LLM responses.
    """
    yaml_path = omnigent_repo_root / yaml_rel
    assert yaml_path.exists(), f"Fixture missing: {yaml_path}"

    reset_mock_llm(mock_llm_server_url)
    if mock_key is not None:
        configure_mock_llm(mock_llm_server_url, mock_responses, key=mock_key)
    else:
        # Use default queue for agents with their own executor.model
        configure_mock_llm(mock_llm_server_url, mock_responses)

    args = [
        str(omnigent_python),
        "-m",
        "omnigent",
        "run",
        str(yaml_path),
        "--no-session",
        *extra_args,
        "-p",
        prompt,
    ]
    result = subprocess.run(
        args,
        env=mock_credentials_env,
        cwd=str(omnigent_repo_root),
        capture_output=True,
        text=True,
        timeout=_ONESHOT_TIMEOUT_SEC,
    )

    combined = result.stdout + result.stderr

    for marker in forbidden_markers:
        assert marker not in combined, (
            f"{yaml_rel}: forbidden marker {marker!r} appeared in "
            f"output.\nstderr tail:\n{result.stderr[-1500:]}"
        )

    assert result.returncode == 0, (
        f"{yaml_rel}: exited {result.returncode}. "
        f"stderr tail:\n{result.stderr[-2000:]}\n"
        f"stdout tail:\n{result.stdout[-1500:]}"
    )

    hits = [m for m in success_markers if m.lower() in result.stdout.lower()]
    assert hits, (
        f"{yaml_rel}: none of the success markers "
        f"{success_markers!r} appeared in stdout. "
        f"stdout tail:\n{result.stdout[-2500:]}"
    )
