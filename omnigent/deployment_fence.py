"""Process-local write-fence primitives shared by server and host runtimes."""

from __future__ import annotations

import os
from pathlib import Path

FENCE_FILENAME = "deployment-write-fence"


def fence_path(data_dir: str | Path | None = None) -> Path | None:
    """Return the trusted process-local fence path, or ``None`` if unset."""
    value = str(data_dir if data_dir is not None else os.environ.get("OMNIGENT_DATA_DIR", ""))
    if not value:
        return None
    return Path(value).resolve() / FENCE_FILENAME


def write_fence_active(data_dir: str | Path | None = None) -> bool:
    """Return true for any present fence, including an unexpected symlink."""
    path = fence_path(data_dir)
    if path is None:
        return False
    try:
        return os.path.lexists(path)
    except OSError:
        # An inaccessible fence is not safe to treat as absent.
        return True


__all__ = ["FENCE_FILENAME", "fence_path", "write_fence_active"]
