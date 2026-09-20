#!/usr/bin/env python3
"""Shared, dependency-free path policy for heavyweight CI workflows."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable
from pathlib import PurePosixPath

DOCS_ONLY_GLOBS = ("*.md", "**/*.md")

# GitHub workflow triggers cannot import shared data, so tests enforce each
# heavyweight event's complete, ordered filter list from this policy.
EVENT_FILTERED_HEAVY_WORKFLOWS: dict[str, dict[str, tuple[str, ...]]] = {
    "ci.yml": {
        "pull_request": ("web/**", "tests/e2e_ui/**", *DOCS_ONLY_GLOBS),
        "push": ("web/**", "tests/e2e_ui/**", *DOCS_ONLY_GLOBS),
    },
    "e2e.yml": {
        "pull_request": ("web/**", "tests/e2e_ui/**", *DOCS_ONLY_GLOBS),
    },
    "e2e-ui.yml": {"pull_request": DOCS_ONLY_GLOBS},
    "integration.yml": {
        "pull_request": ("web/**", "tests/e2e_ui/**", *DOCS_ONLY_GLOBS),
    },
    "windows.yml": {
        "pull_request": ("web/**", "tests/e2e_ui/**", *DOCS_ONLY_GLOBS),
        "push": ("web/**", "tests/e2e_ui/**", *DOCS_ONLY_GLOBS),
    },
}

# Keep directly-required named checks job-filtered: GitHub leaves an entire
# path-filtered required workflow pending instead of recording a skipped check.
JOB_FILTERED_HEAVY_WORKFLOWS = ("ui-snapshot.yml",)

ALWAYS_RUN_FOR_DOCS = ("lint.yml", "security-scan.yml")

_UI_SNAPSHOT_PREFIXES = (
    "web/",
    "tests/e2e_ui/visual/",
    ".github/actions/setup-pnpm/",
)
_UI_SNAPSHOT_EXACT = {
    "tests/e2e_ui/conftest.py",
    ".github/workflows/ui-snapshot.yml",
    "pnpm-lock.yaml",
    "pnpm-workspace.yaml",
    "pyproject.toml",
    "uv.lock",
}


def _normalized(paths: Iterable[str]) -> tuple[str, ...]:
    return tuple(path.strip() for path in paths if path.strip())


def is_documentation_path(path: str) -> bool:
    """Return whether *path* is Markdown documentation or repo guidance."""

    return any(PurePosixPath(path).match(pattern) for pattern in DOCS_ONLY_GLOBS)


def is_docs_only(paths: Iterable[str]) -> bool:
    """Return true only for a non-empty set made entirely of Markdown files."""

    normalized = _normalized(paths)
    return bool(normalized) and all(is_documentation_path(path) for path in normalized)


def affects_ui_snapshot(paths: Iterable[str]) -> bool:
    """Fail open, except that an all-Markdown change never needs a render."""

    normalized = _normalized(paths)
    if not normalized:
        return True
    if is_docs_only(normalized):
        return False
    return any(
        path in _UI_SNAPSHOT_EXACT
        or any(path.startswith(prefix) for prefix in _UI_SNAPSHOT_PREFIXES)
        for path in normalized
    )


def affects_ui_snapshot_pr_files(lines: Iterable[str], *, expected_count: int) -> bool:
    """Classify GitHub pull-files JSONL, failing open on malformed/truncated data."""

    entries: list[dict[str, object]] = []
    for line in _normalized(lines):
        try:
            entry = json.loads(line)
        except (TypeError, json.JSONDecodeError):
            return True
        if not isinstance(entry, dict):
            return True
        entries.append(entry)
    if expected_count < 0 or len(entries) != expected_count:
        return True

    paths: list[str] = []
    for entry in entries:
        filename = entry.get("filename")
        previous_filename = entry.get("previous_filename")
        if not isinstance(filename, str) or not filename:
            return True
        if previous_filename is not None and not isinstance(previous_filename, str):
            return True
        paths.append(filename)
        if previous_filename:
            paths.append(previous_filename)
    return affects_ui_snapshot(paths)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "scope",
        choices=("docs-only", "ui-snapshot", "ui-snapshot-pr-files"),
    )
    parser.add_argument("--expected-count", type=int)
    args = parser.parse_args(argv)
    lines = tuple(sys.stdin.read().splitlines())
    if args.scope == "docs-only":
        result = is_docs_only(lines)
    elif args.scope == "ui-snapshot":
        result = affects_ui_snapshot(lines)
    else:
        result = (
            True
            if args.expected_count is None
            else affects_ui_snapshot_pr_files(lines, expected_count=args.expected_count)
        )
    print(str(result).lower())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
