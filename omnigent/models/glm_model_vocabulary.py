"""Provider-confirmed GLM model spellings used by native Codex lanes.

OmniRoute namespaces provider models in its OpenAI-compatible catalogue while
the direct Z.ai Coding Plan endpoint uses the provider-local spelling. Keeping
the mapping here makes that transport difference explicit without putting UI
copy into either model id.
"""

from __future__ import annotations

from typing import Final

GLM_DIRECT_API_KEY_ENV: Final = "ZAI_API_KEY"
GLM_CODING_BASE_URL: Final = "https://api.z.ai/api/coding/paas/v4"

# These are the model ids returned by the live GLM provider catalogue. The
# OmniRoute ids are the exact route ids accepted by its /v1 API; the direct
# ids are the provider-local ids returned by /api/providers/{id}/models.
GLM_OMNIROUTE_TO_DIRECT: Final[dict[str, str]] = {
    "glm/glm-4.5": "glm-4.5",
    "glm/glm-4.5-air": "glm-4.5-air",
    "glm/glm-4.6": "glm-4.6",
    "glm/glm-4.7": "glm-4.7",
    "glm/glm-5": "glm-5",
    "glm/glm-5-turbo": "glm-5-turbo",
    "glm/glm-5.1": "glm-5.1",
    "glm/glm-5.2": "glm-5.2",
    "glm/glm-5.3": "glm-5.3",
    "glm/glm-5.3-flash": "glm-5.3-flash",
}
GLM_DIRECT_MODELS: Final[frozenset[str]] = frozenset(GLM_OMNIROUTE_TO_DIRECT.values())

GLM_DISPLAY_NAMES: Final[dict[str, str]] = {
    "glm-4.5": "GLM 4.5",
    "glm-4.5-air": "GLM 4.5 Air",
    "glm-4.6": "GLM 4.6",
    "glm-4.7": "GLM 4.7",
    "glm-5": "GLM 5",
    "glm-5-turbo": "GLM 5 Turbo",
    "glm-5.1": "GLM 5.1",
    "glm-5.2": "GLM 5.2",
    "glm-5.3": "GLM 5.3",
    "glm-5.3-flash": "GLM 5.3 Flash",
}


def glm_display_name(model_id: str) -> str:
    """Return a stable human label while preserving the provider id."""
    return GLM_DISPLAY_NAMES.get(model_id, model_id)
