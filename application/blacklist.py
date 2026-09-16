"""
沙盒服务内置用户黑名单。

黑名单是代码内固定策略，不从环境变量、数据库或 REST API 读取，也不提供运行时修改能力。
"""

from __future__ import annotations

from typing import Optional

from domain.errors import UserBlacklistedError

__all__ = [
    "BLACKLISTED_USER_IDS",
    "is_blacklisted",
    "ensure_user_not_blacklisted",
]


BLACKLISTED_USER_IDS = frozenset({"filebrowser"})


def is_blacklisted(user_id: Optional[str]) -> bool:
    """判断用户 ID 是否命中内置黑名单。比较保留用户 ID 的大小写语义。"""
    return isinstance(user_id, str) and user_id.strip() in BLACKLISTED_USER_IDS


def ensure_user_not_blacklisted(user_id: Optional[str]) -> None:
    """命中内置黑名单时抛出统一的 HTTP 403 业务异常。"""
    if is_blacklisted(user_id):
        raise UserBlacklistedError()
