"""SQLAlchemy table definitions for the omnigent database."""

from __future__ import annotations

import contextlib
import hashlib
from collections.abc import Iterator
from contextvars import ContextVar
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Float,
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    false,
    text,
    true,
)
from sqlalchemy.dialects.mysql import BINARY as MySQLBinary
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from omnigent.db.compression import CompressedText

# 32-byte sha256 digest column. LargeBinary → BYTEA (Postgres) / BLOB (SQLite),
# but MySQL cannot index a BLOB without a key-prefix length, so use fixed-length
# BINARY(32) there — an exact fit for the digest and fully indexable.
_CKSUM32 = LargeBinary(32).with_variant(MySQLBinary(32), "mysql")


class Base(DeclarativeBase):
    """Shared declarative base for all omnigent tables."""


# Default workspace id stamped on every row and used as the leading
# member of every composite primary key. 0 is the single-workspace /
# unassigned sentinel: with no workspace bound to the request, all rows
# live in workspace 0.
DEFAULT_WORKSPACE_ID = 0

# Ambient per-request workspace id. Stores are process-wide singletons, so
# the active workspace can't ride on the store instance — it lives here.
# OSS leaves this at the default (single-workspace 0); a multi-tenant
# deployment (e.g. universe) sets it per request from the authenticated
# context (via ``workspace_scope`` in middleware). Reads and inserts
# resolve it through ``current_workspace_id()`` so the same store code
# scopes to the caller's workspace without threading the id through every
# signature — keeping this file byte-identical across deployments.
_current_workspace_id: ContextVar[int] = ContextVar(
    "omnigent_workspace_id", default=DEFAULT_WORKSPACE_ID
)


def current_workspace_id() -> int:
    """Return the workspace id bound to the active request/context.

    Defaults to :data:`DEFAULT_WORKSPACE_ID` (0) — the single-workspace OSS
    deployment. Multi-tenant deployments set it per request so every
    primary-key lookup, filter, and insert scopes to that workspace.
    """
    return _current_workspace_id.get()


@contextlib.contextmanager
def workspace_scope(workspace_id: int) -> Iterator[None]:
    """Bind *workspace_id* for the duration of the ``with`` block.

    Used by multi-tenant request middleware (and tests) to scope all
    store access to one workspace; resets to the prior value on exit so
    nested / concurrent contexts don't leak.
    """
    token = _current_workspace_id.set(workspace_id)
    try:
        yield
    finally:
        _current_workspace_id.reset(token)


AGENT_KIND_TEMPLATE = "template"
AGENT_KIND_SESSION = "session"

POLICY_SCOPE_DEFAULT = "default"
POLICY_SCOPE_SESSION = "session"


class SqlAgent(Base):
    """
    SQLAlchemy model for the ``agents`` table.

    Each row represents a registered agent in the system.

    :param id: Unique agent identifier, e.g. ``"ag_0f1a2b3c..."``.
    :param created_at: Unix epoch seconds when the agent was created.
    :param name: Human-readable agent name. Registered template
        agents require unique names; session-scoped copies may reuse
        the same name across different sessions.
    :param bundle_location: Artifact store key for the current bundle.
        Content-addressed (SHA-256 hex), e.g.
        ``"ag_abc123/a1b2c3d4e5f6..."``.
    :param version: Monotonic version counter. Starts at 1, incremented
        on each update via ``PUT /api/agents/{id}``.
    :param kind: ``"template"`` for server-wide registered agents;
        ``"session"`` for per-conversation copies.
    :param description: Optional free-text description of the agent's
        purpose. ``None`` when not provided.
    :param updated_at: Unix epoch seconds of the last update, or
        ``None`` if the agent has never been updated.
    """

    __tablename__ = "agents"

    # Tenant partition key: Databricks workspace id owning this row (0 = default). Part of the PK.
    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(256))
    bundle_location: Mapped[str] = mapped_column(String(512))
    version: Mapped[int] = mapped_column(Integer, default=1)
    # Enum stored as a stable int code (see omnigent.db.enum_codecs
    # AGENT_KIND: template=1, session=2). The store converts to/from the
    # string name at the row↔entity boundary.
    kind: Mapped[int] = mapped_column(SmallInteger)
    description: Mapped[str | None] = mapped_column(CompressedText, nullable=True)
    updated_at: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        CheckConstraint("kind IN (1, 2)", name="ck_agents_kind"),
        Index("ix_agents_created_at", "workspace_id", "created_at", "id"),
        # Template agents have unique names; session-scoped agents (kind=2)
        # may reuse the same name. That "unique only within the template set"
        # rule can't be a partial unique index (MySQL has none), so it is
        # enforced in the store (SqlAlchemyAgentStore.create). This plain index
        # backs the (workspace_id, name, kind) lookup that check and get_by_name
        # do — kind is included so the seek skips same-named session copies
        # straight to the template row.
        Index("ix_agents_name", "workspace_id", "name", "kind", "id"),
    )


class SqlFile(Base):
    """
    SQLAlchemy model for the ``files`` table.

    Each row represents an uploaded file tracked by the system.

    :param id: Unique file identifier, e.g. ``"file_a1b2c3d4..."``.
    :param created_at: Unix epoch seconds when the file record was
        created.
    :param filename: Original filename as provided by the uploader,
        max 512 characters. e.g. ``"report.pdf"``.
    :param bytes: Size of the file in bytes.
    :param content_type: MIME type of the file, e.g.
        ``"application/pdf"``. ``None`` when not provided.
    """

    __tablename__ = "files"

    # Tenant partition key: Databricks workspace id owning this row (0 = default). Part of the PK.
    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[int] = mapped_column(Integer)
    filename: Mapped[str] = mapped_column(String(512))
    bytes: Mapped[int] = mapped_column(Integer)
    content_type: Mapped[str | None] = mapped_column(String(256), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        Index("ix_files_created_at", "workspace_id", "created_at", "id"),
        Index(
            "ix_files_session_id_created_at",
            "workspace_id",
            "session_id",
            "created_at",
            "id",
        ),
    )


class SqlUser(Base):
    """
    SQLAlchemy model for the ``users`` table.

    Each row represents a user. In header / OIDC modes, ``id`` is
    the upstream identity (email or ``"local"``); the row is
    upserted on first sight and ``password_hash`` stays ``NULL``.
    In ``accounts`` mode, rows are created explicitly by the admin
    or via invite redemption with a populated ``password_hash``.

    :param id: User identifier — email in header/OIDC modes, chosen
        username in accounts mode, ``"local"`` in single-user.
    :param is_admin: When ``True``, the user bypasses all
        permission checks. ``False`` by default.
    :param password_hash: argon2id hash of the user's password.
        ``NULL`` for users created via header/OIDC modes (their
        password is the upstream IdP's).
    :param created_at: Unix epoch seconds when the row was inserted.
        Populated for accounts-mode users; ``NULL`` for legacy rows
        backfilled by the original permissions migration.
    :param last_login_at: Unix epoch seconds of the most recent
        successful ``/auth/login`` (accounts mode). ``NULL`` until
        the first login.
    """

    __tablename__ = "users"

    # Tenant partition key: Databricks workspace id owning this row (0 = default). Part of the PK.
    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    password_hash: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_login_at: Mapped[int | None] = mapped_column(Integer, nullable=True)


class SqlAccountToken(Base):
    """
    SQLAlchemy model for the ``account_tokens`` table.

    Backs both invite tokens (admin-issued, allow self-serve
    registration) and magic-login tokens (CLI-minted, hand off a
    signed-in session into the web UI). Both have the same
    short-TTL single-use lifecycle, so they share one table.

    :param id: Opaque random token string (43+ URL-safe base64
        chars). This is the secret — the user presents it as a
        query param. Stored verbatim because we need
        constant-time lookup; rotation = delete + recreate.
    :param kind: ``"invite"`` (anyone can redeem; creates a new
        user) or ``"magic"`` (the bound ``user_id`` is signed in).
    :param user_id: For ``magic``, the user the token signs in as.
        For ``invite``, ``NULL`` (the username is chosen at
        redemption time).
    :param created_by: User id of the admin who issued an invite
        (``NULL`` for magic tokens, which are self-issued).
    :param created_at: Unix epoch seconds when the token was
        minted. ``expires_at = created_at + ttl_seconds``.
    :param expires_at: Unix epoch seconds when the token stops
        being redeemable. Single-use enforcement is via
        ``redeemed_at``, this just bounds the window.
    :param redeemed_at: Unix epoch seconds when the token was
        consumed. ``NULL`` until then. After being set, the token
        is dead — redeem checks this column atomically.
    :param invited_is_admin: For invite tokens, whether the
        resulting user should be created with admin rights. False
        for magic tokens.
    """

    __tablename__ = "account_tokens"

    # Tenant partition key: Databricks workspace id owning this row (0 = default). Part of the PK.
    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    # Enum stored as a stable int code (see omnigent.db.enum_codecs
    # ACCOUNT_TOKEN_KIND: invite=1, magic=2). The store converts to/from
    # the string name at the row↔entity boundary.
    kind: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[int] = mapped_column(Integer, nullable=False)
    redeemed_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    invited_is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())

    __table_args__ = (
        CheckConstraint("kind IN (1, 2)", name="ck_account_tokens_kind"),
        Index("ix_account_tokens_expires_at", "workspace_id", "expires_at", "id"),
    )


class SqlSessionPermission(Base):
    """
    SQLAlchemy model for the ``session_permissions`` table.

    Junction table mapping ``(user_id, conversation_id)`` to a
    numeric permission level. PK is ``(user_id, conversation_id)``
    — optimized for the hot path ("list sessions I can access"
    = prefix scan on ``user_id``).

    The ``"__public__"`` sentinel ``user_id`` represents public
    read access to a session.

    :param user_id: The grantee, e.g. ``"alice@example.com"``
        or ``"__public__"`` for public access.
    :param conversation_id: The session being shared, e.g.
        ``"conv_e4f5a6b7..."``.
    :param level: Numeric permission level: ``1`` = read,
        ``2`` = edit, ``3`` = manage. Each level subsumes the
        ones below it (comparison is ``>=``).
    """

    __tablename__ = "session_permissions"

    # Tenant partition key: Databricks workspace id owning this row (0 = default). Part of the PK.
    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    user_id: Mapped[str] = mapped_column(
        String(128),
        primary_key=True,
    )
    conversation_id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
    )
    level: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        CheckConstraint("level IN (1, 2, 3, 4)", name="ck_session_permissions_level"),
        # Lookups by conversation (get_session_owner) filter workspace_id +
        # conversation_id; user_id trails to complete the PK.
        Index(
            "ix_session_permissions_conversation_id",
            "workspace_id",
            "conversation_id",
            "user_id",
        ),
    )


class SqlConversation(Base):
    """
    SQLAlchemy model for the ``conversations`` table.

    Each row represents a conversation thread that contains one or
    more conversation items.

    :param id: Unique conversation identifier, e.g.
        ``"conv_e4f5a6b7..."``.
    :param created_at: Unix epoch seconds when the conversation was
        created.
    :param updated_at: Unix epoch seconds when the conversation was
        last updated (item append, title change, etc.).
    :param title: Human-readable title; empty string when untitled.
    :param kind: Conversation type. ``"default"`` for user-initiated,
        ``"sub_agent"`` for sub-agent execution conversations.
    :param parent_conversation_id: For Phase 4 named sub-agents,
        points at the parent conversation. ``None`` for top-level
        conversations. ``ON DELETE CASCADE`` so removing a parent
        cleans up the entire sub-tree.
    :param root_conversation_id: Id of the root (top-level)
        conversation in the spawn tree. Equal to ``id`` for
        top-level conversations. Indexed so ``sys_session_get_history`` /
        ``sys_session_close`` can verify that a target
        ``conversation_id`` lives in the caller's tree in O(1) —
        any agent in the tree can address any other by
        ``conversation_id``. ``ON DELETE CASCADE`` to keep it
        consistent with ``parent_conversation_id`` when a root is
        deleted.
    :param agent_id: Foreign key to the agent bound to this
        conversation at creation time. ``None`` for legacy
        conversations created without an agent binding (these are
        excluded from ``GET /v1/sessions`` results).
    :param runner_id: Runner the conversation is pinned to (hard
        affinity per ``designs/RUNNER.md`` §5). ``None`` until the
        first dispatch claims a runner; thereafter every subsequent
        dispatch routes to this runner while it is online (or fails
        with ``runner_unavailable`` if it isn't). No FK because
        runner records are not persisted in v1 — the registry is
        purely in-memory.
    :param external_session_id: Runtime-native session id this
        conversation wraps, e.g. Claude Code's session uuid for
        ``omnigent claude`` sessions. ``None`` for regular
        AP-only conversations. Populated by the wrapper bridge
        from the underlying runtime and used by ``--resume`` to
        recover the external session's prior transcript. Generic
        across runtimes — at most one external session per
        conversation. No FK because the id is generated externally
        (by Claude Code, Codex, Pi, etc.) and is not tracked in
        any AP-side table.
    :param workspace: Absolute path on disk where the runner should
        start, e.g. ``"/Users/corey/universe/src/foo"``. Required
        when ``host_id`` is set (enforced by check constraint
        ``ck_conversations_workspace_required_for_host``); optional
        for CLI-launched sessions that record their starting cwd
        for display. Stored as the canonicalized realpath returned
        by ``host.stat`` at session-create time; runtime symlinks
        are pre-resolved so the boundary check on the agent's
        ``os_env.cwd`` cannot be smuggled past via a symlink.
        Immutable after creation —
        designs/SESSION_WORKSPACE_SELECTION.md. When a git worktree
        was created for the session, this is the worktree directory
        path rather than the picked source repo.
    :param git_branch: Git branch checked out in the session's
        worktree, e.g. ``"feature/login"``. Set only when the
        session was created with a server-created git worktree;
        ``None`` otherwise. ``git_branch IS NOT NULL`` gates worktree
        cleanup on session delete. See
        designs/SESSION_GIT_WORKTREE.md.
    :param archived: Whether the session is archived. Archived
        sessions are hidden from the default ``GET /v1/sessions``
        listing (and the sidebar); the listing returns them only when
        ``include_archived=True``. ``False`` for normal sessions.
        Reversible via ``PATCH /v1/sessions/{id}``.
    """

    __tablename__ = "conversations"

    # Tenant partition key: Databricks workspace id owning this row (0 = default). Part of the PK.
    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(768), nullable=False, server_default="")
    # Enum stored as a stable int code (see omnigent.db.enum_codecs
    # CONVERSATION_KIND: default=1, sub_agent=2). The store converts to/from
    # the string name at the row↔entity boundary.
    kind: Mapped[int] = mapped_column(SmallInteger, default=1)
    parent_conversation_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    root_conversation_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    agent_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    runner_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Host that launched (or should launch) the runner for this
    # session. Set when a session is created via the Web UI on a
    # specific host. No FK: host records are managed outside this
    # table; deletion is handled explicitly by the application.
    host_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    # Per-session reasoning-effort hint, e.g. "high". Nullable;
    # None means use the agent default.
    reasoning_effort: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Per-session LLM model override, e.g. "claude-opus-4-7". Nullable;
    # None means use the agent default from the spec.
    model_override: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Per-session cost-control switch: "on" | "off". Nullable; None
    # means use the spec default (see entities.Conversation).
    cost_control_mode_override: Mapped[str | None] = mapped_column(String(8), nullable=True)
    # Per-session brain-harness override, e.g. "pi". Nullable; None
    # means use the spec's executor.config.harness (see entities.Conversation).
    harness_override: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Model Routing Agent approval state. Nullable so existing/manual sessions
    # preserve their current picker behavior until explicitly enabled.
    route_approval_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    omniroute_route_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    permission_mode: Mapped[str | None] = mapped_column(String(64), nullable=True)
    omniroute_requires_explicit_approval: Mapped[bool | None] = mapped_column(
        Boolean, nullable=True
    )
    # Sub-agent type name within the parent's spec tree, e.g.
    # "summarizer". The runner uses this to load the sub-agent's
    # AgentSpec instead of the parent's. Replaces task.agent_name
    # from the removed task store. None for top-level sessions.
    sub_agent_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Monotonic allocator for the next item position in this conversation.
    # append() reads and advances this instead of scanning
    # MAX(SqlConversationItem.position) on every write, making position
    # assignment O(1) and collision-free under the conversation lock. New rows
    # start at 0 (column default); NULL marks a row created before this column
    # existed, which append() backfills via a one-time scan on its next write.
    next_position: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0)
    external_session_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # JSON-serialized mutable per-conversation key/value store
    # used by policy callables to accumulate state across turns.
    # NULL when no policy has written state yet; empty JSON object
    # "{}" is equivalent. Stored as Text (not a native JSON column)
    # for SQLite compatibility.
    session_state: Mapped[str | None] = mapped_column(CompressedText, nullable=True)
    # JSON-serialized cumulative LLM token usage for policy
    # callables. Shape: {"input_tokens": N, "output_tokens": M,
    # "total_tokens": T, "cache_read_input_tokens": C1,
    # "cache_creation_input_tokens": C2, "total_cost_usd": X}.
    # NULL when no LLM calls have been recorded yet.
    session_usage: Mapped[str | None] = mapped_column(CompressedText, nullable=True)
    # Pass-through CLI args for a native terminal wrapper (claude /
    # codex), JSON-encoded list of strings, e.g.
    # '["--dangerously-skip-permissions"]'. NULL for non-native
    # sessions. The runner reconstructs the terminal launch command
    # from these plus the harness binary; the command itself and all
    # bridge / AP-URL / auth wiring are runner-owned and never stored
    # here. A flat list (not a dict) is deliberate: there is no key for
    # a user to smuggle internal wiring through. See
    # designs/NATIVE_RUNNER_SERVER_LAUNCH.md.
    terminal_launch_args: Mapped[str | None] = mapped_column(CompressedText, nullable=True)
    # Absolute path on the host where the runner cd's. Required
    # when host_id is set; CHECK constraint below. When a git worktree
    # was created for the session, this is the worktree directory path.
    workspace: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    # Git branch checked out in the session's worktree, e.g.
    # "feature/login". Set only when the session was created with a
    # server-created git worktree; None otherwise. Gates worktree
    # cleanup on delete. See designs/SESSION_GIT_WORKTREE.md.
    git_branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Whether the session is archived (hidden from the default
    # /v1/sessions listing and the sidebar). False for normal
    # sessions; server_default false backfills existing rows on the
    # migration that adds this column. Low-cardinality, so no index —
    # the listing's accessible_by subquery is the selective filter.
    archived: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )

    __table_args__ = (
        CheckConstraint("kind IN (1, 2)", name="ck_conversations_kind"),
        CheckConstraint(
            "host_id IS NULL OR workspace IS NOT NULL",
            name="ck_conversations_workspace_required_for_host",
        ),
        Index("ix_conversations_created_at", "workspace_id", "created_at", "id"),
        Index("ix_conversations_updated_at", "workspace_id", "updated_at", "id"),
        Index("ix_conversations_kind", "workspace_id", "kind", "id"),
        # Agent lookups: find the conversation(s) that own a given agent.
        Index("ix_conversations_agent_id", "workspace_id", "agent_id", "id"),
        Index(
            "ix_conversations_root_conversation_id",
            "workspace_id",
            "root_conversation_id",
            "id",
        ),
        # Reconnect/relaunch reconciliation looks up a runner's session(s)
        # by runner_id (list_conversations_by_runner_id) on every runner
        # reconnect; index it to avoid a full scan.
        Index("ix_conversations_runner_id", "workspace_id", "runner_id", "id"),
        # Unique index on (parent_conversation_id, title) prevents two
        # same-named children under the same parent (G36 race protection at
        # the DB layer). Top-level conversations (NULL parent) are exempt
        # automatically: NULLs are distinct in a unique index, so no WHERE
        # predicate is needed — keeping it a plain index MySQL can build.
        Index(
            "ix_conversations_parent_title_unique",
            "workspace_id",
            "parent_conversation_id",
            "title",
            unique=True,
            mysql_length={"title": 512},
        ),
        # Composite index for child-session listing
        # (list_conversations(kind="sub_agent", parent_conversation_id=...)).
        # Non-unique, so no scoping predicate is required; it simply indexes
        # every parented row rather than only the sub-agent ones.
        Index(
            "idx_conversations_parent",
            "workspace_id",
            "parent_conversation_id",
            text("created_at DESC"),
            text("id DESC"),
        ),
    )


class SqlConversationItem(Base):
    """
    SQLAlchemy model for the ``conversation_items`` table.

    Each row represents a single item (message, function call,
    function call output, or reasoning block) within a conversation.

    :param id: Unique item identifier with a type-based prefix,
        e.g. ``"msg_a1b2c3..."``, ``"fc_d4e5f6..."``.
    :param conversation_id: Foreign key to
        :class:`SqlConversation.id`. Cascades on delete.
    :param response_id: The task/response ID this item belongs to,
        e.g. ``"resp_d8e9f0a1..."``.
    :param created_at: Unix epoch seconds when the item was created.
    :param status: Item status string. Defaults to ``"completed"``.
    :param position: Zero-based ordering index within the
        conversation. Used for deterministic item ordering.
    :param type: Item type discriminator, one of ``"message"``,
        ``"function_call"``, ``"function_call_output"``,
        ``"reasoning"``.
    :param data: JSON-serialized item payload. Structure varies by
        ``type``.
    :param search_text: Plain-text extraction of ``data`` used for
        full-text search indexing.
    :param created_by: Identity of the human actor who authored the
        item, or ``None`` for agent/tool/system items and single-user
        mode. Mirrors :class:`SqlComment.created_by`.
    """

    __tablename__ = "conversation_items"

    # Tenant partition key: Databricks workspace id owning this row (0 = default). Part of the PK.
    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    # conversation_id leads id in the PK so a conversation's items stay
    # contiguous for the per-conversation prefix scans that dominate reads.
    conversation_id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    response_id: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[int] = mapped_column(Integer)
    # Enum stored as a stable int code (see omnigent.db.enum_codecs
    # ITEM_STATUS: completed=1). Only "completed" is written today, but the
    # CHECK admits the wider OpenAI-style status vocabulary reserved there.
    status: Mapped[int] = mapped_column(SmallInteger, default=1)
    position: Mapped[int] = mapped_column(Integer)
    # Enum stored as a stable int code (see omnigent.db.enum_codecs
    # ITEM_TYPE). The store converts to/from the string name at the
    # row↔entity boundary.
    type: Mapped[int] = mapped_column(SmallInteger)
    data: Mapped[str] = mapped_column(Text)
    search_text: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)

    __table_args__ = (
        Index(
            "ix_conversation_items_conversation_id_position",
            "workspace_id",
            "conversation_id",
            "position",
            unique=True,
        ),
        # Fork-truncation looks up by workspace_id + conversation_id +
        # response_id; id trails to complete the PK.
        Index(
            "ix_conversation_items_response_id",
            "workspace_id",
            "conversation_id",
            "response_id",
            "id",
        ),
        CheckConstraint(
            "type IN (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11)",
            name="ck_conversation_items_type",
        ),
        CheckConstraint("status IN (1, 2, 3, 4)", name="ck_conversation_items_status"),
    )


# Width of the ``conversation_labels.value`` column. Exported so the store
# (and the session-status error-label path) can clamp values to fit instead
# of letting an over-length write raise ``DataError`` on PostgreSQL.
LABEL_VALUE_MAX_LEN = 256


class SqlConversationLabel(Base):
    """
    SQLAlchemy model for the ``conversation_labels`` table.

    One row per (conversation, label-key) pair. Labels live in
    a dedicated table rather than a JSON column on
    ``conversations`` so per-key UPDATEs are atomic without
    read-modify-write (see POLICIES.md §6). The table is keyed
    only by ``conversation_id`` + ``key``, so it is untouched
    by compaction (which rewrites ``conversation_items``) —
    labels set turn 3 still exist turn 20 even after the
    earlier turns have been folded into a summary.

    :param conversation_id: The conversation this label belongs
        to. Composite PK member. Deleted with the conversation
        via ``ON DELETE CASCADE``.
    :param key: The label key, e.g. ``"integrity"``,
        ``"sensitivity"``. Composite PK member.
    :param value: The label value as a string, e.g. ``"0"``,
        ``"confidential"``. All label values are string-typed
        regardless of what the YAML author wrote — the parser
        coerces scalar / list values during spec load
        (POLICIES.md §14).
    :param updated_at: Unix epoch seconds of the last write.
        Single timestamp for each row; on UPSERT the row's
        timestamp is refreshed even when the value is
        unchanged (matches omnigent parity and keeps
        debugging timelines accurate).
    """

    __tablename__ = "conversation_labels"

    # Tenant partition key: Databricks workspace id owning this row (0 = default). Part of the PK.
    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    conversation_id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
    )
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(String(LABEL_VALUE_MAX_LEN))
    updated_at: Mapped[int] = mapped_column(Integer)


class SqlComment(Base):
    """SQLAlchemy model for the ``comments`` table.

    Stores per-review comments associated with a conversation.
    Each comment is anchored to a character range in the file expressed
    as absolute document-level offsets. Comments survive server restarts
    and are cleaned up when the owning conversation is deleted.

    :param id: UUID primary key, e.g. ``"a1b2c3d4-..."``.
    :param conversation_id: The conversation this comment belongs to.
    :param path: File path relative to the workspace root,
        e.g. ``"src/App.tsx"``.
    :param start_index: 0-based absolute character offset (inclusive)
        within the file where the anchor range begins.
    :param end_index: 0-based absolute character offset (exclusive)
        within the file where the anchor range ends.
    :param body: The comment text.
    :param status: One of ``"draft"``, ``"addressed"``.
    :param created_at: Unix epoch seconds at row creation.
    :param updated_at: Unix epoch **microseconds** of the last
        body/status mutation; set at creation for never-edited
        comments. Feeds the per-session comments fingerprint surfaced
        on ``GET /v1/sessions`` so clients can detect comment changes;
        microsecond precision keeps back-to-back mutations within one
        second distinguishable while remaining an exact integer in
        JavaScript. ``BigInteger`` because epoch-µs overflows a
        32-bit column on PostgreSQL.
    :param anchor_content: Plain-text snapshot of the selected range at
        comment creation time. Used to re-anchor the comment (e.g. via
        content search) when the file is subsequently edited.
        ``NULL`` for legacy comments created before anchor support.
    :param created_by: Email of the user who created this comment,
        e.g. ``"alice@example.com"``. ``NULL`` for legacy comments or
        comments created in single-user mode.
    """

    __tablename__ = "comments"

    # Tenant partition key: Databricks workspace id owning this row (0 = default). Part of the PK.
    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(64))
    path: Mapped[str] = mapped_column(String(4096))
    start_index: Mapped[int] = mapped_column(Integer)
    end_index: Mapped[int] = mapped_column(Integer)
    body: Mapped[str] = mapped_column(CompressedText)
    # Enum stored as a stable int code (see omnigent.db.enum_codecs
    # COMMENT_STATUS: draft=1, addressed=2).
    status: Mapped[int] = mapped_column(SmallInteger)
    created_at: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[int] = mapped_column(BigInteger)
    anchor_content: Mapped[str | None] = mapped_column(CompressedText, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)

    __table_args__ = (
        CheckConstraint("status IN (1, 2)", name="ck_comments_status"),
        # Serves list_for_conversation: WHERE workspace_id + conversation_id
        # ORDER BY created_at, id. Folds created_at in (over a bare
        # conversation_id index) so the sort is index-ordered; trails id to
        # complete the PK.
        Index(
            "ix_comments_conversation_id",
            "workspace_id",
            "conversation_id",
            "created_at",
            "id",
        ),
    )


def policy_name_cksum(name: str) -> bytes:
    """Return the sha256 digest of a policy name.

    This 32-byte digest is what the name-uniqueness indexes key on instead
    of the raw ``VARCHAR(256)`` name — a fixed, compact index entry. Two
    names collide iff their digests do, so uniqueness is preserved.
    """
    return hashlib.sha256(name.encode("utf-8")).digest()


def _default_policy_name_cksum(context: Any) -> bytes:
    """Column default: derive ``name_cksum`` from the bound ``name`` on INSERT.

    Mirrors the ``workspace_id`` default pattern so every ORM insert stamps
    the checksum without the caller setting it. Column defaults do not fire
    on UPDATE, so renames recompute it explicitly in the store.
    """
    return policy_name_cksum(context.get_current_parameters()["name"])


class SqlPolicy(Base):
    """
    SQLAlchemy model for the ``policies`` table.

    Policies are either session-scoped (``session_id`` set, FK to
    ``conversations.id``) or server-wide defaults
    (``session_id IS NULL``).

    Session-scoped policies are created via
    ``POST /v1/sessions/{session_id}/policies``. Default policies
    are created via ``POST /v1/policies``.

    :param id: Opaque PK, e.g. ``"pol_a1b2c3..."``.
    :param name: Human-readable name. UNIQUE per session for
        session policies; globally unique for default policies
        (``session_id IS NULL``). Uniqueness is enforced on
        ``name_cksum`` rather than this column.
    :param name_cksum: sha256 digest of ``name`` (32 bytes). The
        name-uniqueness indexes key on this compact digest instead
        of the wide ``VARCHAR(256)`` name. Stamped on INSERT by a
        column default; recomputed by the store on rename.
    :param session_id: FK to ``conversations.id``. ``None`` for
        server-wide default policies. ``ON DELETE CASCADE`` so
        removing a session cleans up its policies.
    :param created_at: Unix epoch seconds at row creation.
    :param updated_at: Unix epoch seconds of the last write,
        ``None`` if the row has never been updated.
    :param type: Handler discriminator: ``"python"``,
        ``"url"``.
    :param handler: Dotted import path (``type="python"``)
        or HTTPS URL (``type="url"``).
    :param factory_params: JSON-encoded dict of kwargs passed to
        the handler when it is a factory function. ``None`` when
        the handler is a direct callable or for ``type="url"``.
    :param enabled: Whether the engine consults this row.
        Defaults to true.
    :param scope: ``"default"`` for server-wide policies;
        ``"session"`` for session-scoped policies. Explicit
        discriminator so queries filter by column value instead
        of checking ``session_id IS NULL``.
    :param created_by: User ID of the admin who created this
        policy. ``None`` in single-user mode or for
        session-scoped policies.
    """

    __tablename__ = "policies"

    # Tenant partition key: Databricks workspace id owning this row (0 = default). Part of the PK.
    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(256))
    # sha256(name) — the value the name-uniqueness indexes key on instead of
    # the wide name column. Stamped from `name` on INSERT via the column
    # default; the store recomputes it on rename (defaults don't fire on UPDATE).
    name_cksum: Mapped[bytes] = mapped_column(_CKSUM32, default=_default_policy_name_cksum)
    # Nullable: NULL for server-wide default policies.
    session_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    created_at: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Handler discriminator stored as a stable int code (see
    # omnigent.db.enum_codecs POLICY_TYPE: python=1, url=2).
    type: Mapped[int] = mapped_column(SmallInteger)
    # Dotted import path (type="python") or HTTPS URL
    # (type="url") for the policy handler.
    handler: Mapped[str] = mapped_column(Text)
    # JSON-encoded dict of factory kwargs for type="python" when
    # the handler is a factory function. NULL when the handler is
    # a direct callable or for type="url". See the design doc's
    # FunctionRef.arguments pattern.
    factory_params: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=true())
    # "default" for server-wide policies; "session" for per-conversation
    # copies. Mirrors the agents.kind pattern so queries filter by column
    # value rather than session_id IS NULL. Enum stored as a stable int
    # code (see omnigent.db.enum_codecs POLICY_SCOPE: default=1, session=2).
    scope: Mapped[int] = mapped_column(SmallInteger)
    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)

    __table_args__ = (
        CheckConstraint("type IN (1, 2)", name="ck_policies_type"),
        CheckConstraint("scope IN (1, 2)", name="ck_policies_scope"),
        Index("ix_policies_created_at", "workspace_id", "created_at", "id"),
        Index("ix_policies_session_id", "workspace_id", "session_id", "id"),
        # Name uniqueness keys on name_cksum (sha256 of name) rather than the
        # wide name column, for a compact 32-byte index entry.
        UniqueConstraint(
            "workspace_id",
            "session_id",
            "name_cksum",
            name="uq_policies_session_id_name_cksum",
        ),
        # Default policies must have unique names; session-scoped policies
        # may reuse the same name. That "unique only within the default set"
        # rule can't be a partial unique index (MySQL has none), so it is
        # enforced in the store (add_default / update_default). This plain
        # index just backs the name_cksum lookup those checks perform.
        Index("ix_policies_name_cksum", "workspace_id", "name_cksum", "id"),
    )


class SqlHost(Base):
    """
    SQLAlchemy model for the ``hosts`` table.

    Each row represents a machine that has connected to the server
    via ``omnigent host``. The row is upserted on first connect
    and updated on subsequent reconnects (name, status, timestamps).

    :param host_id: Stable host identifier from the host's local
        ``~/.omnigent/config.yaml``, e.g. ``"host_a1b2c3d4e5f6..."``.
    :param name: Human-readable name from ``config.yaml``, e.g.
        ``"corey-laptop"``. Displayed in the Web UI host picker. Max 64
        characters.
    :param owner: User ID from the Databricks auth Bearer token
        presented during the host's WebSocket handshake, e.g.
        ``"corey.zumar@databricks.com"``.
    :param status: ``"online"`` when the host has an active WebSocket
        connection, ``"offline"`` when disconnected.
    :param created_at: Unix epoch seconds when the host was first
        registered (first ``omnigent host``).
    :param updated_at: Unix epoch seconds the row was last touched — a
        status change (connect/disconnect) or a tunnel heartbeat. Doubles
        as the host's last-seen for the liveness freshness gate, so a
        host that crashed without a graceful disconnect ages out of the
        "online" set once this stops advancing.
    :param token_hash: Hex SHA-256 digest of the launch token that
        authenticates a SERVER-MANAGED sandbox host's tunnel connection
        (``host_type="managed"`` sessions) — never the raw token.
        ``NULL`` for external (user-connected) hosts. Overwritten when
        the sandbox is relaunched, which atomically revokes the
        previous generation's token.
    :param token_expires_at: Unix epoch seconds after which the launch
        token no longer authenticates. Scoped to the TOKEN, not the
        host — the host row is durable across sandbox generations; the
        expiry is set past the provider's maximum sandbox lifetime so a
        live sandbox can always reconnect while a token leaked from a
        dead one cannot. ``NULL`` for external hosts.
    :param sandbox_provider: Sandbox provider backing a managed host,
        e.g. ``"modal"``. ``NULL`` for external hosts — non-NULL is the
        "this host is server-managed" discriminator.
    :param sandbox_id: Provider-assigned id of the sandbox currently
        backing the host, e.g. ``"sb-a1b2c3"`` — what termination is
        issued against. ``NULL`` for external hosts.
    :param configured_harnesses: JSON-encoded per-harness readiness map
        reported in the host's last ``host.hello`` frame, e.g.
        ``'{"claude-sdk": true, "codex": false}'``. ``NULL`` when the
        host has never reported it (older host build) — unknown, not
        "nothing configured". Surfaced via ``GET /v1/hosts`` so the web
        agent picker can warn about unconfigured harnesses.
    """

    __tablename__ = "hosts"

    # Tenant partition key: Databricks workspace id owning this row (0 = default). Part of the PK.
    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    host_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner: Mapped[str] = mapped_column(String(256), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    # Enum stored as a stable int code (see omnigent.db.enum_codecs
    # HOST_STATUS: online=1, offline=2).
    status: Mapped[int] = mapped_column(SmallInteger)
    created_at: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[int] = mapped_column(Integer)
    token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    token_expires_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sandbox_provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    sandbox_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    configured_harnesses: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN (1, 2)",
            name="ck_hosts_status",
        ),
        # (workspace_id, owner, name) was the old PK; keep it unique so the
        # upsert-on-connect logic (look up by owner+name to detect host_id
        # rotation) stays consistent.
        UniqueConstraint("workspace_id", "owner", "name", name="uq_hosts_workspace_owner_name"),
        # resolve_launch_token filters workspace_id + token_hash, so scoping
        # the unique to the workspace keeps that lookup index-served.
        UniqueConstraint("workspace_id", "token_hash", name="uq_hosts_token_hash"),
    )


class SqlUserDailyCost(Base):
    """
    SQLAlchemy model for the ``user_daily_cost`` table.

    A running per-user, per-UTC-day rollup of LLM spend, used by
    cost-aware policies (e.g. the "downgrade expensive model once a
    user has spent >$X today" sample policy) to read a user's
    accumulated daily cost as a single O(1) point lookup instead of
    aggregating the per-session ``conversations.session_usage`` blobs
    on every policy evaluation.

    One row per ``(user_id, day_utc)``. Incremented (UPSERT
    ``cost_usd = cost_usd + delta``) at each turn boundary from the
    cost write sites — but only when the session runs under at least
    one policy, so the table is never touched in deployments that
    have no policies configured (this keeps the shared server code
    inert against a database that lacks this table).

    :param user_id: The user the cost is attributed to — the session
        creator (``LEVEL_OWNER`` grantee), e.g.
        ``"alice@example.com"``.
    :param day_utc: The UTC calendar day the spend occurred, as an
        ISO date string ``"YYYY-MM-DD"``, e.g. ``"2026-06-05"``.
        Bucketed by the turn's wall-clock time, so a session spanning
        midnight splits its cost across both days correctly.
    :param cost_usd: Cumulative USD spend for this user on this day.
        Starts at the first turn's delta and grows by each subsequent
        turn's delta.
    :param ask_approved_usd: Highest soft warning checkpoint (USD) the
        user has already approved continuing past for this day — read
        and written by the per-user daily cost-budget policy so an
        approved checkpoint prompts at most once per day (across all of
        the user's sessions), not once per session. ``0.0`` (the
        server default) means no checkpoint approved yet.
    :param updated_at: Unix epoch seconds of the last increment.
    """

    __tablename__ = "user_daily_cost"

    # Tenant partition key: Databricks workspace id owning this row (0 = default). Part of the PK.
    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    user_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    day_utc: Mapped[str] = mapped_column(String(10), primary_key=True)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False)
    ask_approved_usd: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    updated_at: Mapped[int] = mapped_column(Integer)


class SqlTaskRun(Base):
    """
    SQLAlchemy model for the ``task_runs`` table.

    One row per routed coding execution attempt initiated by a user
    message. The relay creates the row on ``response.in_progress``
    with the routing snapshot captured at approval time (when
    approval is on); the row's status is updated to a terminal
    value on the matching response.* event.

    The routing snapshot is immutable after first write so a later
    ``PATCH /v1/sessions/{id}`` (e.g. the user changing route) does
    not retroactively rewrite provenance. Token usage comes from
    ``response.completed.usage`` and is best-effort — the harness
    is not required to populate every bucket.

    See ``omnigent/server/task_outcome_recorder.py`` for the write
    paths and ``omnigent/entities/task_outcome.py`` for the wire /
    store boundary shape.

    :param id: UUID for the run, e.g. ``"tr_abc123"``.
    :param conversation_id: Owning session id, e.g. ``"conv_abc123"``.
    :param response_id: Harness-side task id (matches
        ``conversation_items.response_id`` for the user message that
        started the run). Stamped on first ``response.in_progress``;
        ``None`` while the harness hasn't yet emitted it.
    :param triggering_message_id: User message item id that started
        the run (when known).
    :param project_path: Repository / workspace identifier at task
        start. Free-form so a deployment can fill in the cwd, the
        repo URL, etc. without a schema change.
    :param task_description: Sanitized, bounded task description
        (truncated summary of the triggering user message).
    :param proposed_task_family: Family proposed by the LLM
        evaluator; ``None`` until evaluation runs.
    :param estimated_difficulty: Optional difficulty hint from the
        routing agent (e.g. ``"hard"``); ``None`` until surfaced.
    :param harness_id: Harness name resolved at task start, e.g.
        ``"OpenCode Native"`` or ``"pi"``.
    :param requested_route_id: Native OmniRoute route id the
        routing agent proposed, e.g. ``"auto/coding"``. Mirrors the
        routing snapshot at start; never updated.
    :param selected_provider: Concrete provider resolved by
        OmniRoute for this run (e.g. ``"databricks"``).
    :param selected_model: Concrete model resolved by OmniRoute for
        this run (e.g. ``"databricks-claude-sonnet-4-6"``).
    :param reasoning_effort: Routing snapshot — e.g. ``"medium"``.
    :param permission_mode: Routing snapshot — e.g.
        ``"ask_before_edits"``.
    :param omniroute_decision_id: Stable per-call decision id
        returned by OmniRoute (header
        ``x-omniroute-decision-id``).
    :param selection_strategy: Strategy used by OmniRoute (e.g.
        ``"single"``); ``None`` when not reported.
    :param billing_class: Billing class for the resolved model
        (e.g. ``"free"``, ``"subscription"``,
        ``"api_billed"``, ``"unknown"``).
    :param fallback_used: ``True`` when OmniRoute fell back to a
        secondary model. ``None`` until the response lifecycle
        reports it.
    :param terminal_status: SMALLINT code matching
        ``db.enum_codecs.TASK_RUN_STATUS``: ``running``/``completed``/
        ``failed``/``cancelled``/``incomplete``.
    :param started_at: Unix epoch seconds the run row was created.
    :param terminal_at: Unix epoch seconds the terminal event was
        observed.
    :param duration_ms: ``terminal_at - started_at`` in
        milliseconds. ``None`` while running.
    :param input_tokens: Total input tokens across the turn
        (best-effort; harness is not required to populate).
    :param output_tokens: Total output tokens across the turn.
    :param total_cost_usd: Catalog-priced or harness-reported USD
        cost for the turn. ``None`` when unpriced.
    :param response_summary: Truncated final assistant response
        (sanitized to a bounded length by the writer).
    :param changed_files_json: JSON-encoded list of changed file
        paths when the harness surfaced them, e.g.
        ``'["src/api/x.py", "src/api/y.py"]'``. ``None`` when not
        surfaced.
    :param commit_sha: Git commit SHA when the harness surfaced it;
        ``None`` when unavailable.
    :param failure_error_code: Error code from
        ``response.failed.error.code`` or
        ``response.incomplete.incomplete_details.reason``.
    :param failure_error_message: Error message from the same
        payload, truncated.
    :param langfuse_trace_id: Langfuse trace id stamped on first
        successful sync; ``None`` while pending or unconfigured.
    :param langfuse_observation_id: Langfuse observation id for
        the root trace observation (per Langfuse's
        ``observation`` field on the scores endpoint).
    :param created_at: Unix epoch seconds when the row was first
        written (same as ``started_at`` for this schema).
    :param updated_at: Unix epoch seconds of the last write.
    """

    __tablename__ = "task_runs"

    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    response_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    triggering_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    project_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    task_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    proposed_task_family: Mapped[str | None] = mapped_column(String(64), nullable=True)
    estimated_difficulty: Mapped[str | None] = mapped_column(String(32), nullable=True)
    harness_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    requested_route_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    selected_provider: Mapped[str | None] = mapped_column(String(128), nullable=True)
    selected_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reasoning_effort: Mapped[str | None] = mapped_column(String(32), nullable=True)
    permission_mode: Mapped[str | None] = mapped_column(String(64), nullable=True)
    omniroute_decision_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    selection_strategy: Mapped[str | None] = mapped_column(String(64), nullable=True)
    billing_class: Mapped[str | None] = mapped_column(String(32), nullable=True)
    fallback_used: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # Legacy projection of execution_status. Never written by evaluation code.
    terminal_status: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="1")
    execution_status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="running"
    )
    evaluation_status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="not_requested"
    )
    execution_started_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    execution_finished_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    execution_duration_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    evaluation_started_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    evaluation_finished_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    timeout_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_useful_activity_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    actual_provider: Mapped[str | None] = mapped_column(String(128), nullable=True)
    actual_provider_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    actual_provenance_verified: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    started_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    terminal_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    total_cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    response_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    changed_files_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    commit_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    langfuse_trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    langfuse_observation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "terminal_status IN (1, 2, 3, 4, 5)",
            name="ck_task_runs_terminal_status",
        ),
        # (workspace_id, conversation_id, started_at DESC) — the listing
        # path's WHERE + ORDER BY index, id trailing to complete the PK.
        Index(
            "ix_task_runs_conversation_started_at",
            "workspace_id",
            "conversation_id",
            text("started_at DESC"),
            "id",
        ),
        # (workspace_id, response_id, id) — task-run lookup by response id
        # (the relay stamps ``response_id`` on first in_progress and the
        # terminal branch needs to find the row it just updated).
        Index(
            "ix_task_runs_response_id",
            "workspace_id",
            "response_id",
            "id",
        ),
        # (workspace_id, terminal_status, id) — powers the unreviewed /
        # pending-evaluator listings.
        Index(
            "ix_task_runs_terminal_status",
            "workspace_id",
            "terminal_status",
            "id",
        ),
    )


class SqlTaskRunModelCall(Base):
    """Sanitized, per-request runtime evidence captured by the provenance proxy."""

    __tablename__ = "task_run_model_calls"

    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    conversation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    opencode_session_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    requested_provider: Mapped[str] = mapped_column(String(128), nullable=False)
    requested_model: Mapped[str] = mapped_column(String(128), nullable=False)
    requested_reasoning: Mapped[str | None] = mapped_column(String(32), nullable=True)
    effective_reasoning: Mapped[str | None] = mapped_column(String(32), nullable=True)
    stream: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    selected_provider: Mapped[str | None] = mapped_column(String(128), nullable=True)
    selected_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    omniroute_request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    omniroute_decision_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    fallback_used: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    selection_strategy: Mapped[str | None] = mapped_column(String(64), nullable=True)
    billing_class: Mapped[str | None] = mapped_column(String(32), nullable=True)
    provenance_verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=false()
    )
    request_status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="in_progress"
    )
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    failure_stage: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    first_response_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    finished_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "correlation_id", name="uq_task_run_model_calls_correlation"
        ),
        UniqueConstraint(
            "workspace_id", "task_run_id", "ordinal", name="uq_task_run_model_calls_ordinal"
        ),
        UniqueConstraint(
            "workspace_id", "omniroute_request_id", name="uq_task_run_model_calls_request"
        ),
        Index("ix_task_run_model_calls_run_ordinal", "workspace_id", "task_run_id", "ordinal"),
    )


class SqlTaskEvaluation(Base):
    """
    SQLAlchemy model for the ``task_evaluations`` table.

    Append-only: each row is one immutable automated evaluation. LLM
    evaluations are produced by ``omnigent.server.task_outcome_evaluator``
    after the task reaches a terminal state. When the evaluator fails,
    a single ``verdict='inconclusive'`` row is still recorded so the
    schema contract is "always exactly one evaluation per task run"
    rather than "sometimes missing" (the schema avoids a JOIN on the
    review card to decide whether evaluation ran).

    :param id: UUID for the evaluation, e.g. ``"tev_abc123"``.
    :param task_run_id: Owning :class:`SqlTaskRun.id`.
    :param evaluator_type: SMALLINT code matching
        ``db.enum_codecs.TASK_EVALUATION_TYPE``: ``deterministic``/``llm``.
    :param evaluator_provider: Concrete provider resolved by OmniRoute
        for the evaluation call.
    :param evaluator_model: Concrete model resolved by OmniRoute for
        the evaluation call.
    :param evaluator_route_id: Native OmniRoute route id the
        evaluator used, when known (the same ``PolicyLLMClient`` the
        routing agent uses, so it inherits the same routing path).
    :param verdict: ``success`` / ``partial`` / ``failure`` /
        ``inconclusive``. ``inconclusive`` is recorded both when the
        evaluator returns it AND when the evaluator call itself
        fails (the failure is captured in ``reasoning``).
    :param confidence: 0.0–1.0 confidence from the LLM. ``None``
        when the evaluator didn't produce one (deterministic / failed).
    :param quality_score: 1–5 from the LLM. ``None`` when not
        produced.
    :param proposed_task_family: Family the evaluator proposes.
    :param reasoning: Free-text reasoning from the evaluator (or a
        bounded error message when the evaluator call failed).
    :param evidence_json: JSON-encoded list of strings (evidence
        items the evaluator cited).
    :param unresolved_issues_json: JSON-encoded list of strings
        (issues the evaluator flagged but did not resolve).
    :param created_at: Unix epoch seconds when the row was written.
    """

    __tablename__ = "task_evaluations"

    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    evaluator_type: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    evaluator_provider: Mapped[str | None] = mapped_column(String(128), nullable=True)
    evaluator_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    evaluator_route_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    verdict: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    quality_score: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    proposed_task_family: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    unresolved_issues_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (
        CheckConstraint("evaluator_type IN (1, 2)", name="ck_task_evaluations_type"),
        CheckConstraint(
            "verdict IN ('success','partial','failure','inconclusive')",
            name="ck_task_evaluations_verdict",
        ),
        # (workspace_id, task_run_id, created_at, id) — listing all
        # evaluations for a run ordered by time.
        Index(
            "ix_task_evaluations_run",
            "workspace_id",
            "task_run_id",
            "created_at",
            "id",
        ),
    )


class SqlTaskReview(Base):
    """
    SQLAlchemy model for the ``task_reviews`` table.

    One human review per ``(task_run_id, reviewer)``. The unique
    constraint on ``(workspace_id, task_run_id, created_by)`` is what
    makes the table idempotent on re-submit — a PATCH to the same
    reviewer's review updates the existing row instead of appending
    a duplicate. Reviews are stored SEPARATELY from the LLM
    evaluation so a human disagreement never overwrites the LLM
    verdict.

    :param id: UUID for the review, e.g. ``"trv_abc123"``.
    :param task_run_id: Owning :class:`SqlTaskRun.id`.
    :param verdict: ``success`` / ``partial`` / ``failure`` /
        ``unsure`` / ``skipped``. ``skipped`` is a real persisted
        state (so the UI can show "Skipped" rather than
        "Not reviewed") and remains re-editable later.
    :param quality_score: 1–5 from the human, optional.
    :param final_task_family: Task family the human picked (may
        correct the LLM's proposed family).
    :param evaluator_accuracy: ``correct`` / ``partly_correct`` /
        ``incorrect`` / ``unsure`` — the human's view of whether the
        LLM verdict was right. ``None`` when not yet filled in.
    :param comments: Optional free-text.
    :param created_by: Reviewer email / id. ``NULL`` in single-user
        mode and for legacy reviews.
    :param created_at: Unix epoch seconds when the row was first
        written.
    :param updated_at: Unix epoch seconds of the last write
        (body / verdict / etc.). Same value as ``created_at`` for
        never-edited reviews.
    """

    __tablename__ = "task_reviews"

    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    verdict: Mapped[str] = mapped_column(String(32), nullable=False)
    quality_score: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    final_task_family: Mapped[str | None] = mapped_column(String(64), nullable=True)
    evaluator_accuracy: Mapped[str | None] = mapped_column(String(32), nullable=True)
    comments: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    review_action: Mapped[str | None] = mapped_column(String(16), nullable=True)
    learning_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="0")
    route_fit: Mapped[str | None] = mapped_column(String(32), nullable=True)
    failure_attribution: Mapped[str | None] = mapped_column(String(32), nullable=True)
    preferred_route_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    preferred_reasoning_effort: Mapped[str | None] = mapped_column(String(16), nullable=True)
    source_evaluation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    review_schema_version: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default="1"
    )
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "verdict IN ('success','partial','failure','unsure','skipped')",
            name="ck_task_reviews_verdict",
        ),
        CheckConstraint(
            "review_action IS NULL OR review_action IN ('accepted','adjusted','declined')",
            name="ck_task_reviews_action",
        ),
        CheckConstraint(
            "learning_eligible = 0 OR review_action IN ('accepted','adjusted')",
            name="ck_task_reviews_learning",
        ),
        CheckConstraint(
            "route_fit IS NULL OR route_fit IN "
            "('appropriate','too_weak','overkill','wrong_capability','unsure')",
            name="ck_task_reviews_route_fit",
        ),
        CheckConstraint(
            "failure_attribution IS NULL OR failure_attribution IN "
            "('router','model','harness','environment','permissions','task_definition',"
            "'external_service','unknown')",
            name="ck_task_reviews_failure_attribution",
        ),
        CheckConstraint(
            "evaluator_accuracy IS NULL OR evaluator_accuracy IN "
            "('correct','partly_correct','incorrect','unsure')",
            name="ck_task_reviews_evaluator_accuracy",
        ),
        # Idempotent re-submit: a re-submission by the same reviewer
        # replaces the existing row in place instead of appending.
        UniqueConstraint(
            "workspace_id",
            "task_run_id",
            "created_by",
            name="uq_task_reviews_run_reviewer",
        ),
        # (workspace_id, task_run_id, updated_at, id) — listing reviews
        # for a run ordered by most-recent-first.
        Index(
            "ix_task_reviews_run",
            "workspace_id",
            "task_run_id",
            "updated_at",
            "id",
        ),
    )


class SqlLangfuseSyncOutbox(Base):
    """
    SQLAlchemy model for the ``langfuse_sync_outbox`` table.

    Transactional outbox for Langfuse delivery. Every score / trace
    summary that should reach Langfuse is written here in the same
    database transaction as the originating event (task terminal
    status, evaluator row, or human review), then drained by a
    bounded retry worker started in ``server/app.py``'s lifespan.

    Rows are NEVER deleted (so audits can see what was attempted
    even after delivery). ``status`` moves pending → delivered
    or pending → dead (retry budget exhausted). ``status='skipped'``
    is the audit-record path when Langfuse is unconfigured
    (``LANGFUSE_*`` env unset) — the worker writes it once and the
    row stays as proof that no Langfuse call was attempted.

    :param id: UUID for the outbox row, e.g. ``"lfs_abc123"``.
    :param task_run_id: Owning :class:`SqlTaskRun.id`.
    :param task_evaluation_id: Owning :class:`SqlTaskEvaluation.id`
        when the event is an evaluator row (``None`` for trace /
        human-review-only events).
    :param event_type: ``task_root`` / ``llm_verdict`` /
        ``human_verdict`` / ``human_quality`` /
        ``llm_evaluation_accuracy``. Used by the worker to choose
        the Langfuse endpoint shape (scores vs trace summary).
    :param idempotency_key: Stable Langfuse score id (``task:<run>:…:v1``).
        The Langfuse API uses this as the score ``id`` field so
        retries are idempotent at the Langfuse side.
    :param payload_json: UTF-8 bytes of the JSON request body the
        worker will POST. Pre-computed so a retry replays the same
        body without re-deriving it (and so a future writer change
        cannot silently shift the payload shape).
    :param status: SMALLINT code matching
        ``db.enum_codecs.LANGFUSE_OUTBOX_STATUS``: ``pending`` /
        ``delivered`` / ``dead`` / ``skipped``.
    :param attempt_count: Number of POST attempts so far (incl. the
        current one). Bounded by the retry schedule; capped so a
        stuck row can't keep spinning forever.
    :param last_error: Truncated last-error string from the worker
        (HTTP status / exception message). ``None`` while pending
        and never written.
    :param next_attempt_at: Unix epoch seconds the worker should
        next try this row. Set to the row's ``created_at`` initially
        so the first attempt happens immediately, then advanced by
        the retry schedule.
    :param created_at: Unix epoch seconds the row was written.
    :param delivered_at: Unix epoch seconds of the successful POST.
        ``None`` while pending / dead / skipped.
    """

    __tablename__ = "langfuse_sync_outbox"

    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    task_evaluation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_json: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    status: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="1")
    attempt_count: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_attempt_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    delivered_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    __table_args__ = (
        CheckConstraint("status IN (1, 2, 3, 4)", name="ck_langfuse_outbox_status"),
        # (workspace_id, status, next_attempt_at, id) — the worker's
        # "what's due right now" scan: WHERE status=1 AND
        # next_attempt_at <= now ORDER BY next_attempt_at, id LIMIT 50.
        Index(
            "ix_langfuse_outbox_due",
            "workspace_id",
            "status",
            "next_attempt_at",
            "id",
        ),
        # (workspace_id, task_run_id, id) — "what's pending for this run"
        # join used by the API's review-detail endpoint.
        Index(
            "ix_langfuse_outbox_run",
            "workspace_id",
            "task_run_id",
            "id",
        ),
    )
