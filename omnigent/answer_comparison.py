"""Bounded text-only prompt contract shared by the host and comparison API."""

from __future__ import annotations

import json
import re
from typing import Any, Literal

MAX_TASK_CHARS = 24_000
MAX_ANSWER_CHARS = 32_000
CALL_TIMEOUT = 100.0
Vote = Literal["a", "b", "tie", "neither", "skip"]

# Deliberately conservative: unknown/action-oriented requests keep the ordinary
# execution path. The host ALSO disables every tool at the transport boundary.
_ACTION = re.compile(
    r"```|\b(code|coding|program(?:ming)?|implement|debug|refactor|deploy|commit|"
    r"repository|worktree|terminal|shell|script|sql|api|install|configure|"
    r"spreadsheet|workbook|attachment|upload|download|browse|search|"
    r"send|delete|remove|restart|purchase|book|schedule|remind|"
    r"today|latest|current|live|weather)\b",
    re.IGNORECASE,
)
_TEXT_TASK = re.compile(
    r"\b(explain|describe|compare|difference|why|what|how|write|rewrite|draft|"
    r"translate|summari[sz]e|brainstorm|suggest|idea|advice|think|opinion|"
    r"story|poem|meaning|calculate|solve|proof|discuss|possible)\b",
    re.IGNORECASE,
)


def text_task_eligible(task: str) -> bool:
    return bool(
        task.strip()
        and len(task) <= MAX_TASK_CHARS
        and not _ACTION.search(task)
        and _TEXT_TASK.search(task)
    )


def answer_request(task: str) -> dict[str, Any]:
    return {"purpose": "answer-comparison", "task": task}


def comparison_prompt(request: dict[str, Any]) -> str:
    task = request.get("task")
    if (
        request.get("purpose") != "answer-comparison"
        or set(request) != {"purpose", "task"}
        or not isinstance(task, str)
        or not text_task_eligible(task)
    ):
        raise ValueError("Invalid text comparison request")
    return (
        "Answer the user's task below as a standalone text reply. Do not use tools. "
        "If answering requires coding, file access, browsing, external actions, or information "
        'you cannot establish without tools, return eligible=false and answer="". '
        "Otherwise return eligible=true with the full answer. Return only the JSON object "
        "required by the output schema.\n\nUser task (JSON string):\n" + json.dumps(task)
    )


ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "eligible": {"type": "boolean"},
        "answer": {"type": "string", "maxLength": MAX_ANSWER_CHARS},
    },
    "required": ["eligible", "answer"],
    "additionalProperties": False,
}


def parse_answer(raw: str) -> str | None:
    if len(raw) > MAX_ANSWER_CHARS * 8:
        raise ValueError("Comparison output exceeded its limit")
    payload = json.loads(raw)
    if (
        not isinstance(payload, dict)
        or set(payload) != {"eligible", "answer"}
        or type(payload["eligible"]) is not bool
        or not isinstance(payload["answer"], str)
        or len(payload["answer"]) > MAX_ANSWER_CHARS
    ):
        raise ValueError("Update the connected host to support text answer comparisons")
    if not payload["eligible"]:
        return None
    if not payload["answer"].strip():
        raise ValueError("Comparison produced an empty answer")
    return payload["answer"].strip()
