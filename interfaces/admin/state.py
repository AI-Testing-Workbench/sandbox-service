"""
管理员基础状态统计 API。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from application import container as container_service
from interfaces.admin.auth import require_admin_access
from interfaces.admin.schemas import AdminStateResponse
from interfaces.common import ErrorResponse, api_responses

__all__ = [
    "router",
]


router = APIRouter(
    prefix="/admin",
    tags=["管理员 API (容器操作)"],
    dependencies=[Depends(require_admin_access)],
    responses={401: {"model": ErrorResponse, "description": "未认证"}},
)


@router.get(
    "/state",
    response_model=AdminStateResponse,
    responses=api_responses("成功", 200),
)
def get_state() -> AdminStateResponse:
    """获取容器及用户清单基础统计。"""
    view = container_service.get_admin_state()
    return AdminStateResponse(
        container_count=view.container_count,
        whitelist_container_count=view.whitelist_container_count,
        admin_container_count=view.admin_container_count,
        whitelist_count=view.whitelist_count,
        admin_count=view.admin_count,
    )
