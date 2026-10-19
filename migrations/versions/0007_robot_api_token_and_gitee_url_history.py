"""Add robot API token and Gitee URL history.

Revision ID: 7
Revises: 6
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision: str = "7"
down_revision: str | None = "6"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None

_DESIRED_CONTAINER_COLUMNS = (
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
    "created_at",
    "expiration_hours",
    "deleted_at",
)

_REVISION_6_CONTAINER_COLUMNS = (
    "container_id",
    "service_id",
    "container_type",
    "image",
    "user_id",
    "gitee_user",
    "gitee_repository",
    "gitee_branch",
    "gitee_url",
    "authorize_general_account",
    "git_fin_status",
    "created_at",
    "expiration_hours",
    "deleted_at",
)


def upgrade() -> None:
    """Add the token column in-place logically and create URL history storage."""
    with op.batch_alter_table(
        "containers",
        recreate="always",
        partial_reordering=[_DESIRED_CONTAINER_COLUMNS],
    ) as batch_op:
        batch_op.add_column(
            sa.Column(
                "api_token",
                sa.Text(),
                nullable=False,
                server_default="",
            )
        )

    op.create_table(
        "gitee_url_history",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("gitee_url", sa.Text(), nullable=False),
        sa.Column("gitee_branch", sa.Text(), nullable=False, server_default=""),
        sa.UniqueConstraint(
            "user_id",
            "gitee_url",
            "gitee_branch",
            name="uq_gitee_url_history_user_url_branch",
        ),
        sqlite_autoincrement=True,
    )


def downgrade() -> None:
    """Remove URL history and the robot API token column."""
    op.drop_table("gitee_url_history")
    with op.batch_alter_table(
        "containers",
        recreate="always",
        partial_reordering=[_REVISION_6_CONTAINER_COLUMNS],
    ) as batch_op:
        batch_op.drop_column("api_token")
