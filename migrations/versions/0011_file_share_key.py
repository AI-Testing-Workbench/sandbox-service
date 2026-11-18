"""Add FileBrowser share keys to containers.

Revision ID: 11
Revises: 10
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

revision: str = "11"
down_revision: str | None = "10"
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
    "file_share_key",
)


def upgrade() -> None:
    """Add the key column, then backfill existing app-directory shares."""
    with op.batch_alter_table(
        "containers",
        recreate="always",
        partial_reordering=[_CONTAINER_COLUMNS],
    ) as batch_op:
        batch_op.add_column(
            sa.Column(
                "file_share_key",
                sa.Text(),
                nullable=False,
                server_default=sa.text("''"),
            )
        )

    from config import settings

    if not settings.filebrowser_enabled:
        return

    from infra.filebrowser import FileBrowserClient, FileBrowserNotFoundError

    bind = op.get_bind()
    client = FileBrowserClient()
    rows = bind.execute(
        text("SELECT container_id, user_id, service_id FROM containers")
    ).mappings()
    for row in rows:
        path = f"/{row['user_id']}/{row['service_id']}/app"
        try:
            resource = client.get_resource(path)
        except FileBrowserNotFoundError:
            continue
        if not resource.is_directory:
            continue

        share = client.create_share(path)
        if not share.hash.strip():
            raise RuntimeError(
                f"revision 11 FileBrowser 分享响应缺少有效 hash: {row['container_id']}"
            )
        bind.execute(
            text(
                "UPDATE containers SET file_share_key = :file_share_key "
                "WHERE container_id = :container_id"
            ),
            {
                "file_share_key": share.hash,
                "container_id": row["container_id"],
            },
        )


def downgrade() -> None:
    """Database downgrades are not supported by this service."""
    raise NotImplementedError("Database downgrades are not supported")
