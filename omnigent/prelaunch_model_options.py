"""Pre-launch model-option adapters for the Control Room web picker.

The upstream model catalog is the source of truth for provider resolution,
live discovery, and harness-family compatibility.  This module adds only the
small amount of presentation policy needed before a session exists: Pi support,
configured pinned-model merging, explicit default annotation, and fail-loud
semantics for an unusable catalog.

It deliberately does *not* implement another HTTP ``/v1/models`` client.
"""

from __future__ import annotations

from typing import Any

from omnigent.model_catalog import ModelListing, list_models_for_worker
from omnigent.model_override import model_family_mismatch
from omnigent.onboarding.provider_config import (
    ANTHROPIC_FAMILY,
    OPENAI_FAMILY,
    PI_SURFACE,
    ProviderEntry,
    default_provider_for_harness,
    load_config,
    surface_default_model,
)
from omnigent.spec.types import AgentSpec, ExecutorSpec


class PrelaunchModelOptionsError(RuntimeError):
    """The selected host cannot provide a truthful pre-launch model catalog."""


def _prelaunch_spec(harness: str) -> AgentSpec:
    """Build the minimal unpinned spec used for pre-launch provider resolution."""
    return AgentSpec(
        spec_version=1,
        name=f"{harness}-prelaunch",
        executor=ExecutorSpec(
            type="omnigent",
            config={"harness": harness},
        ),
    )


def _configured_pi_provider() -> ProviderEntry | None:
    """Return the explicitly configured Pi default, without inventing a fallback.

    ``list_models_for_worker`` remains authoritative for what actually resolves
    on the machine (including ambient detection).  We consult the explicit
    config only for presentation metadata that cannot be reconstructed from a
    ``ModelListing``: pinned ids and ``models.default``.
    """
    return default_provider_for_harness(load_config(), PI_SURFACE)


def _pinned_pi_models(provider: ProviderEntry | None) -> tuple[str, ...]:
    """Return configured Anthropic/OpenAI model ids in stable, deduplicated order."""
    if provider is None:
        return ()
    seen: set[str] = set()
    pinned: list[str] = []
    for family_name in (ANTHROPIC_FAMILY, OPENAI_FAMILY):
        family = provider.families.get(family_name)
        if family is None:
            continue
        for model_id in family.models.values():
            if (
                isinstance(model_id, str)
                and model_id
                and model_id not in seen
                and model_family_mismatch("pi-native", model_id) is None
            ):
                seen.add(model_id)
                pinned.append(model_id)
    return tuple(pinned)


def _render_options(
    listing: ModelListing,
    *,
    pinned: tuple[str, ...] = (),
    default_model: str | None = None,
) -> list[dict[str, Any]]:
    """Render a verified upstream listing as the host-frame picker row shape."""
    if listing.source == "none":
        raise PrelaunchModelOptionsError(listing.note or "no model provider resolved")
    if not listing.models:
        # Pinned ids must never disguise a broken/empty live catalog.  The user
        # should see the provider failure instead of selecting a model that was
        # never verified against the endpoint.
        raise PrelaunchModelOptionsError(
            listing.note or "the resolved provider returned no compatible models"
        )

    ordered: list[str] = []
    display_names: dict[str, str] = {}
    seen: set[str] = set()
    for model in listing.models:
        if model.id in seen:
            continue
        seen.add(model.id)
        ordered.append(model.id)
        display_names[model.id] = model.id
    for model_id in pinned:
        if model_id not in seen:
            seen.add(model_id)
            ordered.append(model_id)
            display_names[model_id] = model_id

    return [
        {
            "id": model_id,
            "displayName": display_names[model_id],
            **({"isDefault": True} if default_model == model_id else {}),
        }
        for model_id in ordered
    ]


def pi_native_prelaunch_model_options() -> list[dict[str, Any]]:
    """Return truthful pre-launch model choices for ``pi-native``.

    Provider resolution, live ``/v1/models`` discovery, and compatibility
    filtering are delegated to upstream :func:`list_models_for_worker`.  The
    configured Pi provider contributes only its pinned ids and explicit
    ``models.default`` annotation.  No first-row-as-default heuristic is used.

    :raises PrelaunchModelOptionsError: If provider resolution/catalog discovery
        yields no usable models.
    """
    spec = _prelaunch_spec("pi-native")
    listing = list_models_for_worker(spec, "pi-native")
    provider = _configured_pi_provider()
    default_model = surface_default_model(provider, PI_SURFACE) if provider is not None else None
    return _render_options(
        listing,
        pinned=_pinned_pi_models(provider),
        default_model=default_model,
    )
