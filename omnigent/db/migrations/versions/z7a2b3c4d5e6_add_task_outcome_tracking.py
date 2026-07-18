"""add task outcome tracking (task_runs / task_evaluations / task_reviews / langfuse_sync_outbox)

Revision ID: z7a2b3c4d5e6
Revises: z6a2b3c4d5e6
Create Date: 2026-07-10

First production-ready vertical slice of task-outcome tracking. After a routed
coding execution finishes, Omnigent now records the task, routing provenance,
execution result, and any objective evidence that can be observed without
parsing prose. It then runs an LLM evaluation through OmniRoute (via the same
``PolicyLLMClient`` the routing agent and policies already use) and surfaces a
human review card so an operator can confirm or correct the result.

This migration is purely data-collection. It does NOT:

- modify OmniRoute combo ordering
- enable ``evalRouting``
- update LKGP from human success judgments
- weight models by routing performance

The schema carries enough stable provenance (task_run_id, decision_id, route
id, selected model/provider, evaluator model/provider, etc.) to support those
later, but no caller reads it yet.

Tables
------
``task_runs``        — one row per routed coding execution attempt initiated by
                       a user message. Keyed on ``(workspace_id, id)``;
                       ``conversation_id`` + ``response_id`` link to existing
                       conversation_items. Captures routing snapshot at start
                       (immutable thereafter), execution status, durations, and
                       available objective evidence.

``task_evaluations`` — append-only automated evaluations. Keyed on
                       ``(workspace_id, id)``; ``task_run_id`` FK. LLM
                       evaluations record verdict/confidence/quality/family/
                       reasoning/evidence/unresolved_issues. An evaluation
                       failure is itself a row (``verdict='inconclusive'``)
                       so the schema is "append-only": no UPDATE.

``task_reviews``     — human verification, stored SEPARATELY from the LLM
                       evaluation. Keyed on ``(workspace_id, id)``;
                       ``task_run_id`` FK. Idempotent on
                       ``(workspace_id, task_run_id, created_by)`` so a
                       reviewer can re-submit and the row is replaced (small
                       implementation consistent with ``comments.update_comment``).

``langfuse_sync_outbox`` — transactional outbox for Langfuse score delivery.
                       Keyed on ``(workspace_id, id)``; ``task_run_id`` and
                       ``task_evaluation_id`` are denormalized for cheap
                       "what's still pending" joins. A bounded retry worker
                       (start/stop wired in ``server/app.py``) drains
                       ``status='pending'`` rows on a 30s tick. Langfuse
                       unavailable leaves rows pending and never loses data.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "z7a2b3c4d5e6"
down_revision: str | None = "z6a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── task_runs ──────────────────────────────────────────────────────────
    # Status enum is stored as a SMALLINT code (see db/enum_codecs.py
    # ``TASK_RUN_STATUS``): running=1, completed=2, failed=3, cancelled=4,
    # incomplete=5. CHECK admits the wider OpenAI-style vocabulary reserved
    # there so a future status widens without a migration.
    op.create_table(
        "task_runs",
        sa.Column(
            "workspace_id",
            sa.BigInteger(),
            primary_key=True,
            nullable=False,
            server_default="0",
        ),
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("conversation_id", sa.String(length=64), nullable=False),
        # ``response_id`` is the harness-side response / task id (the same id
        # that lives on conversation_items.response_id). Nullable because the
        # relay stamps it on the first ``response.in_progress``; until then
        # the row exists as a placeholder so the routing snapshot doesn't
        # get lost if the harness fails to start.
        sa.Column("response_id", sa.String(length=128), nullable=True),
        # Stable trigger id: the user message item that started the run.
        sa.Column("triggering_message_id", sa.String(length=64), nullable=True),
        # Project / repo identification, e.g. the workspace path or
        # git_branch recorded at task start. Stored as a single TEXT column
        # so a deployment that uses different identifiers can fill it in
        # however it likes without a schema change.
        sa.Column("project_path", sa.Text(), nullable=True),
        # Sanitized, bounded task description (truncated summary of the
        # triggering user message). The "sanitized" promise is enforced by
        # the writer, not the column — see task_outcome_store.
        sa.Column("task_description", sa.Text(), nullable=True),
        # Proposed family from the evaluator; nullable until LLM eval runs.
        sa.Column("proposed_task_family", sa.String(length=64), nullable=True),
        sa.Column("estimated_difficulty", sa.String(length=32), nullable=True),
        sa.Column("harness_id", sa.String(length=64), nullable=True),
        # Routing snapshot — immutable after the row is first written so
        # later PATCHes to the conversation don't retroactively rewrite
        # provenance. All columns nullable so partial / legacy states survive.
        sa.Column("requested_route_id", sa.String(length=64), nullable=True),
        sa.Column("selected_provider", sa.String(length=128), nullable=True),
        sa.Column("selected_model", sa.String(length=128), nullable=True),
        sa.Column("reasoning_effort", sa.String(length=32), nullable=True),
        sa.Column("permission_mode", sa.String(length=64), nullable=True),
        sa.Column("omniroute_decision_id", sa.String(length=128), nullable=True),
        sa.Column("selection_strategy", sa.String(length=64), nullable=True),
        sa.Column("billing_class", sa.String(length=32), nullable=True),
        sa.Column("fallback_used", sa.Boolean(), nullable=True),
        sa.Column(
            # SMALLINT code matching db/enum_codecs.TASK_RUN_STATUS.
            "terminal_status",
            sa.SmallInteger(),
            nullable=False,
            server_default="1",
        ),
        sa.Column("started_at", sa.BigInteger(), nullable=True),
        sa.Column("terminal_at", sa.BigInteger(), nullable=True),
        sa.Column("duration_ms", sa.BigInteger(), nullable=True),
        sa.Column("input_tokens", sa.BigInteger(), nullable=True),
        sa.Column("output_tokens", sa.BigInteger(), nullable=True),
        sa.Column("total_cost_usd", sa.Float(), nullable=True),
        # Final assistant response summary (truncated to a bounded length
        # by the writer) + changed-file / commit metadata when available.
        sa.Column("response_summary", sa.Text(), nullable=True),
        sa.Column("changed_files_json", sa.Text(), nullable=True),
        sa.Column("commit_sha", sa.String(length=64), nullable=True),
        # Failure payload from response.failed / response.incomplete.
        sa.Column("failure_error_code", sa.String(length=64), nullable=True),
        sa.Column("failure_error_message", sa.Text(), nullable=True),
        # Langfuse linkage (filled on first successful sync attempt).
        sa.Column("langfuse_trace_id", sa.String(length=64), nullable=True),
        sa.Column("langfuse_observation_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "terminal_status IN (1, 2, 3, 4, 5)",
            name="ck_task_runs_terminal_status",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "conversation_id"],
            ["conversations.workspace_id", "conversations.id"],
            name="fk_task_runs_conversation",
        ),
    )
    op.create_index(
        "ix_task_runs_conversation_started_at",
        "task_runs",
        ["workspace_id", "conversation_id", sa.text("started_at DESC"), "id"],
    )
    op.create_index(
        "ix_task_runs_response_id",
        "task_runs",
        ["workspace_id", "response_id", "id"],
    )
    op.create_index(
        "ix_task_runs_terminal_status",
        "task_runs",
        ["workspace_id", "terminal_status", "id"],
    )

    # ── task_evaluations ───────────────────────────────────────────────────
    # Evaluator type is a SMALLINT code: deterministic=1, llm=2. Verdict is
    # a string with a CHECK constraint to match the documented vocabulary
    # (``success``/``partial``/``failure``/``inconclusive``); string keeps
    # the human-readable name on the wire without round-tripping through
    # a codec on every API call.
    op.create_table(
        "task_evaluations",
        sa.Column(
            "workspace_id",
            sa.BigInteger(),
            primary_key=True,
            nullable=False,
            server_default="0",
        ),
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("task_run_id", sa.String(length=64), nullable=False),
        sa.Column("evaluator_type", sa.SmallInteger(), nullable=False),
        sa.Column("evaluator_provider", sa.String(length=128), nullable=True),
        sa.Column("evaluator_model", sa.String(length=128), nullable=True),
        sa.Column("evaluator_route_id", sa.String(length=64), nullable=True),
        sa.Column("verdict", sa.String(length=32), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("quality_score", sa.SmallInteger(), nullable=True),
        sa.Column("proposed_task_family", sa.String(length=64), nullable=True),
        sa.Column("reasoning", sa.Text(), nullable=True),
        sa.Column("evidence_json", sa.Text(), nullable=True),
        sa.Column("unresolved_issues_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("evaluator_type IN (1, 2)", name="ck_task_evaluations_type"),
        sa.CheckConstraint(
            "verdict IN ('success','partial','failure','inconclusive')",
            name="ck_task_evaluations_verdict",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "task_run_id"],
            ["task_runs.workspace_id", "task_runs.id"],
            name="fk_task_evaluations_run",
        ),
    )
    op.create_index(
        "ix_task_evaluations_run",
        "task_evaluations",
        ["workspace_id", "task_run_id", "created_at", "id"],
    )

    # ── task_reviews ───────────────────────────────────────────────────────
    # One human review per (task_run, reviewer). Unique on
    # (workspace_id, task_run_id, created_by) so a re-submit UPDATEs the
    # same row instead of appending a duplicate — matches the comment-store
    # idempotency pattern. ``created_by`` defaults to NULL for legacy /
    # single-user rows.
    op.create_table(
        "task_reviews",
        sa.Column(
            "workspace_id",
            sa.BigInteger(),
            primary_key=True,
            nullable=False,
            server_default="0",
        ),
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("task_run_id", sa.String(length=64), nullable=False),
        sa.Column("verdict", sa.String(length=32), nullable=False),
        sa.Column("quality_score", sa.SmallInteger(), nullable=True),
        sa.Column("final_task_family", sa.String(length=64), nullable=True),
        # Evaluator assessment: how accurate the LLM verdict was.
        # ``correct`` / ``partly_correct`` / ``incorrect`` / ``unsure``.
        sa.Column("evaluator_accuracy", sa.String(length=32), nullable=True),
        sa.Column("comments", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "verdict IN ('success','partial','failure','unsure','skipped')",
            name="ck_task_reviews_verdict",
        ),
        sa.CheckConstraint(
            "evaluator_accuracy IS NULL OR evaluator_accuracy IN "
            "('correct','partly_correct','incorrect','unsure')",
            name="ck_task_reviews_evaluator_accuracy",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "task_run_id"],
            ["task_runs.workspace_id", "task_runs.id"],
            name="fk_task_reviews_run",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "task_run_id",
            "created_by",
            name="uq_task_reviews_run_reviewer",
        ),
    )
    op.create_index(
        "ix_task_reviews_run",
        "task_reviews",
        ["workspace_id", "task_run_id", "updated_at", "id"],
    )

    # ── langfuse_sync_outbox ───────────────────────────────────────────────
    # Status enum: pending=1, delivered=2, dead=3, skipped=4. Rows are never
    # deleted — when Langfuse is unconfigured (``LANGFUSE_*`` env unset) the
    # adapter writes ``status='skipped'`` so the outbox stays queryable for
    # audit. ``payload_json`` is the bytes of a Langfuse score request body
    # (idempotency-keyed) so the worker can replay without recomputing.
    op.create_table(
        "langfuse_sync_outbox",
        sa.Column(
            "workspace_id",
            sa.BigInteger(),
            primary_key=True,
            nullable=False,
            server_default="0",
        ),
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("task_run_id", sa.String(length=64), nullable=False),
        sa.Column(
            "task_evaluation_id",
            sa.String(length=64),
            nullable=True,
        ),
        # Event type discriminator: ``task_root`` / ``llm_verdict`` /
        # ``human_verdict`` / ``human_quality`` / ``llm_evaluation_accuracy``.
        sa.Column("event_type", sa.String(length=64), nullable=False),
        # Stable, idempotent Langfuse score name. Mirrored verbatim to
        # ``payload_json['name']`` by the adapter; a retry of the same row
        # updates the same Langfuse score (its ``id`` field also carries
        # this value).
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("payload_json", sa.LargeBinary(), nullable=False),
        sa.Column("status", sa.SmallInteger(), nullable=False, server_default="1"),
        sa.Column("attempt_count", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("next_attempt_at", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("delivered_at", sa.BigInteger(), nullable=True),
        sa.CheckConstraint("status IN (1, 2, 3, 4)", name="ck_langfuse_outbox_status"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "task_run_id"],
            ["task_runs.workspace_id", "task_runs.id"],
            name="fk_langfuse_outbox_run",
        ),
    )
    op.create_index(
        "ix_langfuse_outbox_due",
        "langfuse_sync_outbox",
        ["workspace_id", "status", "next_attempt_at", "id"],
    )
    op.create_index(
        "ix_langfuse_outbox_run",
        "langfuse_sync_outbox",
        ["workspace_id", "task_run_id", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_langfuse_outbox_run", table_name="langfuse_sync_outbox")
    op.drop_index("ix_langfuse_outbox_due", table_name="langfuse_sync_outbox")
    op.drop_table("langfuse_sync_outbox")

    op.drop_index("ix_task_reviews_run", table_name="task_reviews")
    op.drop_table("task_reviews")

    op.drop_index("ix_task_evaluations_run", table_name="task_evaluations")
    op.drop_table("task_evaluations")

    op.drop_index("ix_task_runs_terminal_status", table_name="task_runs")
    op.drop_index("ix_task_runs_response_id", table_name="task_runs")
    op.drop_index("ix_task_runs_conversation_started_at", table_name="task_runs")
    op.drop_table("task_runs")
