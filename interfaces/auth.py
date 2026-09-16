"""
REST 请求身份信息的通用依赖。

`X-Operator-User-ID` 表示发起当前操作的用户 ID。具体权限由上层依赖
决定；本模块只负责提取请求头，并为回环请求提供本地运维旁路。
"""

from __future__ import annotations

import ipaddress
from typing import Annotated

from fastapi import Header, Request

from application.blacklist import ensure_user_not_blacklisted
from domain.errors import UnauthorizedError

__all__ = [
    "OPERATOR_USER_ID_HEADER",
    "get_operator_user_id",
]


OPERATOR_USER_ID_HEADER = "X-Operator-User-ID"


def get_operator_user_id(
    request: Request,
    operator_user_id: Annotated[
        str | None,
        Header(alias=OPERATOR_USER_ID_HEADER),
    ] = None,
) -> str | None:
    """读取操作用户 ID；回环请求跳过请求头认证。"""
    # 黑名单优先于回环旁路，避免携带被禁止的操作用户头时绕过策略。
    ensure_user_not_blacklisted(operator_user_id)
    if _is_loopback_client(request):
        return None
    if operator_user_id is None or not operator_user_id.strip():
        raise UnauthorizedError("未认证")
    return operator_user_id


def _is_loopback_client(request: Request) -> bool:
    client = request.client
    if client is None:
        return False
    try:
        return ipaddress.ip_address(client.host).is_loopback
    except ValueError:
        return False
