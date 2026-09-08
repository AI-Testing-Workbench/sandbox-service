"""
管理员 REST API 的统一访问校验。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from application import admin_users
from domain.errors import UnauthorizedError
from interfaces.auth import get_operator_user_id

__all__ = [
    "require_admin_access",
]


def require_admin_access(
    operator_user_id: Annotated[str | None, Depends(get_operator_user_id)],
) -> None:
    """要求操作用户属于管理员清单；回环请求由通用依赖旁路。"""
    if operator_user_id is None:
        return
    if not admin_users.is_admin(operator_user_id):
        raise UnauthorizedError("未认证")
