"""Materialize the local tool-free agent without native wrapper presentation."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import yaml

AGENT_NAME = "local-tool-free"


def ensure_agent(agent_store: Any, artifact_store: Any, agent_cache: Any) -> None:
    from omnigent.server.app import _ensure_builtin_agent, _tar_gz_dir
    from omnigent.spec import materialize_bundle

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "local-tool-free.yaml"
        source.write_text(
            yaml.safe_dump(
                {
                    "name": AGENT_NAME,
                    "prompt": "Answer the approved task using only the supplied context. "
                    "No external tools are available. If external access is needed, explain that "
                    "a new tool-capable routing review is required.",
                    "executor": {"harness": "local-tool-free"},
                    "spawn": False,
                    "tools": {},
                }
            )
        )
        bundle = materialize_bundle(source, root / "bundle")
        _ensure_builtin_agent(
            agent_store,
            artifact_store,
            agent_cache,
            name=AGENT_NAME,
            bundle_bytes=_tar_gz_dir(bundle),
        )
