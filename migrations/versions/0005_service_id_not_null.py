"""
回填历史 service_id 并收紧为非空字段。

Revision ID: 5
Revises: 4
"""

from __future__ import annotations

from uuid import uuid4

from alembic import op
import sqlalchemy as sa

revision: str = "5"
down_revision: str | None = "4"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    """为旧容器生成一次性 UUID service_id，然后禁止 NULL。"""
    connection = op.get_bind()
    container_ids = list(
        connection.execute(
            sa.text("SELECT container_id FROM containers WHERE service_id IS NULL")
        ).scalars()
    )
    for container_id in container_ids:
        connection.execute(
            sa.text(
                "UPDATE containers SET service_id = :service_id "
                "WHERE container_id = :container_id"
            ),
            {
                "service_id": str(uuid4()),
                "container_id": container_id,
            },
        )

    with op.batch_alter_table("containers") as batch_op:
        batch_op.alter_column(
            "service_id",
            existing_type=sa.Text(),
            nullable=False,
        )


def downgrade() -> None:
    """恢复 service_id 可空约束；保留已生成的值。"""
    with op.batch_alter_table("containers") as batch_op:
        batch_op.alter_column(
            "service_id",
            existing_type=sa.Text(),
            nullable=True,
        )
