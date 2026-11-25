"""记录容器自动续期次数。

版本 ID：12
前序版本：11
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision: str = "12"
down_revision: str | None = "11"
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
    "renew_count",
    "deleted_at",
    "stop_at",
    "file_share_key",
)


def upgrade() -> None:
    """在约定的物理列位置添加非空续期计数列。"""
    with op.batch_alter_table(
        "containers",
        recreate="always",
        partial_reordering=[_CONTAINER_COLUMNS],
    ) as batch_op:
        batch_op.add_column(
            sa.Column(
                "renew_count",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )


def downgrade() -> None:
    """本服务不支持数据库降级。"""
    raise NotImplementedError("本服务不支持数据库降级")
