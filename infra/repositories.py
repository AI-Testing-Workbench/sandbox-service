"""
数据访问层（v4 §6.3）。

统一接口约定：
- `add(...) -> bool`：是否加入当前 Session；pending 或已加载的记录返回 False。
- `get(pk) -> Optional[Entity]`：不存在返回 None。
- `exists(pk) -> bool`。
- `delete(pk) -> None`：幂等删除。
- `list_all() -> list[Entity]`：全部记录。
- 各仓库特有方法见类内说明。
- 创建限制（模式/数量）由应用层在**同一事务内**原子校验（变更 #3）；本层不设唯一约束兜底。
- 不携带运行时状态（OpenSandbox 读到状态不落库，见 v4 §6.1 原则）。
- 码云凭证 Repository 只保存密文；加密、解密和业务字段校验由 application 层负责。
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import ColumnElement, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from infra.orm import (
    AdminUserRow,
    Container,
    GiteeUrlHistoryRow,
    GitCredentialRow,
    SettingsRow,
    WhitelistUserRow,
)


def _identity_contains(session: AsyncSession, model: type[object], key: str) -> bool:
    """Check already-loaded persistent rows without issuing synchronous I/O."""
    return any(
        identity[0] is model and identity[1] == (key,)
        for identity in session.identity_map.keys()
    )


__all__ = [
    "ContainerRepository",
    "GiteeUrlHistoryRepository",
    "SettingsRepository",
    "WhitelistUserRepository",
    "AdminUserRepository",
    "GitCredentialRepository",
]


class ContainerRepository:
    """`containers` 表数据访问。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, container: Container) -> bool:
        """新增记录，返回 True。"""
        self._session.add(container)
        return True

    async def get(self, container_id: str) -> Optional[Container]:
        return await self._session.get(Container, container_id)

    async def get_by_service_id(self, service_id: str) -> Optional[Container]:
        """按持久化码云 service_id 查询容器记录。"""
        return await self._session.scalar(
            select(Container).where(Container.service_id == service_id)
        )

    async def update_api_token(self, service_id: str, api_token: str) -> bool:
        """Set the robot API token for a service ID; return whether it exists."""
        result = await self._session.execute(
            update(Container)
            .where(Container.service_id == service_id)
            .values(api_token=api_token)
        )
        return getattr(result, "rowcount", 0) == 1

    async def exists(self, container_id: str) -> bool:
        return await self.get(container_id) is not None

    async def delete(self, container_id: str) -> None:
        """物理删除记录（彻底移除）；不存在则无操作。"""
        container = await self.get(container_id)
        if container is not None:
            await self._session.delete(container)

    async def list_all(self) -> list[Container]:
        """全部记录（含业务已删除），供 Scheduler 扫描与补偿使用。"""
        return list(await self._session.scalars(select(Container)))

    async def list_all_ids(self) -> list[str]:
        """全部容器 ID（含业务已删除），供远端孤儿容器比对使用。"""
        return list(await self._session.scalars(select(Container.container_id)))

    async def list_active(
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
        return list(await self._session.scalars(stmt))

    async def count_active(
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
        result = await self._session.execute(stmt)
        return int(result.scalar_one())

    async def business_delete(self, container_id: str, deleted_at: str) -> None:
        """业务删除：持久化业务删除及远端删除意图，记录保留。"""
        container = await self.get(container_id)
        if container is not None:
            container.deleted_at = deleted_at
            container.stop_at = deleted_at

    async def business_restore(
        self,
        container_id: str,
        created_at: str,
        expiration_hours: int,
    ) -> None:
        """业务恢复：清除 `deleted_at`，重写 `created_at` 与 `expiration_hours`。"""
        container = await self.get(container_id)
        if container is not None:
            container.deleted_at = None
            container.stop_at = None
            container.created_at = created_at
            container.expiration_hours = expiration_hours

    async def set_stop_intent(self, container_id: str, stop_at: str) -> bool:
        """Persist an intentional Stop before deleting the remote sandbox."""
        result = await self._session.execute(
            update(Container)
            .where(Container.container_id == container_id, Container.deleted_at.is_(None))
            .values(stop_at=stop_at)
        )
        return getattr(result, "rowcount", 0) == 1

    async def clear_stop_intent(self, container_id: str) -> bool:
        """Clear a Stop/rebuild intent after the backend is running or deleted."""
        result = await self._session.execute(
            update(Container)
            .where(Container.container_id == container_id, Container.stop_at.is_not(None))
            .values(stop_at=None)
        )
        return getattr(result, "rowcount", 0) == 1

    async def replace_container_id(
        self,
        service_id: str,
        expected_container_id: str,
        new_container_id: str,
        *,
        restore: Optional[tuple[str, int]] = None,
    ) -> bool:
        """Atomically replace a backend ID while preserving its service record."""
        values: dict[str, object] = {
            "container_id": new_container_id,
            "stop_at": None,
        }
        if restore is not None:
            values.update(
                deleted_at=None,
                created_at=restore[0],
                expiration_hours=restore[1],
            )
        result = await self._session.execute(
            update(Container)
            .where(
                Container.service_id == service_id,
                Container.container_id == expected_container_id,
            )
            .values(**values)
        )
        return getattr(result, "rowcount", 0) == 1

    async def update_expiration(self, container_id: str, expiration_hours: int) -> None:
        """设置业务有效时长：仅改 `expiration_hours`，不重置 `created_at`。"""
        container = await self.get(container_id)
        if container is not None:
            container.expiration_hours = expiration_hours

    async def set_git_fin_status_if_unset(
        self,
        container_id: str,
        git_fin_status: str,
    ) -> tuple[bool, Optional[str]]:
        """仅在当前没有最终状态时写入，并返回 `(是否写入, 既有状态)`。"""
        result = await self._session.execute(
            update(Container)
            .where(
                Container.container_id == container_id,
                Container.git_fin_status.is_(None),
            )
            .values(git_fin_status=git_fin_status)
        )
        if getattr(result, "rowcount", 0) == 1:
            return True, None
        container = await self.get(container_id)
        if container is None:
            return False, None
        return False, container.git_fin_status

    async def clear_git_fin_status_for_recovery(self, container_id: str) -> bool:
        """清除启动恢复窗口内的旧 Git 终态，返回是否实际清除。"""
        result = await self._session.execute(
            update(Container)
            .where(
                Container.container_id == container_id,
                Container.git_fin_status.is_not(None),
            )
            .values(git_fin_status=None)
        )
        return getattr(result, "rowcount", 0) == 1


class GiteeUrlHistoryRepository:
    """`gitee_url_history` table persistence."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def replace(
        self,
        user_id: str,
        gitee_url: str,
        gitee_branch: str,
    ) -> GiteeUrlHistoryRow:
        """Replace a duplicate entry in the current transaction to refresh its ID."""
        await self._session.execute(
            delete(GiteeUrlHistoryRow).where(
                GiteeUrlHistoryRow.user_id == user_id,
                GiteeUrlHistoryRow.gitee_url == gitee_url,
                GiteeUrlHistoryRow.gitee_branch == gitee_branch,
            )
        )
        row = GiteeUrlHistoryRow(
            user_id=user_id,
            gitee_url=gitee_url,
            gitee_branch=gitee_branch or "",
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def list_for_user(self, user_id: str) -> list[GiteeUrlHistoryRow]:
        """Return a user's history from most recently inserted to oldest."""
        rows = await self._session.scalars(
            select(GiteeUrlHistoryRow)
            .where(GiteeUrlHistoryRow.user_id == user_id)
            .order_by(GiteeUrlHistoryRow.id.desc())
        )
        return list(rows)


class SettingsRepository:
    """`settings` 表数据访问（config.py 的默认镜像 / 数量限制读写经本仓储）。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, key: str) -> Optional[SettingsRow]:
        return await self._session.get(SettingsRow, key)

    async def exists(self, key: str) -> bool:
        return await self.get(key) is not None

    async def set(self, key: str, value: str) -> None:
        """写入或更新配置项。"""
        row = await self.get(key)
        if row is None:
            self._session.add(SettingsRow(key=key, value=value))
        else:
            row.value = value

    async def delete(self, key: str) -> None:
        row = await self.get(key)
        if row is not None:
            await self._session.delete(row)

    async def list_all(self) -> list[SettingsRow]:
        return list(await self._session.scalars(select(SettingsRow)))


class WhitelistUserRepository:
    """`whitelist_users` 表数据访问。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, user_id: str) -> bool:
        """新增白名单用户；本 Session 已有该用户时返回 False。"""
        if self._pending_contains(user_id) or _identity_contains(
            self._session, WhitelistUserRow, user_id
        ):
            return False
        self._session.add(WhitelistUserRow(user_id=user_id))
        return True

    async def get(self, user_id: str) -> Optional[WhitelistUserRow]:
        return await self._session.get(WhitelistUserRow, user_id)

    async def exists(self, user_id: str) -> bool:
        return self._pending_contains(user_id) or (await self.get(user_id)) is not None

    def _pending_contains(self, user_id: str) -> bool:
        for row in self._session.new:
            if isinstance(row, WhitelistUserRow) and row.user_id == user_id:
                return True
        return False

    async def delete(self, user_id: str) -> None:
        row = await self.get(user_id)
        if row is not None:
            await self._session.delete(row)

    async def list_all(self) -> list[WhitelistUserRow]:
        return list(await self._session.scalars(select(WhitelistUserRow)))


class AdminUserRepository:
    """`admin_users` 管理员清单表数据访问。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, user_id: str) -> bool:
        """新增管理员用户；本 Session 已有该用户时返回 False。"""
        if self._pending_contains(user_id) or _identity_contains(
            self._session, AdminUserRow, user_id
        ):
            return False
        self._session.add(AdminUserRow(user_id=user_id))
        return True

    async def get(self, user_id: str) -> Optional[AdminUserRow]:
        return await self._session.get(AdminUserRow, user_id)

    async def exists(self, user_id: str) -> bool:
        return self._pending_contains(user_id) or (await self.get(user_id)) is not None

    def _pending_contains(self, user_id: str) -> bool:
        for row in self._session.new:
            if isinstance(row, AdminUserRow) and row.user_id == user_id:
                return True
        return False

    async def delete(self, user_id: str) -> None:
        """删除管理员用户；不存在则无操作。"""
        row = await self.get(user_id)
        if row is not None:
            await self._session.delete(row)

    async def list_all(self) -> list[AdminUserRow]:
        return list(await self._session.scalars(select(AdminUserRow)))


class GitCredentialRepository:
    """`git_credentials` 用户级凭证数据访问。

    Repository 只保存和返回数据库中的密文；密码加密、解密和业务字段校验由应用层负责。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, credential: GitCredentialRow) -> bool:
        """新增用户凭证；本 Session 已有相同用户时返回 False。"""
        if self._pending_get(credential.user_id) is not None or _identity_contains(
            self._session, GitCredentialRow, credential.user_id
        ):
            return False
        self._session.add(credential)
        return True

    async def get(self, user_id: str) -> Optional[GitCredentialRow]:
        return await self._session.get(GitCredentialRow, user_id)

    async def exists(self, user_id: str) -> bool:
        return self._pending_get(user_id) is not None or (await self.get(user_id)) is not None

    async def upsert(self, credential: GitCredentialRow) -> GitCredentialRow:
        """按 `user_id` 插入或覆盖当前用户凭证，暂不提交事务。"""
        row = self._pending_get(credential.user_id)
        if row is None:
            row = await self.get(credential.user_id)
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

    async def delete(self, user_id: str) -> None:
        """删除用户凭证；不存在时无操作。"""
        row = await self.get(user_id)
        if row is not None:
            await self._session.delete(row)

    async def list_all(self) -> list[GitCredentialRow]:
        return list(await self._session.scalars(select(GitCredentialRow)))

    def _pending_get(self, user_id: str) -> Optional[GitCredentialRow]:
        for row in self._session.new:
            if isinstance(row, GitCredentialRow) and row.user_id == user_id:
                return row
        return None
