"""Provider-confirmed GLM model spellings used by native Codex lanes.

OmniRoute namespaces provider models in its OpenAI-compatible catalogue while
the direct Z.ai endpoint serves the provider-local spelling from a dedicated
Codex-compatible surface. Keeping the mapping here makes that transport
difference explicit without putting UI copy into either model id.

The direct surface is ``https://api.z.ai/api/v1`` — a native Responses
endpoint that Codex >= 0.154 speaks with ``wire_api = "responses"`` and no
adapter. Older Z.ai Coding-Plan Chat-Completions endpoints (and the
session-local Chat→Responses adapter PR #171 needed for them) are obsolete
for this deployment.
"""

from __future__ import annotations

from typing import Final

GLM_DIRECT_API_KEY_ENV: Final = "ZAI_API_KEY"
GLM_DIRECT_BASE_URL: Final = "https://api.z.ai/api/v1"

# Model ids served by the live direct provider catalogue (GET /api/v1/models).
# Restricted to what Z.ai actually answers on the Responses surface; the
# OmniRoute catalogue carries many more GLM generations that this lane must
# not claim.
GLM_DIRECT_MODELS: Final[frozenset[str]] = frozenset({"glm-5.3", "glm-5.3-flash"})

# The OmniRoute route ids naming the same underlying models. Only routes
# confirmed present in the live OmniRoute catalogue belong here: a direct
# lane entry is never offered on the strength of an OmniRoute row alone.
GLM_OMNIROUTE_TO_DIRECT: Final[dict[str, str]] = {
    "glm/glm-5.3": "glm-5.3",
    "glm/glm-5.3-flash": "glm-5.3-flash",
}

GLM_DISPLAY_NAMES: Final[dict[str, str]] = {
    "glm-5.3": "GLM 5.3",
    "glm-5.3-flash": "GLM 5.3 Flash",
}


def glm_display_name(model_id: str) -> str:
    """Return a stable human label while preserving the provider id."""
    return GLM_DISPLAY_NAMES.get(model_id, model_id)
