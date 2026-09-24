"""
白名单用户管理应用层（v4 §12）。

- 数据保存于 `whitelist_users` 表，仅业务字段 `user_id`；管理 API 通过本模块复用这些能力。
- `is_whitelisted` 同时识别 `admin_users` 中的用户；管理员用户不复制到白名单表，但同样跳过创建约束。
"""

from __future__ import annotations

from application.blacklist import ensure_user_not_blacklisted
from domain.errors import InvalidArgumentError, UserNotFoundError
from infra.db import session_scope
from infra.repositories import AdminUserRepository, WhitelistUserRepository

__all__ = [
    "add_user",
    "remove_user",
    "list_users",
    "is_whitelisted"
]


async def add_user(user_id: str) -> bool:
    """新增白名单用户；已存在（含本事务待提交）返回 False。"""
    _validate(user_id)
    ensure_user_not_blacklisted(user_id)
    async with session_scope() as session:
        repo = WhitelistUserRepository(session)
        if await repo.exists(user_id):
            return False
        return repo.add(user_id)


async def remove_user(user_id: str) -> None:
    """删除白名单用户；用户不存在时抛出 404。"""
    _validate(user_id)
    ensure_user_not_blacklisted(user_id)
    async with session_scope() as session:
        repo = WhitelistUserRepository(session)
        if not await repo.exists(user_id):
            raise UserNotFoundError("用户不存在")
        await repo.delete(user_id)


async def list_users() -> list[str]:
    """列出全部白名单用户 ID。"""
    async with session_scope() as session:
        rows = await WhitelistUserRepository(session).list_all()
        return [row.user_id for row in rows]


async def is_whitelisted(user_id: str) -> bool:
    """判断用户是否属于有效白名单（显式白名单或管理员清单）。"""
    async with session_scope() as session:
        if await WhitelistUserRepository(session).exists(user_id):
            return True
        return await AdminUserRepository(session).exists(user_id)


def _validate(user_id: str) -> None:
    if not user_id or not user_id.strip():
        raise InvalidArgumentError("用户 ID 不能为空")
