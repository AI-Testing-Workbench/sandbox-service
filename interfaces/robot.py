"""Unauthenticated robot integration API."""

from __future__ import annotations

from fastapi import APIRouter, Response
from pydantic import BaseModel, ConfigDict, Field

from application import robot as robot_service
from interfaces.common import api_responses

router = APIRouter(prefix="/robot", tags=["招呼机器人 API"])


class SetApiTokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_token: str = Field(min_length=1, strict=True, description="云端沙箱中 TestAgent 的 API Token")


class GiteeUrlsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1, strict=True, description="用户 ID")


class GiteeUrlHistoryItem(BaseModel):
    gitee_url: str
    gitee_branch: str


class GiteeUrlsResponse(BaseModel):
    gitee_urls: list[GiteeUrlHistoryItem]


@router.post(
    "/api-token/{service_id}",
    status_code=204,
    response_model=None,
    operation_id="robot_set_api_token",
    responses=api_responses("成功 (无内容)", 204, 400, 404),
)
async def set_api_token(service_id: str, request: SetApiTokenRequest) -> Response:
    """为指定的 云端沙箱 设置 TestAgent 的 API Token。"""
    await robot_service.set_api_token(service_id, request.api_token)
    return Response(status_code=204)


@router.post(
    "/gitee-urls",
    response_model=GiteeUrlsResponse,
    status_code=200,
    operation_id="robot_list_gitee_urls",
    responses=api_responses("成功", 200, 400),
)
async def list_gitee_urls(request: GiteeUrlsRequest) -> GiteeUrlsResponse:
    """列举指定用户的码云记录。"""
    rows = await robot_service.list_gitee_urls(request.user_id)
    return GiteeUrlsResponse(
        gitee_urls=[
            GiteeUrlHistoryItem(
                gitee_url=row.gitee_url,
                gitee_branch=row.gitee_branch,
            )
            for row in rows
        ]
    )
