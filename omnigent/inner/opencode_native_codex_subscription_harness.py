"""``harness: opencode-native-codex-subscription`` wrap for the native OpenCode server.

Subscription-backed Codex lane: routes through OpenCode's Codex
subscription provider (reachable via the user's Codex subscription /
OpenCode-authenticated path). The API-metered OpenAI / ``codex/`` /
``openai/`` paths are explicitly rejected — this lane never falls
back to them.

Today the resolver returns an empty catalog with a setup message
("Codex Subscription catalog not found" / "Codex subscription is
not configured locally") so the picker surfaces the state instead of
inventing models. The local allowlist is intentionally empty in
stage 2: no public OpenCode Codex-subscription provider prefix is
verified yet, so the executor rejects every model id at pin time.
When OpenCode exposes a verifiable local Codex-subscription catalog,
populate the ``allowed_provider_prefixes`` on the
``opencode-native-codex-subscription`` entry in
:data:`omnigent.inner._opencode_native_lane_config.OPENCODE_NATIVE_LANES`
and the executor is ready to consume it: same executor plumbing as
the free / MiniMax lanes, with a Codex-subscription provider
allowlist on the model pin.

The allowlist is the SINGLE source of truth shared with the
server-side resolver — there is no copy-paste of the membership
list, and a future edit that diverges the two fails the lane-config
contract test loudly.
"""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path
from typing import Any

from omnigent.inner._opencode_native_lane_config import (
    OpenCodeNativeLaneConfig,
    lane_for_executor_harness_id,
)
from omnigent.inner.executor import Executor
from omnigent.native_server_harness import NativeServerHarness
from omnigent.native_server_transport import NativePrompt
from omnigent.opencode_http_transport import OpenCodeHttpTransport
from omnigent.opencode_native_bridge import (
    OPENCODE_NATIVE_BRIDGE_DIR_ENV_VAR,
    OPENCODE_NATIVE_REQUEST_SESSION_ID_ENV_VAR,
    read_bridge_state,
)

# Canonical harness id, surfaced in harness error messages.
OPENCODE_NATIVE_CODEX_SUBSCRIPTION_HARNESS_ID = "opencode-native-codex-subscription"

# Local mirror of the lane's provider-prefix allowlist. The
# authoritative source is
# :func:`omnigent.inner._opencode_native_lane_config.lane_for_executor_harness_id`
# — this constant exists so the test suite can pin the
# fail-closed empty state without going through the shared config.
# The test asserts the two agree.
_OPENCODE_CODEX_SUBSCRIPTION_ALLOWED_PROVIDER_PREFIXES: frozenset[str] = frozenset(
    # Reserved for the future OpenCode Codex-subscription provider prefix.
    # The catalog resolver also gates on this same membership list, so
    # the picker and the executor stay in lockstep.
)


def _lane() -> OpenCodeNativeLaneConfig:
    """Return the shared-config lane for the Codex Subscription harness.

    :returns: The matching :class:`OpenCodeNativeLaneConfig` from the
        shared ``OPENCODE_NATIVE_LANES`` table.
    :raises RuntimeError: When the harness id is not registered in
        the shared config. This is a configuration error — the
        harness module is being imported without the shared config
        knowing about it.
    """
    lane = lane_for_executor_harness_id(OPENCODE_NATIVE_CODEX_SUBSCRIPTION_HARNESS_ID)
    if lane is None:
        raise RuntimeError(
            f"Harness id {OPENCODE_NATIVE_CODEX_SUBSCRIPTION_HARNESS_ID!r} is not "
            "registered in omnigent.inner._opencode_native_lane_config.OPENCODE_NATIVE_LANES. "
            "Add the lane config there — the executor / resolver / picker all "
            "read from the shared table, so a missing entry is a configuration bug."
        )
    return lane


class OpenCodeNativeCodexSubscriptionExecutor(NativeServerHarness):
    """
    Harness-side executor for the OpenCode-backed Codex Subscription lane.

    Mirrors :class:`omnigent.inner.opencode_native_executor.OpenCodeNativeExecutor`
    in bridge / SSE / tmux plumbing; differs in the model-pinning guard
    (rejects everything until the local catalog resolver finds a verified
    Codex-subscription provider prefix).

    No ``OPENAI_API_KEY`` is consulted here, and no OpenAI billing path
    is reachable from this executor — by construction.

    :param bridge_dir: Optional bridge directory override. ``None``
        reads :data:`OPENCODE_NATIVE_BRIDGE_DIR_ENV_VAR`.
    """

    def __init__(self, bridge_dir: Path | None = None) -> None:
        self._bridge_dir = bridge_dir or _bridge_dir_from_env()
        self._request_session_id = _request_session_id_from_env()
        super().__init__(
            harness_id=OPENCODE_NATIVE_CODEX_SUBSCRIPTION_HARNESS_ID,
            supports_enqueue=True,
            transport=OpenCodeHttpTransport(bridge_dir=self._bridge_dir),
            resolve_session_id=self._resolve_opencode_session_id,
            build_prompt=self._build_prompt_with_model_override,
        )

    def _build_prompt_with_model_override(self, content: Any) -> NativePrompt | None:
        """
        Build a prompt, pinning the resolved model only if the local
        Codex subscription provider is verified.

        Today the local catalog resolver returns an empty list with a
        "Codex Subscription not configured locally" status — so any
        ``model_override`` carried on the bridge state is rejected at
        pin time. When OpenCode ships a verifiable local Codex-
        subscription catalog, this guard becomes the same allowlist
        the resolver uses.
        """
        prompt = _content_to_native_prompt(content)
        if prompt is None or prompt.model:
            return prompt
        state = read_bridge_state(self._bridge_dir)
        model = state.model_override if state is not None else None
        if not model:
            return prompt
        if not _is_allowed_codex_subscription_model(model):
            lane = _lane()
            raise RuntimeError(
                f"Model {model!r} is not a verified Codex subscription model. "
                f"The {lane.resolver_id} lane only accepts models from the "
                "locally-verified Codex subscription catalog; OpenAI "
                "API-billed paths are explicitly rejected. Configure the local "
                "Codex subscription catalog before launching a session in this "
                "lane — see docs/omnigent-tailscale-eval.md."
            )
        return dataclasses.replace(prompt, model=model)

    async def _resolve_opencode_session_id(self) -> str | None:
        """
        Resolve the OpenCode session id from bridge state.

        :returns: The OpenCode session id when this harness may inject into
            it, else ``None``.
        """
        state = read_bridge_state(self._bridge_dir)
        if state is None:
            return None
        request_session_id = self._request_session_id
        if request_session_id is not None and state.session_id != request_session_id:
            return None
        return state.opencode_session_id


def _is_allowed_codex_subscription_model(model: str) -> bool:
    """Return True iff ``model`` lives under a verified Codex subscription prefix.

    Accepts both the bare OpenCode form (``<provider>/<model>``) and the
    fully-qualified form (``opencode/<provider>/<model>``).

    The allowlist is sourced from the shared
    :class:`OpenCodeNativeLaneConfig.allowed_provider_prefixes` (via
    :func:`_lane`) — the same membership list the server-side
    resolver uses. The local constant
    ``_OPENCODE_CODEX_SUBSCRIPTION_ALLOWED_PROVIDER_PREFIXES`` exists
    only for the test suite's belt-and-braces pin (today it's empty —
    fail closed).
    """
    lane = _lane()
    if not lane.allowed_provider_prefixes:
        # No verified prefix yet → no model can be admitted. This is the
        # intentional "fail closed" state: until a local catalog is in
        # place, the lane cannot launch any model and the resolver returns
        # an empty list with a setup message.
        return False
    bare = model[len("opencode/"):] if model.startswith("opencode/") else model
    prefix = bare.split("/", 1)[0] if "/" in bare else ""
    return prefix in lane.allowed_provider_prefixes


def _bridge_dir_from_env() -> Path:
    """
    Resolve the native OpenCode bridge directory from harness spawn env.

    :returns: Bridge directory path.
    :raises RuntimeError: If the env var is missing.
    """
    raw = os.environ.get(OPENCODE_NATIVE_BRIDGE_DIR_ENV_VAR, "").strip()
    if not raw:
        raise RuntimeError(f"{OPENCODE_NATIVE_BRIDGE_DIR_ENV_VAR} is required")
    return Path(raw)


def _content_to_native_prompt(content: Any) -> NativePrompt | None:
    """Mirror ``omnigent.inner.opencode_native_executor`` content coercion.

    Kept self-contained so the Codex Subscription lane doesn't import
    the free lane's internals beyond the executor factory contract.
    """
    if isinstance(content, str):
        return NativePrompt(text=content) if content else None
    if isinstance(content, list):
        from collections.abc import Mapping

        texts: list[str] = []
        attachments: list[Mapping[str, Any]] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type in {"input_text", "text"}:
                text = block.get("text")
                if isinstance(text, str) and text:
                    texts.append(text)
            elif block_type in {"input_image", "input_file"}:
                attachments.append(block)
        if not texts and not attachments:
            return None
        return NativePrompt(text="\n".join(texts), attachments=tuple(attachments))
    if content is None:
        return None
    import json

    return NativePrompt(text=json.dumps(content, ensure_ascii=True))


def _build_opencode_native_codex_subscription_executor() -> Executor:
    """
    Construct the native OpenCode bridge executor for the Codex
    Subscription lane.

    :returns: An :class:`OpenCodeNativeCodexSubscriptionExecutor`.
    """
    return OpenCodeNativeCodexSubscriptionExecutor()


def create_app():  # type: ignore[no-untyped-def]
    """
    Build the ``opencode-native-codex-subscription`` harness FastAPI app.

    :returns: The FastAPI app from :class:`ExecutorAdapter`.
    """
    from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter

    adapter = ExecutorAdapter(
        executor_factory=_build_opencode_native_codex_subscription_executor
    )
    return adapter.build()
