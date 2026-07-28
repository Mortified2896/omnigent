"""SQLAlchemy table definitions for the omnigent database."""

from __future__ import annotations

import contextlib
import hashlib
import uuid
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
    TypeDecorator,
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

# 16-byte (truncated sha256) digest column, same rationale as _CKSUM32 but half
# the width. 128 bits of collision resistance is ample for a per-parent unique
# key, so the conversation title hash uses this narrower form.
_CKSUM16 = LargeBinary(16).with_variant(MySQLBinary(16), "mysql")


# Hex length of a bare uuid4 id, the canonical Python-side form.
_UUID_HEX_LEN = 32

# Prefixes ids carried before they became bare 32-char hex. ``uuid_to_bytes``
# strips exactly these (so old URLs/clients keep resolving) and nothing else —
# an unknown prefix fails loud rather than silently storing a wrong-typed id's
# hex tail (e.g. a ``resp_``/``runner_token_`` value mis-passed to a uuid column).
_LEGACY_ID_PREFIXES = frozenset(
    {
        "ag",
        "conv",
        "host",
        "pol",
        "file",
        "cmt",
        # conversation-item per-type prefixes
        "msg",
        "fc",
        "fco",
        "err",
        "rs",
        "cmp",
        "nt",
        "rse",
        "sc",
        "tc",
        "rd",
        # Control Room task-outcome and routing audit prefixes; these are
        # legacy-shaped identifiers minted before the v0.6 binary conversion
        # (the upstream task-outcome store still emits them via _generate_*).
        "tr",
        "tev",
        "trv",
        "rp",
        "rdc",
        "rdt",
        "lfs",
        "lso",
        "st",
        "str",
        # runner-internal conversation binding
        "agy_conv",
    }
)


class InvalidUuidError(ValueError):
    """An id string could not be normalised to a 32-char hex uuid.

    Subclasses ``ValueError`` so existing ``except ValueError`` sites keep
    working. Surfaced (wrapped in ``sqlalchemy.exc.StatementError``) when a
    malformed id reaches a ``Uuid16`` column bind; the server maps it to a 404
    so a bad id in a URL is not-found rather than a 500.
    """


def uuid_to_bytes(value: str | uuid.UUID | bytes | bytearray | memoryview) -> bytes:
    """Normalise an id to the 16 raw bytes stored in a ``Uuid16`` column.

    Accepts, reducing them all to the same 16 bytes: a :class:`uuid.UUID`
    object; the bare 32-char hex form (what generators emit); the dashed
    canonical uuid (``str(uuid4())``); a legacy id carrying one of the
    known :data:`_LEGACY_ID_PREFIXES` (``conv_<hex>``, ``ag_<hex>``, …) — so
    old bookmarked URLs, pasted ids, and pre-migration clients keep resolving;
    or already-binary 16 bytes (a round-trip through the column layer). Other
    lengths of binary input are treated as a malformed id (fail loud rather
    than silently storing the wrong bytes).

    :param value: A ``uuid.UUID``, a 32-char hex uuid optionally dashed or
        legacy-prefixed, or already-binary bytes.
    :returns: The 16-byte big-endian value.
    :raises InvalidUuidError: If *value* is not a 32-char hex uuid.
    """
    if isinstance(value, uuid.UUID):
        return value.bytes
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        if len(raw) == _UUID_HEX_LEN // 2:
            return raw
        # Otherwise it is a legacy-prefixed hex literal in its ascii bytes
        # form (an upstream caller passed the bytes-equivalent of a str id).
        try:
            value = raw.decode("ascii")
        except UnicodeDecodeError as exc:
            raise InvalidUuidError(f"non-ascii id bytes: {value!r}") from exc
    normalized = value.replace("-", "")
    if "_" in normalized:
        prefix, _, tail = normalized.rpartition("_")
        if prefix in _LEGACY_ID_PREFIXES and len(tail) == _UUID_HEX_LEN:
            normalized = tail
    if len(normalized) != _UUID_HEX_LEN:
        raise InvalidUuidError(f"expected a 32-char hex uuid, got {value!r}")
    try:
        return bytes.fromhex(normalized)
    except ValueError as exc:
        raise InvalidUuidError(f"invalid hex uuid: {value!r}") from exc


def normalize_uuid(value: str | None) -> str | None:
    """Return the bare 32-char hex form of *value*, or *value* unchanged.

    The forgiving companion to :func:`uuid_to_bytes` for **Python-side** id
    comparisons (e.g. a store's scope check against an ORM attribute, which
    always reads back bare hex). A legacy-prefixed or dashed input normalises
    to bare hex; a malformed input is returned as-is so the comparison simply
    mismatches — preserving the pre-migration "unknown id = not found"
    behaviour instead of raising. ``None`` passes through.

    :param value: Any caller-supplied id string, or ``None``.
    :returns: The bare 32-char hex form, or *value* verbatim if not a uuid.
    """
    if value is None:
        return None
    try:
        return uuid_to_bytes(value).hex()
    except InvalidUuidError:
        return value


class Uuid16(TypeDecorator[str]):
    """A uuid stored as 16 raw bytes, presented to Python as bare 32-char hex.

    Our ids are opaque 128-bit uuid4s stored as raw bytes — ``BYTEA``
    (PostgreSQL), ``BLOB`` (SQLite / D1), fixed-length ``BINARY(16)`` (MySQL,
    where a BLOB is not indexable without a key-prefix length). The rest of
    the system keeps the readable bare 32-char hex form (entities, JSON
    blobs, URLs, the FTS mirror), so this type converts at the column
    boundary and nothing else has to change. Binds accept bare, dashed, or
    legacy-prefixed uuids; results always come back as bare lowercase hex.
    Result values guard the same driver variance ``CompressedText`` does:
    ``bytes``, ``memoryview`` (some drivers), or ``str`` (already hex).
    """

    impl = LargeBinary(16)
    cache_ok = True

    def load_dialect_impl(self, dialect: Any) -> Any:
        if dialect.name == "mysql":
            return dialect.type_descriptor(MySQLBinary(16))
        return dialect.type_descriptor(LargeBinary(16))

    def process_bind_param(self, value: str | uuid.UUID | None, _dialect: object) -> bytes | None:
        if value is None:
            return None
        return uuid_to_bytes(value)

    def process_result_value(
        self, value: bytes | memoryview | str | None, _dialect: object
    ) -> str | None:
        if value is None:
            return None
        if isinstance(value, str):
            return value
        return bytes(value).hex()


class OmnigentBase(DeclarativeBase):
    """Declarative base for the Omnigent operational tables.

    Covers agents, files, users, tokens, session permissions,
    conversation metadata, comments, policies, hosts, and daily costs.
    Grouped under their own ``metadata`` so schema creation and Alembic
    autogenerate can target the Omnigent side independently of the
    conversation tables.
    """


class ConversationBase(DeclarativeBase):
    """Declarative base for the conversation tables.

    Covers ``conversations``, ``conversation_items``, and
    ``conversation_labels`` — the user-facing conversation surface
    (the Agent-Platform-side tables). Kept under their own ``metadata``
    so they can be created and, when ``conversation_storage_location``
    is configured, hosted on a separate physical database from the
    Omnigent tables.
    """


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


class SqlAgent(OmnigentBase):
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
    id: Mapped[str] = mapped_column(Uuid16(), primary_key=True)
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


class SqlFile(OmnigentBase):
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
    id: Mapped[str] = mapped_column(Uuid16(), primary_key=True)
    created_at: Mapped[int] = mapped_column(Integer)
    filename: Mapped[str] = mapped_column(String(512))
    bytes: Mapped[int] = mapped_column(Integer)
    content_type: Mapped[str | None] = mapped_column(String(256), nullable=True)
    session_id: Mapped[str | None] = mapped_column(Uuid16(), nullable=True)

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


class SqlUser(OmnigentBase):
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


class SqlAccountToken(OmnigentBase):
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


class SqlDeviceGrant(OmnigentBase):
    """
    SQLAlchemy model for the ``device_grants`` table.

    Backs the generic OAuth 2.0 Device Authorization Grant (RFC 8628) —
    any browserless client (the Slack integration is the first, but the
    mechanism is not Slack-specific) obtains a delegated, per-user access
    token without a user credential passing through the client. One row per
    device-authorization request; it moves ``pending`` → ``approved`` /
    ``denied`` (browser consent) → ``redeemed`` (token issued) and can be
    ``revoked`` at any time.

    Secrets are stored **hashed** (never raw): the client's ``device_code``
    and the current ``refresh_token`` are HMAC-SHA256 digests, so a
    database read cannot recover a usable token.

    :param id: Opaque grant id (also the ``grant_id`` JWT claim on issued
        access tokens, used for revocation).
    :param device_code_hash: HMAC-SHA256 hex digest of the secret
        ``device_code`` the client polls with. Never store the raw code.
    :param user_code: Short human-readable code shown on the verification
        page (also carried in ``verification_uri_complete``).
    :param status: ``pending`` / ``approved`` / ``denied`` / ``redeemed`` /
        ``revoked`` (see :data:`omnigent.db.enum_codecs.DEVICE_GRANT_STATUS`).
    :param client_id: The RFC 8628 client identifier — a public string
        naming the requesting application (e.g. ``"slack"``), the same for
        every grant that application initiates. Shown on the consent page
        and recorded in the issued token's ``act`` claim for audit.
        Display/audit only — not a security-decision key.
    :param user_id: The Omnigent identity that approved the grant, set at
        consent time. ``NULL`` while pending. The delegated token's ``sub``.
    :param refresh_token_hash: HMAC-SHA256 hex digest of the current
        refresh token. Rotated on every refresh; a presented token that no
        longer matches (and isn't the current one) is a reuse signal that
        revokes the grant.
    :param created_at: Unix epoch seconds when the grant was created.
    :param expires_at: Unix epoch seconds after which the ``device_code``
        can no longer be exchanged (the authorization request expires).
    :param approved_at: Unix epoch seconds when the grant was approved,
        starting its absolute lifetime clock. ``NULL`` until approved.
        Refresh is refused once ``approved_at`` is older than the absolute
        max lifetime, forcing periodic re-consent.
    :param last_polled_at: Unix epoch seconds of the last token-poll,
        used to enforce the RFC 8628 ``interval`` / ``slow_down``.
    """

    __tablename__ = "device_grants"

    # Tenant partition key: Databricks workspace id owning this row (0 = default). Part of the PK.
    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    device_code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    user_code: Mapped[str] = mapped_column(String(32), nullable=False)
    # Enum stored as a stable int code (see omnigent.db.enum_codecs
    # DEVICE_GRANT_STATUS). The store converts to/from the name at the
    # row↔entity boundary.
    status: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    client_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    refresh_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Digest of the just-superseded refresh token, kept only so a replay
    # of the previous token can be recognised as reuse (token theft) and
    # revoke the grant. Cleared on revoke.
    prev_refresh_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[int] = mapped_column(Integer, nullable=False)
    approved_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_polled_at: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        CheckConstraint("status IN (1, 2, 3, 4, 5)", name="ck_device_grants_status"),
        # Poll path looks up by device_code_hash; a top-level index keeps it
        # a point lookup rather than a partition scan.
        Index("ix_device_grants_device_code_hash", "workspace_id", "device_code_hash"),
        Index("ix_device_grants_user_code", "workspace_id", "user_code"),
        Index("ix_device_grants_expires_at", "workspace_id", "expires_at", "id"),
        # Refresh + revoke look up by the current / previous refresh-token
        # digest; index both so those paths stay point lookups.
        Index("ix_device_grants_refresh_hash", "workspace_id", "refresh_token_hash"),
        Index("ix_device_grants_prev_refresh_hash", "workspace_id", "prev_refresh_token_hash"),
    )


class SqlSessionPermission(OmnigentBase):
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
        Uuid16(),
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


class SqlConversationMetadata(OmnigentBase):
    """
    SQLAlchemy model for the ``omnigent_conversation_metadata`` table.

    Omnigent-side operational state for a conversation: runner/host
    bindings, native-session linkage, policy accumulators, and launch
    arguments. Paired 1-to-1 with :class:`SqlConversation` by
    ``(workspace_id, id)``; rows are created and deleted together.
    """

    __tablename__ = "omnigent_conversation_metadata"

    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    id: Mapped[str] = mapped_column(Uuid16(), primary_key=True)
    # Enum stored as a stable int code (CONVERSATION_KIND: default=1, sub_agent=2).
    kind: Mapped[int] = mapped_column(SmallInteger, default=1)
    runner_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # No FK: host records are managed outside this table.
    host_id: Mapped[str | None] = mapped_column(Uuid16(), nullable=True)
    sub_agent_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    external_session_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    session_state: Mapped[str | None] = mapped_column(CompressedText, nullable=True)
    session_usage: Mapped[str | None] = mapped_column(CompressedText, nullable=True)
    # JSON-encoded list of strings. NULL for non-native sessions.
    terminal_launch_args: Mapped[str | None] = mapped_column(CompressedText, nullable=True)
    # Required when host_id is set; enforced by check constraint below.
    workspace: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    git_branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Live-state columns, written by the replica holding the runner
    # tunnel so any replica can serve the sidebar's live fields.
    # Writes must never bump conversations.updated_at (it drives
    # sidebar ordering).
    # Epoch seconds the bound runner's tunnel was last seen alive;
    # runner_online is derived from freshness (like host_is_live).
    runner_last_seen: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Last relay-observed turn status (enum_codecs.SESSION_LIVE_STATUS);
    # NULL means no relay has ever reported on this session.
    live_status: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    # Outstanding elicitation (approval-prompt) count; NULL = never written.
    pending_elicitation_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        CheckConstraint("kind IN (1, 2)", name="ck_conversation_metadata_kind"),
        CheckConstraint(
            "host_id IS NULL OR workspace IS NOT NULL",
            name="ck_conversation_metadata_workspace_required_for_host",
        ),
        # Supports list_conversations_by_runner_id and get_runner_ids.
        Index("ix_conversation_metadata_runner_id", "workspace_id", "runner_id", "id"),
    )


def conversation_title_hash(title: str) -> bytes:
    """Return the 16-byte truncated sha256 digest of a conversation title.

    ``ix_conversations_parent_title_unique`` keys on this instead of a wide
    512-char title prefix, so the unique index stays a fixed 16 bytes. The full
    title is hashed verbatim (no normalization), so two titles collide iff their
    digests do — uniqueness is exact and case-sensitive.
    """
    return hashlib.sha256(title.encode("utf-8")).digest()[:16]


def _default_conversation_title_hash(context: Any) -> bytes:
    """Column default: derive ``title_hash`` from the bound ``title`` on INSERT.

    ``title`` carries a ``server_default=""``, so an INSERT that omits it leaves
    ``title`` out of the bound params — fall back to ``""`` to match the stored
    value. Column defaults do not fire on UPDATE, so the store recomputes it
    explicitly on the rename paths.
    """
    return conversation_title_hash(context.get_current_parameters().get("title") or "")


class SqlConversation(ConversationBase):
    """
    SQLAlchemy model for the ``conversations`` table.

    Agent Platform (AP) fields for a conversation: identity, timestamps,
    title, hierarchy, the next_position allocator, and the agent binding
    (``agent_id`` + the ``session_overrides`` JSON blob). Omnigent
    operational state lives in :class:`SqlConversationMetadata`.

    :param id: Unique conversation identifier, e.g.
        ``"conv_e4f5a6b7..."``.
    :param created_at: Unix epoch seconds when the conversation was
        created.
    :param updated_at: Unix epoch seconds when the conversation was
        last updated (item append, title change, etc.).
    :param title: Human-readable title; empty string when untitled.
    :param parent_conversation_id: For Phase 4 named sub-agents,
        points at the parent conversation. ``None`` for top-level
        conversations.
    :param root_conversation_id: Id of the root (top-level)
        conversation in the spawn tree. Equal to ``id`` for
        top-level conversations.
    :param next_position: Monotonic allocator for the next item position.
    :param agent_id: Agent bound to the conversation at creation time.
        ``None`` for conversations created without an agent binding.
    :param session_overrides: Compact JSON blob of per-session config
        overrides (reasoning_effort, model_override,
        cost_control_mode_override, harness_override). ``None`` when the
        session uses all agent/spec defaults.
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
    id: Mapped[str] = mapped_column(Uuid16(), primary_key=True)
    created_at: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(768), nullable=False, server_default="")
    # Fixed-width sha256(title)[:16] mirror of ``title`` that the unique index
    # keys on instead of a wide title prefix. The ORM default stamps it on INSERT
    # and the store recomputes it on rename, so every app-created row has a hash;
    # it is nullable only so raw-SQL inserts (tests/tooling that bypass the ORM
    # default) don't have to supply it.
    title_hash: Mapped[bytes | None] = mapped_column(
        _CKSUM16, nullable=True, default=_default_conversation_title_hash
    )
    parent_conversation_id: Mapped[str | None] = mapped_column(
        Uuid16(),
        nullable=True,
    )
    root_conversation_id: Mapped[str] = mapped_column(
        Uuid16(),
        nullable=False,
    )
    # Monotonic allocator for the next item position in this conversation.
    next_position: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0)
    # Agent bound to this conversation at creation time. NULL for conversations
    # created without an agent binding. Indexed for the agent→conversation
    # reverse lookup and the list filters (agent_id / has_agent_id / agent_name).
    agent_id: Mapped[str | None] = mapped_column(Uuid16(), nullable=True)
    # Per-session config overrides packed as a compact JSON object, e.g.
    # ``{"model_override":"claude-opus-4-8","reasoning_effort":"high"}``. Keys:
    # reasoning_effort, model_override, cost_control_mode_override,
    # harness_override. NULL when the session uses all agent/spec defaults; only
    # set keys are stored. Never filtered in SQL — read and written whole with
    # the row (see the store's _encode/_decode_session_overrides).
    session_overrides: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Model Routing Agent approval state. Nullable so existing/manual sessions
    # preserve their current picker behavior until explicitly enabled.
    route_approval_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # Provenance for persisted model / harness / route choices. NULL keeps
    # legacy rows distinguishable; callers treat NULL + a choice as manual.
    routing_selection_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    omniroute_route_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    permission_mode: Mapped[str | None] = mapped_column(String(64), nullable=True)
    omniroute_requires_explicit_approval: Mapped[bool | None] = mapped_column(
        Boolean, nullable=True
    )
    # Whether the session is archived (hidden from the default sidebar). Lives
    # here on the AP table so list_conversations can filter it inline alongside
    # the created_at/updated_at sort keys, instead of pre-fetching ids from the
    # Omnigent metadata DB.
    archived: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )

    __table_args__ = (
        CheckConstraint(
            "routing_selection_source IS NULL OR "
            "routing_selection_source IN ('manual','route_approval')",
            name="ck_conversations_routing_selection_source",
        ),
        # No bare created_at/updated_at indexes: the sessions list is ACL-scoped
        # (id IN (...)) and resolves via the PK; the default sidebar (archived=
        # false, updated_at DESC) is served by the archived_updated index below.
        Index("ix_conversations_archived_updated", "workspace_id", "archived", "updated_at", "id"),
        Index(
            "ix_conversations_root_conversation_id",
            "workspace_id",
            "root_conversation_id",
            "id",
        ),
        # Agent→conversation reverse lookup and the agent_id / has_agent_id /
        # agent_name list filters. id trails to complete the PK (index-only).
        Index(
            "ix_conversations_agent_id",
            "workspace_id",
            "agent_id",
            "id",
        ),
        # Unique index on (parent_conversation_id, title_hash) prevents two
        # same-named children under the same parent. Keys on the fixed-width
        # title_hash rather than a wide title prefix. NULLs are distinct in a
        # unique index, so top-level conversations (NULL parent) are exempt.
        Index(
            "ix_conversations_parent_title_unique",
            "workspace_id",
            "parent_conversation_id",
            "title_hash",
            unique=True,
        ),
        # Composite index for child-session listing.
        Index(
            "idx_conversations_parent",
            "workspace_id",
            "parent_conversation_id",
            text("created_at DESC"),
            text("id DESC"),
        ),
    )


class SqlConversationItem(ConversationBase):
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
        Uuid16(),
        primary_key=True,
    )
    id: Mapped[str] = mapped_column(Uuid16(), primary_key=True)
    response_id: Mapped[str] = mapped_column(String(64))
    # In the PK so deployments can PARTITION BY (created_at) with pure DDL —
    # both PostgreSQL and MySQL require the partition key in the PK and in
    # every unique index. Immutable: items are insert/delete-only.
    created_at: Mapped[int] = mapped_column(Integer, primary_key=True)
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
        # Backs the per-conversation position-ordered scan (the dominant read).
        # Non-unique on purpose: the real position allocator is the next_position
        # counter advanced under _lock_conversation, which never reuses a
        # position; the DB is not relied on to enforce it (nothing catches a
        # collision). Being non-unique also means it needs no partition key, so
        # created_at is left out — the PK still carries it for partition-readiness.
        Index(
            "ix_conversation_items_conversation_id_position",
            "workspace_id",
            "conversation_id",
            "position",
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
        # Latest-message previews scan one type per conversation ordered by
        # position DESC (list_latest_message_items_for_conversations). Ordering
        # type before position lets the scan seek to (workspace_id,
        # conversation_id, type) and walk position DESC directly, avoiding a
        # heap recheck on type — which no other index covers.
        Index(
            "ix_conversation_items_conv_type_position",
            "workspace_id",
            "conversation_id",
            "type",
            text("position DESC"),
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


class SqlConversationLabel(ConversationBase):
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
        Uuid16(),
        primary_key=True,
    )
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(String(LABEL_VALUE_MAX_LEN))
    updated_at: Mapped[int] = mapped_column(Integer)


class SqlComment(OmnigentBase):
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
    id: Mapped[str] = mapped_column(Uuid16(), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(Uuid16())
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


class SqlPolicy(OmnigentBase):
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
    id: Mapped[str] = mapped_column(Uuid16(), primary_key=True)
    name: Mapped[str] = mapped_column(String(256))
    # sha256(name) — the value the name-uniqueness indexes key on instead of
    # the wide name column. Stamped from `name` on INSERT via the column
    # default; the store recomputes it on rename (defaults don't fire on UPDATE).
    name_cksum: Mapped[bytes] = mapped_column(_CKSUM32, default=_default_policy_name_cksum)
    # Nullable: NULL for server-wide default policies.
    session_id: Mapped[str | None] = mapped_column(
        Uuid16(),
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


class SqlHost(OmnigentBase):
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
    host_id: Mapped[str] = mapped_column(Uuid16(), primary_key=True)
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


class SqlUserDailyCost(OmnigentBase):
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


class SqlScheduledTask(OmnigentBase):
    """
    SQLAlchemy model for the ``scheduled_tasks`` table.

    A scheduled task is a saved, scheduled instruction that fires an agent
    session on a recurring schedule (``rrule``).

    :param id: UUID primary key stored as 16 raw bytes (see :class:`Uuid16`),
        surfaced as a bare 32-char hex string (no dashes).
    :param name: Human-readable task name, e.g. ``"nightly triage"``.
    :param prompt: The instruction dispatched to the agent on each firing.
    :param rrule: The required RFC 5545 recurrence rule for the recurring
        trigger, e.g. ``"FREQ=DAILY;BYHOUR=9;BYMINUTE=0"``. Evaluated in
        ``timezone``.
    :param owner_user_id: User the spawned session's ``LEVEL_OWNER`` grant is
        written for — who the run belongs to, e.g. ``"alice@example.com"``.
        ``None`` in single-user / OSS mode; the fire path resolves it to the
        reserved ``"local"`` user.
    :param agent_id: The agent bound to this task (relates to
        ``agents.id``). Cascade cleanup on agent deletion is application-owned
        — there is no DB-level foreign key (schema Rule R032).
    :param model_override: Per-task LLM model override, e.g.
        ``"claude-opus-4-7"``. ``None`` means use the agent default.
    :param reasoning_effort: Per-task reasoning-effort hint, e.g. ``"high"``.
        ``None`` means use the agent default.
    :param workspace: Absolute path on disk where a fired session's runner
        should start (the source repo / working dir). ``None`` when unset.
    :param base_branch: Git base ref a firing branches FROM when it creates a
        worktree at fire time (mirrors session-create's ``git.base_branch``
        input). Pairs with ``workspace``:
        ``workspace`` is where, ``base_branch`` is what to branch from. ``None``
        when unset. The per-run *output* branch is not stored on the definition.
    :param execution_target: Where a firing runs —
        ``connected_host``/``managed_sandbox``. ``connected_host`` resolves the
        owner's live host at fire time (see ``host_id``); ``managed_sandbox``
        provisions/adopts a sandbox at fire time. Stored as a stable int code
        (see omnigent.db.enum_codecs SCHEDULED_TASK_EXECUTION_TARGET); the store
        converts to/from the string name at the row↔entity boundary. Defaults to
        ``connected_host``.
    :param host_id: For ``execution_target=connected_host``, the specific host
        to run on (relates to ``hosts.host_id``; no DB foreign key, Rule R032).
        ``None`` means "the owner's freshest online host". Always ``None`` for
        ``managed_sandbox`` (the sandbox is provisioned/adopted under a
        deterministic id at fire time, so there is nothing to pin).
    :param timezone: IANA timezone the trigger is evaluated in, e.g.
        ``"America/Los_Angeles"``.
    :param state: Lifecycle state — ``active``/``paused``/``deleted``.
        The scheduler only dispatches ``active`` tasks.
        Stored as a stable int code (see omnigent.db.enum_codecs
        SCHEDULED_TASK_STATE); the store converts to/from the string name at the
        row↔entity boundary. Defaults to ``active``.
    :param last_run_at: Unix epoch seconds of the most recent firing, or
        ``None`` if it has never fired.
    :param last_run_conversation_id: The conversation created by the most recent
        firing (relates to ``conversations.id``). ``None`` if never fired or the
        referenced conversation was deleted (application-owned SET-NULL cleanup;
        no DB foreign key).
    :param created_at: Unix epoch seconds at row creation.
    :param updated_at: Unix epoch seconds of the last write, or ``None`` if the
        row has never been updated.
    """

    __tablename__ = "scheduled_tasks"

    # Tenant partition key: Databricks workspace id owning this row (0 = default). Part of the PK.
    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    id: Mapped[str] = mapped_column(Uuid16, primary_key=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    # Opaque free text, never SQL-queried — stored compressed (CompressedText).
    prompt: Mapped[str] = mapped_column(CompressedText, nullable=False)
    # RFC 5545 recurrence rule, e.g. "FREQ=DAILY;BYHOUR=9;BYMINUTE=0".
    rrule: Mapped[str] = mapped_column(String(512), nullable=False)
    # Session-owner identity: the spawned run's LEVEL_OWNER grant is written
    # for this user. Nullable — None in single-user/OSS mode (the fire path
    # resolves null to the reserved "local" user). String(128) to match
    # session_permissions.user_id (the column the LEVEL_OWNER grant is
    # written into) and every other user-identity column in this schema.
    owner_user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Relates to agents.id. No DB foreign key (Rule R032); cascade is app-owned.
    agent_id: Mapped[str] = mapped_column(Uuid16, nullable=False)
    # Per-task overrides — None means fall back to the agent default. Widths
    # mirror the matching conversations.* override columns.
    model_override: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reasoning_effort: Mapped[str | None] = mapped_column(String(32), nullable=True)
    workspace: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    # Git base ref a firing branches from when it creates a worktree at fire
    # time (mirrors session-create's git.base_branch input). None when unset.
    base_branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Where a firing runs, as a stable int code (see omnigent.db.enum_codecs
    # SCHEDULED_TASK_EXECUTION_TARGET: connected_host=1, managed_sandbox=2).
    # connected_host → resolve the owner's live host at fire time (see host_id);
    # managed_sandbox → provision/adopt a sandbox at fire time. Defaults to
    # connected_host so existing rows keep connected-host behavior. The store
    # converts to/from the string name at the row↔entity boundary.
    execution_target: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="1")
    # For execution_target=connected_host: the specific host to run on (relates
    # to hosts.host_id; No DB foreign key, Rule R032). None = "the owner's
    # freshest online host, whichever". Always None for managed_sandbox (the
    # sandbox is provisioned/adopted under a deterministic id at fire time, so
    # there is nothing to pin here).
    host_id: Mapped[str | None] = mapped_column(Uuid16, nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, server_default="UTC")
    # Enum stored as a stable int code (see omnigent.db.enum_codecs
    # SCHEDULED_TASK_STATE: active=1, paused=2, deleted=3). The
    # store converts to/from the string name at the row↔entity boundary.
    state: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="1")
    last_run_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Relates to conversations.id. No DB foreign key (Rule R032); the
    # application nulls this out when the referenced conversation is deleted.
    last_run_conversation_id: Mapped[str | None] = mapped_column(Uuid16, nullable=True)
    created_at: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        CheckConstraint("state IN (1, 2, 3)", name="ck_scheduled_tasks_state"),
        CheckConstraint("execution_target IN (1, 2)", name="ck_scheduled_tasks_execution_target"),
        Index("ix_scheduled_tasks_created_at", "workspace_id", "created_at", "id"),
        Index("ix_scheduled_tasks_owner_user_id", "workspace_id", "owner_user_id", "id"),
    )


class SqlScheduledTaskRun(OmnigentBase):
    """
    SQLAlchemy model for the ``scheduled_task_runs`` table.

    One row per firing of a scheduled task — the run history. Recorded and
    advanced by the scheduler as a firing moves through its lifecycle.

    :param id: UUID primary key stored as 16 raw bytes (see :class:`Uuid16`),
        surfaced as a bare 32-char hex string (no dashes).
    :param scheduled_task_id: The task this run belongs to (relates to
        ``scheduled_tasks.id``; also a :class:`Uuid16`). Indexed for per-task
        history listing. Cascade cleanup on task deletion is application-owned —
        no DB foreign key (Rule R032).
    :param conversation_id: The conversation created by this firing (relates to
        ``conversations.id``). ``None`` before dispatch, or after the referenced
        conversation is deleted (application-owned SET-NULL; no DB foreign key).
    :param status: Lifecycle state —
        ``scheduled``/``running``/``succeeded``/``failed``/``skipped``. Stored
        as a stable int code (see omnigent.db.enum_codecs
        SCHEDULED_TASK_RUN_STATUS); the store converts to/from the string name
        at the row↔entity boundary.
    :param scheduled_at: Unix epoch seconds the firing was scheduled for.
    :param fired_at: Unix epoch seconds dispatch actually began, or ``None`` if
        it has not fired yet.
    :param finished_at: Unix epoch seconds the run reached a terminal state, or
        ``None`` if still pending/running.
    :param error: Failure detail when ``status = 'failed'``; ``None`` otherwise.
    :param error_code: Short failure classification (e.g. ``"timeout"``,
        ``"rate_limited"``) for future retryable-vs-terminal retry logic;
        ``None`` unless ``status = 'failed'``.
    """

    __tablename__ = "scheduled_task_runs"

    # Tenant partition key: Databricks workspace id owning this row (0 = default). Part of the PK.
    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    id: Mapped[str] = mapped_column(Uuid16, primary_key=True)
    # Relates to scheduled_tasks.id. No DB foreign key (Rule R032); cascade is
    # app-owned.
    scheduled_task_id: Mapped[str] = mapped_column(Uuid16, nullable=False)
    # Relates to conversations.id. No DB foreign key; app nulls on delete.
    conversation_id: Mapped[str | None] = mapped_column(Uuid16, nullable=True)
    # Enum stored as a stable int code (see omnigent.db.enum_codecs
    # SCHEDULED_TASK_RUN_STATUS: scheduled=1, running=2, succeeded=3, failed=4,
    # skipped=5). The store converts to/from the string name at the
    # row↔entity boundary.
    status: Mapped[int] = mapped_column(SmallInteger)
    scheduled_at: Mapped[int] = mapped_column(Integer)
    fired_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    finished_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Opaque free-text error blob, never SQL-queried — stored compressed.
    error: Mapped[str | None] = mapped_column(CompressedText, nullable=True)
    # Short, queryable failure classification token (e.g. "timeout",
    # "rate_limited") for future retry logic. Bounded plain string, not a blob;
    # no CHECK constraint (no code taxonomy defined yet).
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN (1, 2, 3, 4, 5)",
            name="ck_scheduled_task_runs_status",
        ),
        Index(
            "ix_scheduled_task_runs_scheduled_task_id",
            "workspace_id",
            "scheduled_task_id",
            "scheduled_at",
            "id",
        ),
    )


class SqlRoutingProposal(OmnigentBase):
    """Immutable routing-agent recommendation for one eligible user turn."""

    __tablename__ = "routing_proposals"

    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    id: Mapped[str] = mapped_column(Uuid16(), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(Uuid16(), nullable=False)
    elicitation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_message_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    user_message_excerpt: Mapped[str] = mapped_column(Text, nullable=False)
    user_message_chars: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_types_json: Mapped[str] = mapped_column(Text, nullable=False)
    original_harness: Mapped[str | None] = mapped_column(String(64), nullable=True)
    original_provider: Mapped[str | None] = mapped_column(String(128), nullable=True)
    original_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    original_route_id: Mapped[str] = mapped_column(String(64), nullable=False)
    original_reasoning_effort: Mapped[str | None] = mapped_column(String(32), nullable=True)
    original_permission_mode: Mapped[str | None] = mapped_column(String(64), nullable=True)
    requires_explicit_approval: Mapped[bool] = mapped_column(Boolean, nullable=False)
    evaluator_route_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    evaluator_provider: Mapped[str | None] = mapped_column(String(128), nullable=True)
    evaluator_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    evaluator_billing_class: Mapped[str | None] = mapped_column(String(32), nullable=True)
    evaluator_fallback_used: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    evaluator_decision_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    evaluator_selection_strategy: Mapped[str | None] = mapped_column(String(64), nullable=True)
    proposal_payload_excerpt: Mapped[str] = mapped_column(Text, nullable=False)
    proposal_payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "elicitation_id", name="uq_routing_proposals_elicitation"
        ),
        Index(
            "ix_routing_proposals_conversation_created",
            "workspace_id",
            "conversation_id",
            "created_at",
            "id",
        ),
    )


class SqlRoutingDecision(OmnigentBase):
    """Immutable, idempotent user decision for a routing proposal."""

    __tablename__ = "routing_decisions"

    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    id: Mapped[str] = mapped_column(Uuid16(), primary_key=True)
    proposal_id: Mapped[str] = mapped_column(Uuid16(), nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    decision_request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    original_harness: Mapped[str | None] = mapped_column(String(64), nullable=True)
    original_provider: Mapped[str | None] = mapped_column(String(128), nullable=True)
    original_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    original_route_id: Mapped[str] = mapped_column(String(64), nullable=False)
    original_reasoning_effort: Mapped[str | None] = mapped_column(String(32), nullable=True)
    original_permission_mode: Mapped[str | None] = mapped_column(String(64), nullable=True)
    final_harness: Mapped[str | None] = mapped_column(String(64), nullable=True)
    final_provider: Mapped[str | None] = mapped_column(String(128), nullable=True)
    final_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    final_route_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    final_reasoning_effort: Mapped[str | None] = mapped_column(String(32), nullable=True)
    final_permission_mode: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    decision_payload_excerpt: Mapped[str] = mapped_column(Text, nullable=False)
    decision_payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "action IN ('approved','changed','declined')", name="ck_routing_decisions_action"
        ),
        CheckConstraint(
            "action = 'declined' OR final_route_id IS NOT NULL OR final_model IS NOT NULL",
            name="ck_routing_decisions_final_route",
        ),
        UniqueConstraint("workspace_id", "proposal_id", name="uq_routing_decisions_proposal"),
        Index(
            "ix_routing_decisions_created",
            "workspace_id",
            "created_at",
            "id",
        ),
    )


class SqlTaskRun(OmnigentBase):
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
    id: Mapped[str] = mapped_column(Uuid16(), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(Uuid16(), nullable=False)
    response_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    triggering_message_id: Mapped[str | None] = mapped_column(Uuid16(), nullable=True)
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
    routing_proposal_id: Mapped[str | None] = mapped_column(Uuid16(), nullable=True)
    routing_decision_id: Mapped[str | None] = mapped_column(Uuid16(), nullable=True)
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
    evaluation_attempt_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    evaluation_last_attempt_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    evaluation_next_retry_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    evaluation_error_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    evaluation_error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    evaluation_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    evaluation_requested_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
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
        CheckConstraint(
            "evaluation_status IN "
            "('not_requested','pending','completed','deferred','skipped','failed')",
            name="ck_task_runs_evaluation_status",
        ),
        CheckConstraint(
            "evaluation_attempt_count >= 0",
            name="ck_task_runs_evaluation_attempt_count",
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
        Index(
            "ix_task_runs_evaluation_due",
            "workspace_id",
            "evaluation_status",
            "evaluation_next_retry_at",
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


class SqlTaskEvaluation(OmnigentBase):
    """
    SQLAlchemy model for the ``task_evaluations`` table.

    Append-only: each row is one immutable automated evaluation. LLM
    evaluations are produced by ``omnigent.server.task_outcome_evaluator``
    after the task reaches a terminal state. A row exists only after the
    fixed evaluator returned valid structured output with verified provenance;
    request failures remain on ``task_runs`` as deferred or failed lifecycle
    metadata.

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
        ``inconclusive``. ``inconclusive`` means the evaluator successfully
        judged that the available evidence was insufficient.
    :param confidence: 0.0–1.0 confidence from the LLM. ``None``
        when the evaluator didn't produce one (deterministic / failed).
    :param quality_score: 1–5 from the LLM. ``None`` when not
        produced.
    :param proposed_task_family: Family the evaluator proposes.
    :param reasoning: Free-text reasoning from the evaluator.
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
    id: Mapped[str] = mapped_column(Uuid16(), primary_key=True)
    task_run_id: Mapped[str] = mapped_column(Uuid16(), nullable=False)
    evaluator_type: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    evaluator_provider: Mapped[str | None] = mapped_column(String(128), nullable=True)
    evaluator_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    evaluator_route_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    evaluator_fallback_used: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    evaluator_decision_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
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
        UniqueConstraint(
            "workspace_id",
            "task_run_id",
            name="uq_task_evaluations_run",
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


class SqlTaskReview(OmnigentBase):
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
    id: Mapped[str] = mapped_column(Uuid16(), primary_key=True)
    task_run_id: Mapped[str] = mapped_column(Uuid16(), nullable=False)
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
    source_evaluation_id: Mapped[str | None] = mapped_column(Uuid16(), nullable=True)
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
            "review_action IS NULL OR review_action IN "
            "('accepted','adjusted','declined','not_logged')",
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


class SqlLangfuseSyncOutbox(OmnigentBase):
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
    id: Mapped[str] = mapped_column(Uuid16(), primary_key=True)
    task_run_id: Mapped[str] = mapped_column(Uuid16(), nullable=False)
    task_evaluation_id: Mapped[str | None] = mapped_column(Uuid16(), nullable=True)
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


# ── Oversight Autopilot v1: issue-run persistence (issue #18) ───


class SqlIssueRun(OmnigentBase):
    """
    SQLAlchemy model for the ``issue_runs`` table.

    One row per Autopilot run on a (repository, issue_number) pair.
    The row is the durable record of who is working on what, with a
    lease the store uses as the atomic claim lock (issue #18). The
    Autopilot scheduler / recovery sweep reads ``list_active`` and
    reaps expired leases; the runner transitions the row along the
    state ladder declared in ``omnigent.entities.issue_run``.

    :param id: Bare 32-char hex UUID primary key (see :class:`Uuid16`).
    :param repository: ``owner/repo`` slug, e.g.
        ``"Mortified2896/omnigent"``.
    :param issue_number: The GitHub issue number this run targets.
    :param state: Lifecycle state — ``queued``/``claiming``/``claimed``/
        ``in_progress``/``pr_ready``/``done``/``failed``/``abandoned``.
        Stored as a stable int code (see ``omnigent.db.enum_codecs``)
        so adding new states doesn't require a schema migration.
    :param lease_owner: Bare 32-char hex UUID of the runner holding
        the lease. ``None`` when the row is unclaimed (e.g. terminal
        or expired).
    :param lease_acquired_at: Unix epoch seconds the lease was taken.
    :param lease_expires_at: Unix epoch seconds the lease lapses;
        the recovery sweep considers ``lease_expires_at < now`` as
        available for re-claim.
    :param parent_session_id: The parent harness session id that
        spawned the worker (bare 32-char hex UUID). Optional.
    :param worker_session_id: The worker session id (bare 32-char hex
        UUID). Optional until the worker has started.
    :param branch: The branch the worker is using, e.g.
        ``"feat/issue-18-durable-run-persistence"``.
    :param worktree: Absolute path of the worker's worktree.
    :param head_sha: Last commit sha the worker pushed.
    :param pr_number: PR number this run opened.
    :param review_iteration: Current review / iteration counter.
    :param retry_count: Terminal retry counter (commits / pushes /
        migrations always retry-disabled).
    :param last_error: Most recent error message (truncated).
    :param last_error_code: Short classification, e.g.
        ``"timeout"``, ``"rate_limited"``.
    :param created_at: Unix epoch seconds the row was first written.
    :param updated_at: Unix epoch seconds the row was last touched.
    """

    __tablename__ = "issue_runs"

    # Tenant partition key: Databricks workspace id owning this row (0 = default).
    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    id: Mapped[str] = mapped_column(Uuid16, primary_key=True)
    repository: Mapped[str] = mapped_column(String(255), nullable=False)
    issue_number: Mapped[int] = mapped_column(Integer, nullable=False)
    # Lifecycle state as a stable int code — see
    # ``omnigent.db.enum_codecs.ISSUE_RUN_STATE`` for the mapping.
    state: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(Uuid16, nullable=True)
    lease_acquired_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    lease_expires_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    parent_session_id: Mapped[str | None] = mapped_column(Uuid16, nullable=True)
    worker_session_id: Mapped[str | None] = mapped_column(Uuid16, nullable=True)
    branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    worktree: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    head_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pr_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    review_iteration: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    retry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (
        # ``(repository, issue_number)`` is the natural key — only one
        # non-terminal row may exist for the pair at a time. The
        # uniqueness is enforced application-side (``try_claim``
        # refuses to insert a second non-terminal row); a DB
        # UNIQUE constraint would break terminal -> follow-up flows.
        Index("ix_issue_runs_repo_issue", "workspace_id", "repository", "issue_number"),
        # ``(state, lease_expires_at)`` — the recovery sweep's primary
        # scan: WHERE state IN (non-terminal) AND lease_expires_at IS NOT NULL
        # AND lease_expires_at < now ORDER BY lease_expires_at, id.
        Index("ix_issue_runs_state_lease", "state", "lease_expires_at", "id"),
        # ``(repository, state, updated_at)`` — the scheduler's "show
        # me active runs across the repo" listing.
        Index(
            "ix_issue_runs_repo_state_updated",
            "workspace_id",
            "repository",
            "state",
            "updated_at",
        ),
        CheckConstraint("issue_number > 0", name="ck_issue_runs_issue_number_positive"),
    )


class SqlIssueRunEvent(OmnigentBase):
    """
    SQLAlchemy model for the ``issue_run_events`` table.

    One row per state transition or external event recorded against
    an :class:`SqlIssueRun`. The event log is the source of truth for
    restart-safe checkpoint recovery: replaying the events in
    ``sequence`` order reconstructs the run's last-known state without
    relying on the snapshot row alone.

    :param id: Bare 32-char hex UUID primary key (see :class:`Uuid16`).
    :param run_id: Owning :class:`SqlIssueRun.id`.
    :param sequence: Monotonic per-run sequence number (1, 2, 3,
        ...). Assigned by the store; callers should leave ``None``
        on insert so the store can assign the next gap-free number.
    :param kind: Event kind literal (see
        :data:`omnigent.entities.issue_run.ISSUE_RUN_EVENT_KINDS`).
        Stored as a stable string so the API can decode without an
        enum lookup.
    :param from_state: Prior state (the ``state`` column was this
        when the event was emitted). ``None`` for creation events.
    :param to_state: New state after the event. ``None`` for pure
        observation events (e.g. ``ci_failed``).
    :param payload: Free-form JSON payload, e.g. CI url, comment
        id, log excerpt. Stored as compressed text.
    :param created_at: Unix epoch seconds the event row was written.
    """

    __tablename__ = "issue_run_events"

    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    id: Mapped[str] = mapped_column(Uuid16, primary_key=True)
    run_id: Mapped[str] = mapped_column(Uuid16, nullable=False)
    # Sequence is part of the PK so two events for the same run can
    # never share a number; the ``(workspace_id, run_id, sequence)``
    # triple is globally unique within a workspace.
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    from_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    payload: Mapped[str | None] = mapped_column(CompressedText, nullable=True)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (
        # Replay path: per-run events in sequence order.
        Index("ix_issue_run_events_run_sequence", "workspace_id", "run_id", "sequence"),
        # "Show me every ci_failed event" query.
        Index("ix_issue_run_events_kind_created", "workspace_id", "kind", "created_at"),
    )
