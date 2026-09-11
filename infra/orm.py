"""
表定义（v4 §6.2）：`containers`、`settings`、`whitelist_users`、`admin_users`、
`git_credentials`；
另含由迁移维护的 `schema_version` 元数据表。

注：`containers` 表不设唯一约束；模式/数量限制在应用层（application）校验，
以便白名单用户跳过约束（变更 #3）。
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import Boolean, CheckConstraint, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

__all__ = [
    "Base",
    "Container",
    "SettingsRow",
    "WhitelistUserRow",
    "AdminUserRow",
    "GitCredentialRow",
    "SchemaVersionRow",
]


class Base(DeclarativeBase):
    """ORM 模型基类。"""


class Container(Base):
    """`containers` 容器业务数据（含 Gitee 仓库地址）。"""

    __tablename__ = "containers"
    __table_args__ = (
        CheckConstraint(
            "git_fin_status IS NULL OR git_fin_status IN "
            "('initialized', 'failed_timeout', 'failed_max_attempts', "
            "'failed_unexpected_state', 'failed_git', 'failed_service', "
            "'failed_container', 'failed_initialize', 'failed_user_cancelled')",
            name="ck_containers_git_fin_status",
        ),
        Index("ux_containers_service_id", "service_id", unique=True),
    )

    container_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    image: Mapped[str] = mapped_column(String(512))
    container_type: Mapped[str] = mapped_column(
        String(32),
        default="testagent_cloud",
        server_default="testagent_cloud",
    )

    user_id: Mapped[str] = mapped_column(String(128))
    service_id: Mapped[str] = mapped_column(Text, nullable=False)

    gitee_user: Mapped[str] = mapped_column(String(128))
    gitee_repository: Mapped[str] = mapped_column(String(128))
    gitee_branch: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    gitee_url: Mapped[str] = mapped_column(String(512), default="")
    authorize_general_account: Mapped[bool] = mapped_column(Boolean)
    git_fin_status: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[str] = mapped_column(String(32))
    expiration_hours: Mapped[int] = mapped_column(Integer)
    deleted_at: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)


class GitCredentialRow(Base):
    """`git_credentials` 用户级 Git 密码凭证（密码列保存密文）。"""

    __tablename__ = "git_credentials"
    __table_args__ = (
        CheckConstraint(
            "type = 'password'",
            name="ck_git_credentials_type_password",
        ),
    )

    # 字段顺序是跨软件 Git 凭证设计报告 §2.1 的数据库合同。
    user_id: Mapped[str] = mapped_column(Text, primary_key=True)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    git_username: Mapped[str] = mapped_column(Text, nullable=False)
    git_email: Mapped[str] = mapped_column(Text, nullable=False)
    git_password: Mapped[str] = mapped_column(Text, nullable=False)


class SettingsRow(Base):
    """`settings` 配置表（v4 §6.2.2）。"""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String)


class WhitelistUserRow(Base):
    """`whitelist_users` 白名单用户表（v4 §6.2.3）。"""

    __tablename__ = "whitelist_users"

    user_id: Mapped[str] = mapped_column(String(128), primary_key=True)


class AdminUserRow(Base):
    """`admin_users` 管理员清单表（变更 #13）。"""

    __tablename__ = "admin_users"

    #: 管理员用户 ID；管理员清单不参与鉴权或其他业务逻辑
    user_id: Mapped[str] = mapped_column(String(128), primary_key=True)


class SchemaVersionRow(Base):
    """数据库架构版本元数据；仅允许一条版本记录。"""

    __tablename__ = "schema_version"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_schema_version_single_row"),
        CheckConstraint("version >= 1", name="ck_schema_version_positive"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
