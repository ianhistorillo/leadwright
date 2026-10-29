"""Add processing_errors table and jobs.options_json for resume continuity.

Revision ID: 0002_retry_resume
Revises: 0001_initial_schema
Create Date: 2026-07-24 18:50:00
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002_retry_resume"
down_revision: Union[str, Sequence[str], None] = "0001_initial_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("jobs") as batch_op:
        batch_op.add_column(sa.Column("options_json", sa.Text(), nullable=True))

    op.create_table(
        "processing_errors",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=True),
        sa.Column("job_item_id", sa.String(length=36), nullable=True),
        sa.Column("company_id", sa.String(length=36), nullable=True),
        sa.Column("provider", sa.String(length=64), nullable=True),
        sa.Column("operation", sa.String(length=64), nullable=False),
        sa.Column("error_type", sa.String(length=128), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column(
            "retryable",
            sa.Boolean(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("attempt", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["job_item_id"], ["job_items.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_processing_errors_job_id", "processing_errors", ["job_id"])
    op.create_index(
        "ix_processing_errors_job_item_id",
        "processing_errors",
        ["job_item_id"],
    )
    op.create_index(
        "ix_processing_errors_created_at",
        "processing_errors",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_processing_errors_created_at", table_name="processing_errors")
    op.drop_index("ix_processing_errors_job_item_id", table_name="processing_errors")
    op.drop_index("ix_processing_errors_job_id", table_name="processing_errors")
    op.drop_table("processing_errors")
    with op.batch_alter_table("jobs") as batch_op:
        batch_op.drop_column("options_json")
