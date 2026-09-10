"""
数据访问层（v4 §6.3）。

统一接口约定：
- `add(...) -> bool`：是否新插入记录；已存在（含本事务内待提交）不重复插入并返回 False。
- `get(pk) -> Optional[Entity]`：不存在返回 None。
- `exists(pk) -> bool`。
- `delete(pk) -> None`：幂等删除。
- `list_all() -> list[Entity]`：全部记录。
- 各仓库特有方法见类内说明。
- 创建限制（模式/数量）由应用层在**同一事务内**原子校验（变更 #3）；本层不设唯一约束兜底。
- 不携带运行时状态（OpenSandbox 读到状态不落库，见 v4 §6.1 原则）。
- Git 凭证 Repository 只保存密文；加密、解密和业务字段校验由 application 层负责。
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.orm import Session

from infra.orm import (
    AdminUserRow,
    Container,
    GitCredentialRow,
    SettingsRow,
    WhitelistUserRow,
)

__all__ = [
    "ContainerRepository",
    "SettingsRepository",
    "WhitelistUserRepository",
    "AdminUserRepository",
    "GitCredentialRepository",
]


class ContainerRepository:
    """`containers` 表数据访问。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, container: Container) -> bool:
        """新增记录，返回 True。"""
        self._session.add(container)
        return True

    def get(self, container_id: str) -> Optional[Container]:
        return self._session.get(Container, container_id)

    def exists(self, container_id: str) -> bool:
        return self.get(container_id) is not None

    def delete(self, container_id: str) -> None:
        """物理删除记录（彻底移除）；不存在则无操作。"""
        container = self.get(container_id)
        if container is not None:
            self._session.delete(container)

    def list_all(self) -> list[Container]:
        """全部记录（含业务已删除），供 Scheduler 扫描与补偿使用。"""
        return list(self._session.scalars(select(Container)))

    def list_all_ids(self) -> list[str]:
        """全部容器 ID（含业务已删除），供远端孤儿容器比对使用。"""
        return list(self._session.scalars(select(Container.container_id)))

    def list_active(
        self,
        *,
        user_id: Optional[str] = None,
        gitee_user: Optional[str] = None,
        gitee_repository: Optional[str] = None,
        gitee_branch: Optional[str] = None,
        container_type: Optional[str] = None,
    ) -> list[Container]:
        """活跃记录（`deleted_at IS NULL`），查询条件 AND 组合，按创建时间倒序。"""
        stmt = select(Container).where(Container.deleted_at.is_(None))
        if user_id is not None:
            stmt = stmt.where(Container.user_id == user_id)
        if gitee_user is not None:
            stmt = stmt.where(Container.gitee_user == gitee_user)
        if gitee_repository is not None:
            stmt = stmt.where(Container.gitee_repository == gitee_repository)
        if gitee_branch is not None:
            stmt = stmt.where(Container.gitee_branch == gitee_branch)
        if container_type is not None:
            stmt = stmt.where(Container.container_type == container_type)
        stmt = stmt.order_by(Container.created_at.desc())
        return list(self._session.scalars(stmt))

    def count_active(
        self,
        user_id: Optional[str] = None,
        gitee_repository: Optional[str] = None,
        gitee_user: Optional[str] = None,
        container_type: Optional[str] = None,
    ) -> int:
        """活跃记录数（`deleted_at IS NULL`，不含业务已删除）；供应用层数量/模式限制校验。"""
        conditions: list[ColumnElement[bool]] = [Container.deleted_at.is_(None)]
        if user_id is not None:
            conditions.append(Container.user_id == user_id)
        if gitee_repository is not None:
            conditions.append(Container.gitee_repository == gitee_repository)
        if gitee_user is not None:
            conditions.append(Container.gitee_user == gitee_user)
        if container_type is not None:
            conditions.append(Container.container_type == container_type)
        stmt = select(func.count()).select_from(Container).where(*conditions)
        return int(self._session.execute(stmt).scalar_one())

    def business_delete(self, container_id: str, deleted_at: str) -> None:
        """业务删除：写 `deleted_at`，记录保留。"""
        container = self.get(container_id)
        if container is not None:
            container.deleted_at = deleted_at

    def business_restore(self, container_id: str, created_at: str, expiration_hours: int) -> None:
        """业务恢复：清除 `deleted_at`，重写 `created_at` 与 `expiration_hours`。"""
        container = self.get(container_id)
        if container is not None:
            container.deleted_at = None
            container.created_at = created_at
            container.expiration_hours = expiration_hours

    def update_expiration(self, container_id: str, expiration_hours: int) -> None:
        """设置业务有效时长：仅改 `expiration_hours`，不重置 `created_at`。"""
        container = self.get(container_id)
        if container is not None:
            container.expiration_hours = expiration_hours


class SettingsRepository:
    """`settings` 表数据访问（config.py 的默认镜像 / 数量限制读写经本仓储）。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, key: str) -> Optional[SettingsRow]:
        return self._session.get(SettingsRow, key)

    def exists(self, key: str) -> bool:
        return self.get(key) is not None

    def set(self, key: str, value: str) -> None:
        """写入或更新配置项。"""
        row = self.get(key)
        if row is None:
            self._session.add(SettingsRow(key=key, value=value))
        else:
            row.value = value

    def delete(self, key: str) -> None:
        row = self.get(key)
        if row is not None:
            self._session.delete(row)

    def list_all(self) -> list[SettingsRow]:
        return list(self._session.scalars(select(SettingsRow)))


class WhitelistUserRepository:
    """`whitelist_users` 表数据访问。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, user_id: str) -> bool:
        """新增白名单用户；已存在（含本事务内待提交）返回 False。"""
        if self._pending_contains(user_id):
            return False
        if self.exists(user_id):
            return False
        self._session.add(WhitelistUserRow(user_id=user_id))
        return True

    def get(self, user_id: str) -> Optional[WhitelistUserRow]:
        return self._session.get(WhitelistUserRow, user_id)

    def exists(self, user_id: str) -> bool:
        return self._pending_contains(user_id) or self.get(user_id) is not None

    def _pending_contains(self, user_id: str) -> bool:
        for row in self._session.new:
            if isinstance(row, WhitelistUserRow) and row.user_id == user_id:
                return True
        return False

    def delete(self, user_id: str) -> None:
        row = self.get(user_id)
        if row is not None:
            self._session.delete(row)

    def list_all(self) -> list[WhitelistUserRow]:
        return list(self._session.scalars(select(WhitelistUserRow)))


class AdminUserRepository:
    """`admin_users` 管理员清单表数据访问。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, user_id: str) -> bool:
        """新增管理员用户；已存在（含本事务内待提交）返回 False。"""
        if self._pending_contains(user_id):
            return False
        if self.exists(user_id):
            return False
        self._session.add(AdminUserRow(user_id=user_id))
        return True

    def get(self, user_id: str) -> Optional[AdminUserRow]:
        return self._session.get(AdminUserRow, user_id)

    def exists(self, user_id: str) -> bool:
        return self._pending_contains(user_id) or self.get(user_id) is not None

    def _pending_contains(self, user_id: str) -> bool:
        for row in self._session.new:
            if isinstance(row, AdminUserRow) and row.user_id == user_id:
                return True
        return False

    def delete(self, user_id: str) -> None:
        """删除管理员用户；不存在则无操作。"""
        row = self.get(user_id)
        if row is not None:
            self._session.delete(row)

    def list_all(self) -> list[AdminUserRow]:
        return list(self._session.scalars(select(AdminUserRow)))


class GitCredentialRepository:
    """`git_credentials` 用户级凭证数据访问。

    Repository 只保存和返回数据库中的密文；密码加密、解密和业务字段校验由应用层负责。
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, credential: GitCredentialRow) -> bool:
        """新增用户凭证；同一事务中已有相同用户时返回 False。"""
        if self.exists(credential.user_id):
            return False
        self._session.add(credential)
        return True

    def get(self, user_id: str) -> Optional[GitCredentialRow]:
        return self._session.get(GitCredentialRow, user_id)

    def exists(self, user_id: str) -> bool:
        return self._pending_get(user_id) is not None or self.get(user_id) is not None

    def upsert(self, credential: GitCredentialRow) -> GitCredentialRow:
        """按 `user_id` 插入或覆盖当前用户凭证，暂不提交事务。"""
        row = self._pending_get(credential.user_id)
        if row is None:
            row = self.get(credential.user_id)
        if row is None:
            self._session.add(credential)
            return credential

        existing: GitCredentialRow = row
        if existing is not credential:
            existing.type = credential.type
            existing.git_username = credential.git_username
            existing.git_email = credential.git_email
            existing.git_password = credential.git_password
        return existing

    def delete(self, user_id: str) -> None:
        """删除用户凭证；不存在时无操作。"""
        row = self.get(user_id)
        if row is not None:
            self._session.delete(row)

    def list_all(self) -> list[GitCredentialRow]:
        return list(self._session.scalars(select(GitCredentialRow)))

    def _pending_get(self, user_id: str) -> Optional[GitCredentialRow]:
        for row in self._session.new:
            if isinstance(row, GitCredentialRow) and row.user_id == user_id:
                return row
        return None
