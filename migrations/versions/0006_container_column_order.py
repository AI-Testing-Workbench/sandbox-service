"""
调整容器表字段顺序。

Revision ID: 6
Revises: 5
"""

from __future__ import annotations

from alembic import op

revision: str = "6"
down_revision: str | None = "5"
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
    "authorize_general_account",
    "git_fin_status",
    "created_at",
    "expiration_hours",
    "deleted_at",
)

_REVISION_5_CONTAINER_COLUMNS = (
    "container_id",
    "image",
    "user_id",
    "gitee_user",
    "gitee_repository",
    "gitee_branch",
    "gitee_url",
    "authorize_general_account",
    "created_at",
    "expiration_hours",
    "deleted_at",
    "container_type",
    "git_fin_status",
    "service_id",
)


def upgrade() -> None:
    """重建容器表，使业务字段按约定顺序物理存储。"""
    with op.batch_alter_table(
        "containers",
        recreate="always",
        partial_reordering=[_DESIRED_CONTAINER_COLUMNS],
    ):
        pass


def downgrade() -> None:
    """恢复 revision 5 的容器表字段顺序。"""
    with op.batch_alter_table(
        "containers",
        recreate="always",
        partial_reordering=[_REVISION_5_CONTAINER_COLUMNS],
    ):
        pass
