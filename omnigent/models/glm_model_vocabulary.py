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

Two vocabularies live here, deliberately of different widths:

- :data:`GLM_DIRECT_MODELS` is Direct *eligibility*: the models this
  deployment has confirmed on Z.ai's native Responses surface. It stays
  narrow; a generation the direct endpoint has not confirmed is never
  offered there.
- :data:`GLM_OMNIROUTE_ROUTES` is gateway route/display *knowledge*: every
  GLM route id OmniRoute has carried, including generations that are
  OmniRoute-only here. Qualifying a route on the gateway lane requires only
  that the live gateway serves it — it never implies Direct support, and a
  gateway-only generation must not require invented native-Direct support.

The two sets are therefore not required to be equal, and nothing may
re-couple them.
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

# Every GLM route id the OmniRoute gateway has carried, mapped to the
# provider-local id of the same underlying model, or ``None`` when the
# generation is OmniRoute-only in this deployment. Only routes confirmed
# present in the live OmniRoute catalogue are ever surfaced; this map is the
# filter and display vocabulary for that lane, not a Direct eligibility list.
GLM_OMNIROUTE_ROUTES: Final[dict[str, str | None]] = {
    "glm/glm-4.5": None,
    "glm/glm-4.5-air": None,
    "glm/glm-4.6": None,
    "glm/glm-4.7": None,
    "glm/glm-5": None,
    "glm/glm-5-turbo": None,
    "glm/glm-5.1": None,
    "glm/glm-5.2": None,
    "glm/glm-5.3": "glm-5.3",
    "glm/glm-5.3-flash": "glm-5.3-flash",
}

# The subset of gateway routes naming a model the direct lane also serves.
# The direct lane's resolver accepts the route spelling as an alias for its
# provider-local id; gateway-only routes must never resolve on that lane.
GLM_OMNIROUTE_TO_DIRECT: Final[dict[str, str]] = {
    route_id: direct_id
    for route_id, direct_id in GLM_OMNIROUTE_ROUTES.items()
    if direct_id is not None
}

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
