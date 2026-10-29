"""Add people.source_job_id for new-people-only batch exports.

Revision ID: 0005_add_person_source_job_id
Revises: 0004_add_company_lookup_name
Create Date: 2026-08-03 21:35:00
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005_add_person_source_job_id"
down_revision: Union[str, Sequence[str], None] = "0004_add_company_lookup_name"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("people") as batch_op:
        batch_op.add_column(sa.Column("source_job_id", sa.String(length=36), nullable=True))
        batch_op.create_index("ix_people_source_job_id", ["source_job_id"])


def downgrade() -> None:
    with op.batch_alter_table("people") as batch_op:
        batch_op.drop_index("ix_people_source_job_id")
        batch_op.drop_column("source_job_id")
