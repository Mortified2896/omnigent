"""``harness: opencode-native-minimax-token-plan`` wrap for the native OpenCode server.

Subscription-backed MiniMax Token Plan lane: routes through OpenCode's
built-in ``minimax-coding-plan/`` and ``minimax-cn-coding-plan/``
providers (Token Plan / subscription only). The API-metered
``minimax/`` and ``minimax-cn/`` prefixes are explicitly rejected by
the model-options resolver and by this executor's prompt-pinning
step — so neither a buggy catalog nor a stale stored model pick can
silently substitute an API-billed MiniMax id.

The executor is the same ``OpenCodeNativeExecutor`` as the free lane
(``opencode-native``); only the model-prefix allowlist differs. Reusing
the executor keeps the bridge / SSE forwarder / tmux plumbing shared
so the runner doesn't fork the runtime for a provider-only variant.

The allowlist itself is sourced from the shared
:class:`omnigent.inner._opencode_native_lane_config.OPENCODE_NATIVE_LANES`
table — the single source of truth shared with the server-side
resolver, the sync / verify scripts, and any future OpenCode-backed
subscription lane. The hard-coded mirror
``_MINIMAX_TOKEN_PLAN_ALLOWED_PROVIDER_PREFIXES`` below is kept as a
belt-and-braces pin for the test suite (the test asserts both the
local constant and the shared-config entry agree, so a future edit
that diverges the two fails loudly).
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
from omnigent.inner.opencode_native_executor import OpenCodeNativeExecutor
from omnigent.native_server_harness import NativeServerHarness
from omnigent.native_server_transport import NativePrompt
from omnigent.opencode_http_transport import OpenCodeHttpTransport
from omnigent.opencode_native_bridge import (
    OPENCODE_NATIVE_BRIDGE_DIR_ENV_VAR,
    OPENCODE_NATIVE_REQUEST_SESSION_ID_ENV_VAR,
    read_bridge_state,
)

# Canonical harness id, surfaced in harness error messages.
OPENCODE_NATIVE_MINIMAX_TOKEN_PLAN_HARNESS_ID = "opencode-native-minimax-token-plan"

# Local mirror of the lane's provider-prefix allowlist. The
# authoritative source is
# :func:`omnigent.inner._opencode_native_lane_config.lane_for_executor_harness_id`
# — this constant exists so the test suite can pin the contract
# without going through the shared config. The test asserts the
# two agree.
_MINIMAX_TOKEN_PLAN_ALLOWED_PROVIDER_PREFIXES: frozenset[str] = frozenset(
    {"minimax-coding-plan", "minimax-cn-coding-plan"}
)


def _lane() -> OpenCodeNativeLaneConfig:
    """Return the shared-config lane for the MiniMax Token Plan harness.

    :returns: The matching :class:`OpenCodeNativeLaneConfig` from the
        shared ``OPENCODE_NATIVE_LANES`` table.
    :raises RuntimeError: When the harness id is not registered in the
        shared config. This is a configuration error — the harness
        module is being imported without the shared config knowing
        about it.
    """
    lane = lane_for_executor_harness_id(OPENCODE_NATIVE_MINIMAX_TOKEN_PLAN_HARNESS_ID)
    if lane is None:
        raise RuntimeError(
            f"Harness id {OPENCODE_NATIVE_MINIMAX_TOKEN_PLAN_HARNESS_ID!r} is not "
            "registered in omnigent.inner._opencode_native_lane_config.OPENCODE_NATIVE_LANES. "
            "Add the lane config there — the executor / resolver / picker all "
            "read from the shared table, so a missing entry is a configuration bug."
        )
    return lane


class OpenCodeNativeMinimaxTokenPlanExecutor(NativeServerHarness):
    """
    Harness-side executor for the OpenCode-backed MiniMax Token Plan lane.

    Differs from :class:`OpenCodeNativeExecutor` only in the
    model-pinning guard: a resolved ``model_override`` must live under
    one of the allowed Token Plan provider prefixes
    (``minimax-coding-plan/`` or ``minimax-cn-coding-plan/``). Anything
    else raises before the prompt is sent so the runner never reaches
    the OpenCode bridge with an out-of-lane model.

    The allowlist is sourced from the shared
    ``OPENCODE_NATIVE_LANES`` table (via :func:`_lane`), so the
    executor and the catalog resolver always agree — there is no
    risk of one drifting from the other.

    :param bridge_dir: Optional bridge directory override. ``None``
        reads :data:`OPENCODE_NATIVE_BRIDGE_DIR_ENV_VAR`.
    """

    def __init__(self, bridge_dir: Path | None = None) -> None:
        self._bridge_dir = bridge_dir or _bridge_dir_from_env()
        self._request_session_id = _request_session_id_from_env()
        super().__init__(
            harness_id=OPENCODE_NATIVE_MINIMAX_TOKEN_PLAN_HARNESS_ID,
            supports_enqueue=True,
            transport=OpenCodeHttpTransport(bridge_dir=self._bridge_dir),
            resolve_session_id=self._resolve_opencode_session_id,
            build_prompt=self._build_prompt_with_model_override,
        )

    def _build_prompt_with_model_override(self, content: Any) -> NativePrompt | None:
        """
        Build a prompt, pinning the resolved model after validating the
        provider prefix is Token Plan-only.

        Raises if ``state.model_override`` is set but its provider
        prefix is not in the Token Plan allowlist — this is the
        defense-in-depth layer that backs the model-options resolver.
        An API-metered ``minimax/...`` model id MUST NOT reach the
        OpenCode bridge from this lane, even if the catalog reader
        were ever compromised.
        """
        prompt = _content_to_native_prompt(content)
        if prompt is None or prompt.model:
            return prompt
        state = read_bridge_state(self._bridge_dir)
        model = state.model_override if state is not None else None
        if not model:
            return prompt
        if not _is_allowed_token_plan_model(model):
            lane = _lane()
            raise RuntimeError(
                f"Model {model!r} is not a MiniMax Token Plan model. The "
                f"{lane.resolver_id} lane only accepts models under the "
                f"following OpenCode provider prefixes: "
                f"{sorted(lane.allowed_provider_prefixes)}. API-metered "
                "minimax/ or minimax-cn/ ids are explicitly rejected — "
                "never substituted as a fallback."
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


def _is_allowed_token_plan_model(model: str) -> bool:
    """Return True iff ``model`` is a Token Plan provider id.

    Accepts both the bare OpenCode form (``<provider>/<model>``) and the
    fully-qualified form (``opencode/<provider>/<model>``).

    The allowlist is sourced from the shared
    :class:`OpenCodeNativeLaneConfig.allowed_provider_prefixes` (via
    :func:`_lane`) — the same membership list the server-side
    resolver uses. The local constant
    ``_MINIMAX_TOKEN_PLAN_ALLOWED_PROVIDER_PREFIXES`` exists only for
    the test suite's belt-and-braces pin.
    """
    lane = _lane()
    if not lane.allowed_provider_prefixes:
        # No verified prefix yet → no model can be admitted. This
        # matches the "fail closed" stance the resolver takes.
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

    Keeps the executor self-contained so the Token Plan lane doesn't
    import the free lane's internals beyond the executor factory.
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


def _build_opencode_native_minimax_token_plan_executor() -> Executor:
    """
    Construct the native OpenCode bridge executor for the MiniMax Token
    Plan lane.

    :returns: An :class:`OpenCodeNativeMinimaxTokenPlanExecutor`.
    """
    return OpenCodeNativeMinimaxTokenPlanExecutor()


def create_app():  # type: ignore[no-untyped-def]
    """
    Build the ``opencode-native-minimax-token-plan`` harness FastAPI app.

    :returns: The FastAPI app from :class:`ExecutorAdapter`.
    """
    from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter

    adapter = ExecutorAdapter(executor_factory=_build_opencode_native_minimax_token_plan_executor)
    return adapter.build()