"""Add optional Autotest task fields to containers.

Revision ID: 10
Revises: 9
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision: str = "10"
down_revision: str | None = "9"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None

_CONTAINER_COLUMNS = (
    "container_id",
    "service_id",
    "container_type",
    "image",
    "user_id",
    "gitee_user",
    "gitee_repository",
    "gitee_branch",
    "gitee_url",
    "api_token",
    "authorize_general_account",
    "git_fin_status",
    "auto_test_id",
    "source_business_id",
    "task_type",
    "created_at",
    "expiration_hours",
    "deleted_at",
    "stop_at",
)


def upgrade() -> None:
    """Add nullable fields while preserving the containers column order."""
    with op.batch_alter_table(
        "containers",
        recreate="always",
        partial_reordering=[_CONTAINER_COLUMNS],
    ) as batch_op:
        batch_op.add_column(sa.Column("auto_test_id", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("source_business_id", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("task_type", sa.Integer(), nullable=True))


def downgrade() -> None:
    """Database downgrades are not supported by this service."""
    raise NotImplementedError("Database downgrades are not supported")
