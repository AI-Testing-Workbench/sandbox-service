"""
持久化容器 Git service_id。

Revision ID: 4
Revises: 3
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision: str = "4"
down_revision: str | None = "3"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    """新增可空兼容旧记录的 service_id，并对新值建立唯一索引。"""
    with op.batch_alter_table("containers") as batch_op:
        batch_op.add_column(sa.Column("service_id", sa.Text(), nullable=True))
    op.create_index(
        "ux_containers_service_id",
        "containers",
        ["service_id"],
        unique=True,
    )


def downgrade() -> None:
    """移除容器 service_id 及其唯一索引。"""
    op.drop_index("ux_containers_service_id", table_name="containers")
    with op.batch_alter_table("containers") as batch_op:
        batch_op.drop_column("service_id")
