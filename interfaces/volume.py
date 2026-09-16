"""
FileBrowser 卷功能状态 API。

该接口返回敏感的卷管理配置，因此沿用管理员操作用户认证；只读取已加载的
配置，不请求 FileBrowser 健康检查。
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from config import settings
from interfaces.admin.auth import require_admin_access
from interfaces.common import ErrorResponse, api_responses

__all__ = [
    "VolumeStatusResponse",
    "router",
]


class VolumeStatusResponse(BaseModel):
    """FileBrowser 卷功能配置状态。"""

    enabled: bool = Field(description="卷功能是否已启用")
    filebrowser_url: Optional[str] = Field(
        default=None,
        description="FileBrowser 地址",
    )
    filebrowser_api_key: Optional[str] = Field(
        default=None,
        description="FileBrowser API Token",
    )
    filebrowser_username: Optional[str] = Field(
        default=None,
        description="FileBrowser 用户名",
    )
    filebrowser_password: Optional[str] = Field(
        default=None,
        description="FileBrowser 密码",
    )


router = APIRouter(
    prefix="/volume",
    tags=["管理员 API (卷操作)"],
    dependencies=[Depends(require_admin_access)],
    responses={
        401: {"model": ErrorResponse, "description": "未认证"},
        403: {"model": ErrorResponse, "description": "用户被禁止"},
    },
)


@router.get(
    "/status",
    response_model=VolumeStatusResponse,
    responses=api_responses("成功", 200),
)
def get_volume_status() -> VolumeStatusResponse:
    """返回卷配置。"""
    if not settings.filebrowser_enabled:
        return VolumeStatusResponse(enabled=False)
    return VolumeStatusResponse(
        enabled=True,
        filebrowser_url=settings.filebrowser_url,
        filebrowser_api_key=settings.filebrowser_api_key,
        filebrowser_username=settings.filebrowser_username,
        filebrowser_password=settings.filebrowser_password,
    )
