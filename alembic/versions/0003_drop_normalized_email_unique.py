"""Drop unique constraint on people.normalized_email.

Revision ID: 0003_drop_normalized_email_unique
Revises: 0002_retry_resume
Create Date: 2026-07-25 01:36:00
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003_drop_normalized_email_unique"
down_revision: Union[str, Sequence[str], None] = "0002_retry_resume"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # SQLite cannot DROP CONSTRAINT in place — rebuild via batch mode.
    with op.batch_alter_table("people") as batch_op:
        batch_op.drop_constraint("uq_people_normalized_email", type_="unique")
        batch_op.create_index("ix_people_normalized_email", ["normalized_email"])


def downgrade() -> None:
    with op.batch_alter_table("people") as batch_op:
        batch_op.drop_index("ix_people_normalized_email")
        batch_op.create_unique_constraint(
            "uq_people_normalized_email",
            ["normalized_email"],
        )
