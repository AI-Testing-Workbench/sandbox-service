"""Rename the TestAgent default image settings key.

Revision ID: 9
Revises: 8
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision: str = "9"
down_revision: str | None = "8"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None

_OLD_KEY = "default_image"
_NEW_KEY = "default_image_testagent_cloud"


def upgrade() -> None:
    """Move the legacy TestAgent default image setting to its canonical key."""
    _rename_setting(_OLD_KEY, _NEW_KEY)


def downgrade() -> None:
    """Database downgrades are not supported by this service."""
    raise NotImplementedError("Database downgrades are not supported")


def _rename_setting(source_key: str, target_key: str) -> None:
    bind = op.get_bind()
    source_value = bind.execute(
        text("SELECT value FROM settings WHERE key = :key"), {"key": source_key}
    ).scalar_one_or_none()
    if source_value is None:
        return

    target_value = bind.execute(
        text("SELECT value FROM settings WHERE key = :key"), {"key": target_key}
    ).scalar_one_or_none()
    if target_value is None:
        bind.execute(
            text("UPDATE settings SET key = :target WHERE key = :source"),
            {"target": target_key, "source": source_key},
        )
        return

    if source_value != target_value:
        raise RuntimeError(
            "默认镜像设置键冲突，迁移已回滚："
            f"{source_key}={source_value!r} 与 {target_key}={target_value!r} 不一致"
        )

    bind.execute(
        text("DELETE FROM settings WHERE key = :key"), {"key": source_key}
    )
