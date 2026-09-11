"""
Git 初始化与凭证 API 合同（C53.8）。

本模块定义请求/响应 Schema、路径和请求头依赖；业务处理委托给 application 层服务。
响应模型不包含 `service_id` 或 `container_id`。
"""

from __future__ import annotations

from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict, Field

from application import git_api as git_service
from application.git_credentials import GitCredential
from domain.models import GitStatus
from interfaces.auth import get_operator_user_id
from interfaces.common import api_responses

__all__ = [
    "GitStateResponse",
    "GitReportRequest",
    "GitReportResponse",
    "GitCredentialSubmitRequest",
    "GitCredentialResponse",
    "GitCredentialConflictResponse",
    "router",
]


class GitStateResponse(BaseModel):
    """Git 详细初始化状态"""

    git_status: GitStatus = Field(description="Git 初始化详细状态")


class GitReportRequest(BaseModel):
    """Git 中间或最终状态上报请求"""

    model_config = ConfigDict(extra="forbid")

    git_status: GitStatus = Field(description="Git 初始化详细状态或最终状态")


class GitReportResponse(BaseModel):
    """状态上报成功响应"""

    git_status: GitStatus = Field(description="已接受的 Git 状态")


class GitCredentialSubmitRequest(BaseModel):
    """Git password 类型凭证提交请求"""

    model_config = ConfigDict(extra="forbid")

    type: Literal["password"] = Field(description="凭证类型")
    git_username: str = Field(min_length=1, description="Git 用户名")
    git_email: str = Field(min_length=1, description="Git 邮箱")
    git_password: str = Field(description="Git 密码；空值由业务层解释为取消")
    persist: bool = Field(description="是否写入数据库")


class GitCredentialResponse(BaseModel):
    """授权凭证领取响应"""

    type: Literal["password"] = Field(description="凭证类型")
    git_username: str = Field(description="Git 用户名")
    git_email: str = Field(description="Git 邮箱")
    git_password: str = Field(description="解密后的 Git 密码")


class GitCredentialConflictResponse(BaseModel):
    """凭证尚未可用或流程已失败时的 409 响应"""

    git_status: GitStatus = Field(description="当前 Git 详细状态")
    code: str = Field(description="错误码")
    message: str = Field(description="错误信息")


OperatorUserId = Annotated[str | None, Depends(get_operator_user_id)]

router = APIRouter(prefix="/git", tags=["Git 凭证 API"])


@router.get(
    "/{service_id}/state",
    response_model=GitStateResponse,
    responses=api_responses("成功", 200, 401, 404, 409),
)
def get_git_state(service_id: str, operator_user_id: OperatorUserId) -> GitStateResponse:
    """获取 Git 详细状态。"""
    return GitStateResponse(
        git_status=git_service.get_git_state(service_id, operator_user_id)
    )


@router.post(
    "/{service_id}/report",
    response_model=GitReportResponse,
    status_code=200,
    responses=api_responses("成功", 200, 400, 401, 404, 409, 502),
)
def report_git_state(
    service_id: str,
    request: GitReportRequest,
    operator_user_id: OperatorUserId,
) -> GitReportResponse:
    """统一接收 Git 中间状态和最终状态。"""
    return GitReportResponse(
        git_status=git_service.report_git_status(
            service_id,
            operator_user_id,
            request.git_status,
        )
    )


@router.get(
    "/{service_id}/credential",
    response_model=GitCredentialResponse,
    responses={
        **api_responses("成功", 200, 401, 404, 409),
        409: {
            "model": GitCredentialConflictResponse,
            "description": "凭证等待或 Git 初始化失败",
        },
    },
)
def get_git_credential(
    service_id: str,
    operator_user_id: OperatorUserId,
) -> GitCredentialResponse:
    """领取当前授权资源的 Git 凭证。"""
    credential = git_service.get_git_credential(service_id, operator_user_id)
    return GitCredentialResponse(
        type=cast(Literal["password"], credential.type),
        git_username=credential.git_username,
        git_email=credential.git_email,
        git_password=credential.git_password,
    )


@router.post(
    "/{service_id}/credential",
    status_code=204,
    response_model=None,
    responses=api_responses("成功 (无内容)", 204, 400, 401, 404, 409, 502),
)
def submit_git_credential(
    service_id: str,
    request: GitCredentialSubmitRequest,
    operator_user_id: OperatorUserId,
) -> Response:
    """提交 Git 凭证；响应不返回密码。"""
    git_service.submit_git_credential(
        service_id,
        operator_user_id,
        credential=GitCredential(
            type=request.type,
            git_username=request.git_username,
            git_email=request.git_email,
            git_password=request.git_password,
        ),
        persist=request.persist,
    )
    return Response(status_code=204)
