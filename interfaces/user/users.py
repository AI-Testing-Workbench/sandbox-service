"""
用户 REST API 用户信息端点。
"""

from __future__ import annotations

from fastapi import APIRouter

from application import admin_users, whitelist
from config import settings
from interfaces.common import api_responses
from interfaces.user.schemas import AdminCheckRequest, AdminCheckResponse

__all__ = [
    "router",
]


router = APIRouter(prefix="/user", tags=["用户 API"])


@router.post(
    "/check",
    response_model=AdminCheckResponse,
    status_code=200,
    responses=api_responses("成功", 200, 400, 403),
)
def check_admin(request: AdminCheckRequest) -> AdminCheckResponse:
    """查询指定用户是否为管理员及其容器创建限制模式。"""
    admin = admin_users.is_admin(request.user_id)
    limit = (
        "none"
        if admin or whitelist.is_whitelisted(request.user_id)
        else settings.container_create_limit_mode
    )
    return AdminCheckResponse(admin=admin, limit=limit)
