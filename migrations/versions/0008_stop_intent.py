"""Add durable Stop intent to containers.

Revision ID: 8
Revises: 7
"""

from __future__ import annotations

import asyncio
import logging

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect, text

revision: str = "8"
down_revision: str | None = "7"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None

logger = logging.getLogger(__name__)


def upgrade() -> None:
    """Add Stop intent and clean old business-deleted backends during migration."""
    bind = op.get_bind()
    columns = {column["name"] for column in inspect(bind).get_columns("containers")}
    if "stop_at" not in columns:
        op.add_column(
            "containers",
            sa.Column("stop_at", sa.String(length=32), nullable=True),
        )
    bind.execute(
        text(
            "UPDATE containers SET stop_at = deleted_at "
            "WHERE deleted_at IS NOT NULL AND stop_at IS NULL"
        )
    )
    # Commit the column and retry intents before contacting OpenSandbox.
    # Alembic records revision 8 only after this migration function succeeds.
    with op.get_context().autocommit_block():
        container_ids = list(
            bind.execute(
                text("SELECT container_id FROM containers WHERE deleted_at IS NOT NULL")
            ).scalars()
        )
        asyncio.run(_delete_legacy_backends(container_ids))
        bind.execute(
            text(
                "UPDATE containers SET stop_at = NULL "
                "WHERE deleted_at IS NOT NULL AND stop_at IS NOT NULL"
            )
        )


def downgrade() -> None:
    """Remove the durable Stop intent column."""
    op.drop_column("containers", "stop_at")


async def _delete_legacy_backends(container_ids: list[str]) -> None:
    """Delete pre-revision-8 business-deleted backends outside the DB transaction."""
    if not container_ids:
        return
    from infra.opensandbox.client import OpenSandboxClient, SandboxNotFoundError

    client = OpenSandboxClient()
    for container_id in container_ids:
        try:
            await client.delete(container_id)
        except SandboxNotFoundError:
            continue
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "revision 8 清理业务删除后端失败: %s: %s: %s",
                container_id,
                type(exc).__name__,
                exc,
            )
            raise RuntimeError(
                f"revision 8 外部容器清理失败: {container_id}"
            ) from exc
