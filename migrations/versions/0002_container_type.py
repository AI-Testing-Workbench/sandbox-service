"""
容器表增加类型列，用于区分 testagent_cloud 与 autotest_cloud。

Revision ID: 2
Revises: 1
"""

from __future__ import annotations

# noinspection package-requirements
from alembic import op
import sqlalchemy as sa

revision: str = "2"
down_revision: str | None = "1"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    """为既有记录补全 `container_type='testagent_cloud'`（兼容历史数据）。"""
    op.add_column(
        "containers",
        sa.Column(
            "container_type",
            sa.String(length=32),
            nullable=False,
            server_default="testagent_cloud",
        ),
    )


def downgrade() -> None:
    op.drop_column("containers", "container_type")
