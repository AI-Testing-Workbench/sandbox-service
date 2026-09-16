"""
用户 REST API 请求 / 响应模型。

`POST /user/containers` 请求包含可选字段 `authorize_general_account`（bool，省略时不授权），并支持可选的
Gitee 信息；容器过期时间和资源限制由服务端管理。用户容器详情还返回 Gitee 用户和仓库信息。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from interfaces.common import (
    ContainerCreateRequestBase,
    ContainerRuntimeResponse,
    ErrorResponse,
)

__all__ = [
    "CreateContainerRequest",
    "CreateContainerResponse",
    "ContainerStatusResponse",
    "ContainerStatusListResponse",
    "ContainerIdsResponse",
    "AdminCheckRequest",
    "AdminCheckResponse",
    "ErrorResponse",
]


class CreateContainerRequest(ContainerCreateRequestBase):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "user_id": "10001",
                    "gitee_user": "test_name",
                    "gitee_repository": "test_project",
                    "gitee_branch": "develop",
                    "gitee_url": "https://github.com",
                    "authorize_general_account": True,
                    "type": "testagent_cloud",
                }
            ]
        }
    )


class CreateContainerResponse(BaseModel):
    container_id: str = Field(description="容器 ID")
    service_id: str = Field(description="本次 Git 初始化服务会话 ID")
    type: str = Field(default="testagent_cloud", description="容器类型: testagent_cloud / autotest_cloud")
    novnc_url: str | None = Field(
        default=None,
        description="autotest_cloud 容器的 noVNC 访问地址（宿主机浏览器打开可实时查看 Chrome）；其他类型为空",
    )
    status: str = Field(description="容器状态")
    endpoint: str | None = Field(default=None, description="容器 SSH 访问端点")
    started_at: str | None = Field(default=None, description="容器启动时间")
    expires_at: str | None = Field(default=None, description="容器预计删除时间")


class ContainerStatusResponse(ContainerRuntimeResponse):
    gitee_user: str = Field(description="容器所属的码云用户名")
    gitee_repository: str = Field(description="容器所属的码云仓库")


class ContainerStatusListResponse(BaseModel):
    containers: list[ContainerStatusResponse] = Field(description="容器状态列表（不含业务已删除容器）")


class ContainerIdsResponse(BaseModel):
    container_ids: list[str] = Field(description="容器 ID 列表")


class AdminCheckRequest(BaseModel):
    """查询用户管理员身份请求"""

    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1, description="用户 ID")


class AdminCheckResponse(BaseModel):
    """用户管理员身份及容器创建限制查询响应"""

    admin: bool = Field(description="是否为管理员")
    limit: Literal["user", "repository", "none"] = Field(
        description="容器创建限制模式"
    )
