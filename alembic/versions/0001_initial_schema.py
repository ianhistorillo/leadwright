"""Initial schema: companies, people, email_verifications, jobs, job_items.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-07-24 09:30:00
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial_schema"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "companies",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("website", sa.String(length=512), nullable=True),
        sa.Column("domain", sa.String(length=255), nullable=False),
        sa.Column("apollo_id", sa.String(length=128), nullable=True),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default="pending",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("domain", name="uq_companies_domain"),
    )
    op.create_index("ix_companies_status", "companies", ["status"], unique=False)

    op.create_table(
        "jobs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default="pending",
            nullable=False,
        ),
        sa.Column(
            "total_companies",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "processed_companies",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "successful_companies",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "failed_companies",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_jobs_status", "jobs", ["status"], unique=False)

    op.create_table(
        "people",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("company_id", sa.String(length=36), nullable=False),
        sa.Column("apollo_person_id", sa.String(length=128), nullable=True),
        sa.Column("first_name", sa.String(length=128), nullable=True),
        sa.Column("last_name", sa.String(length=128), nullable=True),
        sa.Column("full_name", sa.String(length=255), nullable=True),
        sa.Column("job_title", sa.String(length=255), nullable=True),
        sa.Column("linkedin_url", sa.String(length=512), nullable=True),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("normalized_email", sa.String(length=320), nullable=True),
        sa.Column("apollo_email_status", sa.String(length=64), nullable=True),
        sa.Column(
            "final_status",
            sa.String(length=32),
            server_default="unverified",
            nullable=False,
        ),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("apollo_person_id", name="uq_people_apollo_person_id"),
        sa.UniqueConstraint("normalized_email", name="uq_people_normalized_email"),
    )
    op.create_index("ix_people_apollo_email_status", "people", ["apollo_email_status"])
    op.create_index("ix_people_company_id", "people", ["company_id"])
    op.create_index("ix_people_final_status", "people", ["final_status"])

    op.create_table(
        "job_items",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("company_id", sa.String(length=36), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default="pending",
            nullable=False,
        ),
        sa.Column(
            "retry_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "company_id", name="uq_job_items_job_company"),
    )
    op.create_index("ix_job_items_job_id", "job_items", ["job_id"])
    op.create_index("ix_job_items_status", "job_items", ["status"])

    op.create_table(
        "email_verifications",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("person_id", sa.String(length=36), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default="unverified",
            nullable=False,
        ),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "checked_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("raw_response", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["person_id"], ["people.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_email_verifications_email", "email_verifications", ["email"])
    op.create_index(
        "ix_email_verifications_person_id",
        "email_verifications",
        ["person_id"],
    )
    op.create_index("ix_email_verifications_status", "email_verifications", ["status"])


def downgrade() -> None:
    op.drop_index("ix_email_verifications_status", table_name="email_verifications")
    op.drop_index("ix_email_verifications_person_id", table_name="email_verifications")
    op.drop_index("ix_email_verifications_email", table_name="email_verifications")
    op.drop_table("email_verifications")

    op.drop_index("ix_job_items_status", table_name="job_items")
    op.drop_index("ix_job_items_job_id", table_name="job_items")
    op.drop_table("job_items")

    op.drop_index("ix_people_final_status", table_name="people")
    op.drop_index("ix_people_company_id", table_name="people")
    op.drop_index("ix_people_apollo_email_status", table_name="people")
    op.drop_table("people")

    op.drop_index("ix_jobs_status", table_name="jobs")
    op.drop_table("jobs")

    op.drop_index("ix_companies_status", table_name="companies")
    op.drop_table("companies")
