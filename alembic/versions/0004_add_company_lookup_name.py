"""Add companies.lookup_name for CSV name-only lookup rows.

Revision ID: 0004_add_company_lookup_name
Revises: 0003_drop_normalized_email_unique
Create Date: 2026-07-30 13:20:00
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004_add_company_lookup_name"
down_revision: Union[str, Sequence[str], None] = "0003_drop_normalized_email_unique"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("companies") as batch_op:
        batch_op.add_column(sa.Column("lookup_name", sa.String(length=255), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("companies") as batch_op:
        batch_op.drop_column("lookup_name")
