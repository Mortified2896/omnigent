"""Tests for canonical system-instruction composition."""

import json
import subprocess
from collections.abc import Generator
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from omnigent.entities import ConversationItem, FunctionCallOutputData
from omnigent.runtime.prompt import (
    TRUSTED_ROOT_ACCESS_ENV,
    TRUSTED_ROOT_INSTRUCTION,
    append_framework_instructions,
    build_instructions,
    framework_capabilities,
    history_to_input_items,
)
from omnigent.spec import AgentSpec


@pytest.fixture(autouse=True)
def _reset_framework_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[None, None, None]:
    """Keep process-lifecycle capability caching isolated between tests."""
    monkeypatch.delenv(TRUSTED_ROOT_ACCESS_ENV, raising=False)
    framework_capabilities.cache_clear()
    yield
    framework_capabilities.cache_clear()


def _output_item(output: str) -> ConversationItem:
    """Build a persisted ``function_call_output`` item for replay tests."""
    return ConversationItem(
        id="i1",
        status="completed",
        response_id="r1",
        created_at=1,
        type="function_call_output",
        data=FunctionCallOutputData(call_id="c1", output=output),
    )


def test_history_replay_strips_inline_base64_image() -> None:
    """A stored image tool result must not replay its base64 as prompt text.

    Older sessions persisted a ``Read`` of an image as a JSON list of
    ``{"type":"image","source":{"type":"base64",...}}`` blocks. Replaying that
    verbatim on resume overflows the context window and wedges compaction, so
    ``history_to_input_items`` strips the base64 to a placeholder.
    """
    huge_b64 = "iVBORw0KGgo" + "A" * 100_000
    stored = json.dumps(
        [
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": huge_b64},
            }
        ],
        separators=(",", ":"),
    )

    result = history_to_input_items([_output_item(stored)])

    output = result[0]["output"]
    assert huge_b64 not in output, "base64 image data must not be replayed as text"
    assert "image/png image omitted from history" in output
    assert "re-run the tool call" in output
    assert len(output) < 300


def test_history_replay_strips_truncated_image_block() -> None:
    """Base64 clipped at the store byte cap (invalid JSON) is still stripped.

    Real wedged sessions stored the image output truncated at the
    conversation-store byte cap, leaving the base64 string unterminated — so it
    no longer parses as JSON. The strip must fall back to an in-place rewrite,
    or the exact payloads that wedge resume would slip through unchanged.
    """
    huge_b64 = "iVBORw0KGgo" + "A" * 100_000
    # Mimic the store cap: a valid prefix cut mid-base64, no closing quote/braces.
    truncated = (
        '[{"type":"image","source":{"type":"base64","data":"'
        + huge_b64
        + "…[truncated by conversation-store: item exceeded 245760B cap]"
    )
    # Precondition: this is genuinely not parseable JSON.
    with pytest.raises(ValueError):
        json.loads(truncated)

    result = history_to_input_items([_output_item(truncated)])

    output = result[0]["output"]
    assert huge_b64 not in output, "truncated base64 must not survive replay"
    assert "image omitted from history" in output
    assert len(output) < 300


def test_history_replay_leaves_plain_text_output_unchanged() -> None:
    """Plain-text tool outputs (the common case) pass through untouched."""
    result = history_to_input_items([_output_item("TODO contents")])
    assert result[0]["output"] == "TODO contents"


def test_history_replay_leaves_non_image_json_output_unchanged() -> None:
    """A JSON tool output with no image block is returned byte-for-byte."""
    stored = json.dumps([{"type": "text", "text": "hello"}], separators=(",", ":"))
    result = history_to_input_items([_output_item(stored)])
    assert result[0]["output"] == stored


def test_framework_instructions_append_after_custom_prompts() -> None:
    spec = cast(AgentSpec, SimpleNamespace(instructions="Agent prompt", skills=[]))

    result = build_instructions(
        spec,
        "Request prompt",
        [],
        framework_instructions=("  Framework prompt  ",),
    )

    assert result == "Agent prompt\n\nRequest prompt\n\nFramework prompt"


def test_empty_framework_instructions_do_not_change_default() -> None:
    spec = cast(AgentSpec, SimpleNamespace(instructions=None, skills=[]))

    assert build_instructions(spec, None, [], framework_instructions=("", "   ")) == (
        "You are a helpful assistant."
    )


def test_framework_only_instructions_use_shared_composer() -> None:
    assert append_framework_instructions(None, ("Rename session",)) == "Rename session"


def test_trusted_root_instruction_absent_by_default() -> None:
    spec = cast(AgentSpec, SimpleNamespace(instructions="Agent prompt", skills=[]))

    assert build_instructions(spec, None, []) == "Agent prompt"


def test_trusted_root_instruction_reaches_final_system_prompt_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TRUSTED_ROOT_ACCESS_ENV, "true")
    calls: list[tuple[list[str], dict[str, object]]] = []

    def _successful_probe(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", _successful_probe)
    spec = cast(AgentSpec, SimpleNamespace(instructions="Agent prompt", skills=[]))

    result = build_instructions(spec, "Request prompt", [])
    second_result = build_instructions(spec, None, [])

    assert result == f"Agent prompt\n\nRequest prompt\n\n{TRUSTED_ROOT_INSTRUCTION}"
    assert result.count(TRUSTED_ROOT_INSTRUCTION) == 1
    assert second_result.count(TRUSTED_ROOT_INSTRUCTION) == 1
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command == ["sudo", "-n", "true"]
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["timeout"] == 2.0
    assert framework_capabilities().trusted_root_access is True
    assert "Never self-upgrade: O1 upgrades O2, O2 upgrades O1" in result


@pytest.mark.parametrize("failure", [1, subprocess.TimeoutExpired(["sudo"], 2.0)])
def test_failed_root_probe_never_advertises_root(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: int | subprocess.TimeoutExpired,
) -> None:
    monkeypatch.setenv(TRUSTED_ROOT_ACCESS_ENV, "true")

    def _failed_probe(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        if isinstance(failure, BaseException):
            raise failure
        return subprocess.CompletedProcess(["sudo", "-n", "true"], failure)

    monkeypatch.setattr(subprocess, "run", _failed_probe)
    spec = cast(AgentSpec, SimpleNamespace(instructions="Agent prompt", skills=[]))

    result = build_instructions(spec, None, [])

    assert result == "Agent prompt"
    assert TRUSTED_ROOT_INSTRUCTION not in result
    assert framework_capabilities().trusted_root_access is False
    assert TRUSTED_ROOT_ACCESS_ENV in caplog.text


def test_trusted_root_policy_is_not_duplicated_in_harness_adapters() -> None:
    repo_root = Path(__file__).parents[2]
    owning_module = repo_root / "omnigent" / "runtime" / "prompt.py"
    marker = "TRUSTED ROOT ACCESS"
    adapter_files = [
        *repo_root.glob("omnigent/inner/*harness*.py"),
        *repo_root.glob("omnigent/inner/*executor*.py"),
        *repo_root.glob("omnigent/runtime/harnesses/*.py"),
    ]

    assert marker in owning_module.read_text()
    assert all(marker not in path.read_text() for path in adapter_files)
