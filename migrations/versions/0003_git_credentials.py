"""
增加 Git 初始化最终状态和用户级凭证表。

Revision ID: 3
Revises: 2
"""

from __future__ import annotations

# noinspection package-requirements
from alembic import op
import sqlalchemy as sa

revision: str = "3"
down_revision: str | None = "2"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None

_GIT_FIN_STATUS_CHECK = (
    "git_fin_status IS NULL OR git_fin_status IN "
    "('initialized', 'failed_timeout', 'failed_max_attempts', "
    "'failed_unexpected_state', 'failed_git', 'failed_service', "
    "'failed_container', 'failed_initialize', 'failed_user_cancelled')"
)


def upgrade() -> None:
    """增加容器 Git 最终状态列和用户级 Git 凭证表。"""
    with op.batch_alter_table("containers") as batch_op:
        batch_op.add_column(sa.Column("git_fin_status", sa.Text(), nullable=True))
        batch_op.create_check_constraint(
            "ck_containers_git_fin_status",
            _GIT_FIN_STATUS_CHECK,
        )

    op.create_table(
        "git_credentials",
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("git_username", sa.Text(), nullable=False),
        sa.Column("git_email", sa.Text(), nullable=False),
        sa.Column("git_password", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "type = 'password'",
            name="ck_git_credentials_type_password",
        ),
        sa.PrimaryKeyConstraint("user_id"),
    )


def downgrade() -> None:
    """移除 Git 凭证表和容器 Git 最终状态列。"""
    op.drop_table("git_credentials")
    with op.batch_alter_table("containers") as batch_op:
        batch_op.drop_constraint("ck_containers_git_fin_status", type_="check")
        batch_op.drop_column("git_fin_status")
