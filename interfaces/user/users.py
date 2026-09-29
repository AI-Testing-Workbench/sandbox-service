"""
用户 REST API 用户信息端点。
"""

from __future__ import annotations

from fastapi import APIRouter

from application import admin_users, container, whitelist
from config import settings
from interfaces.common import api_responses
from interfaces.user.schemas import (
    AdminCheckRequest,
    AdminCheckResponse,
    MapContainerRequest,
    MapContainerResponse,
)

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
async def check_admin(request: AdminCheckRequest) -> AdminCheckResponse:
    """查询指定用户是否为管理员及其云端沙箱创建限制模式。"""
    admin = await admin_users.is_admin(request.user_id)
    limit = (
        "none"
        if admin or await whitelist.is_whitelisted(request.user_id)
        else settings.container_create_limit_mode
    )
    return AdminCheckResponse(admin=admin, limit=limit)


@router.post(
    "/map",
    response_model=MapContainerResponse,
    status_code=200,
    responses=api_responses("成功", 200, 400, 403, 404),
)
async def map_container(request: MapContainerRequest) -> MapContainerResponse:
    """查询云端沙箱服务 ID 对应的容器 ID。"""
    return MapContainerResponse(
        container_id=await container.map_container_id(request.user_id, request.service_id)
    )
