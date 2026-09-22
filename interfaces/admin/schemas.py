"""
管理 REST API 请求 / 响应模型。
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from config import settings
from domain.models import ContainerType
from interfaces.common import (
    ContainerCreateRequestBase,
    ContainerRuntimeResponse,
    ErrorResponse,
    ExpirationRequest,
    ExpirationResponse,
)

__all__ = [
    "ErrorResponse",
    "ImageReferenceRequest",
    "ImageDeleteRequest",
    "SetDefaultImageRequest",
    "ImageListItem",
    "ImageListResponse",
    "DefaultImageResponse",
    "UserIdRequest",
    "UserIdsResponse",
    "UserMutationResponse",
    "AdminCreateContainerRequest",
    "AdminCreateContainerResponse",
    "AdminContainerResponse",
    "AdminContainerListResponse",
    "OrphanContainerListResponse",
    "OrphanContainerDeleteRequest",
    "PodDeleteRequest",
    "AdminStateResponse",
    "ExpirationRequest",
    "ExpirationResponse",
    "ContainerLimitRequest",
    "ContainerLimitResponse",
]


class ImageReferenceRequest(BaseModel):
    """镜像注册表相关请求"""

    full_name: str = Field(min_length=1, description="完整镜像名称")


class ImageDeleteRequest(ImageReferenceRequest):
    """删除镜像请求"""

    also_registry: bool = Field(default=True, description="是否同步删除注册表中的镜像")


class SetDefaultImageRequest(ImageReferenceRequest):
    """设置默认镜像请求"""

    type: ContainerType = Field(
        default=ContainerType.TESTAGENT_CLOUD,
        description="云端沙箱类型: testagent_cloud / autotest_cloud",
    )


class ImageListItem(BaseModel):
    """镜像基本属性字段"""

    id: str = Field(description="镜像 ID")
    full_name: str = Field(description="完整镜像名称")
    registry: str = Field(description="镜像注册表")
    namespace: str = Field(description="镜像命名空间")
    name: str = Field(description="镜像名称")
    version: str = Field(description="镜像版本")
    created_at: Optional[str] = Field(default=None, description="镜像创建时间")
    size: int = Field(description="镜像大小，单位为字节")
    status: str = Field(description="镜像状态")


class ImageListResponse(BaseModel):
    """镜像清单响应"""

    images: list[ImageListItem] = Field(description="本地镜像清单")


class DefaultImageResponse(BaseModel):
    """默认镜像响应"""

    full_name: Optional[str] = Field(default=None, description="当前的默认镜像，未设置时为空")
    type: str = Field(
        default=ContainerType.TESTAGENT_CLOUD.value,
        description="云端沙箱类型: testagent_cloud / autotest_cloud",
    )


class UserIdRequest(BaseModel):
    """用户清单变更请求"""

    user_id: str = Field(min_length=1, description="用户 ID")


class UserIdsResponse(BaseModel):
    """用户清单响应"""

    user_ids: list[str] = Field(description="用户 ID 列表")


class UserMutationResponse(BaseModel):
    """新增用户清单响应"""

    user_id: str = Field(description="用户 ID")


class AdminCreateContainerRequest(ContainerCreateRequestBase):
    """管理员创建云端沙箱请求"""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "user_id": "10001",
                    "image": "localhost:5000/testagent/app:v1",
                    "gitee_user": "test_name",
                    "gitee_repository": "test_project",
                    "gitee_branch": "develop",
                    "gitee_url": "https://github.com",
                    "expiration_hours": settings.container_default_expiration_hours,
                    "authorize_general_account": False,
                    "type": "testagent_cloud",
                    "cpu": 1,
                    "memory": 1,
                }
            ]
        }
    )

    image: Optional[str] = Field(
        default=None,
        min_length=1,
        description="完整镜像名称，省略时将使用默认镜像",
    )
    expiration_hours: Optional[int] = Field(
        default=settings.container_default_expiration_hours,
        ge=0,
        description="云端沙箱过期时间，0 表示永不过期",
    )
    cpu: Optional[float] = Field(default=None, gt=0, description="CPU 核数")
    memory: Optional[int] = Field(default=None, gt=0, description="内存大小，单位 Gi")


class AdminContainerResponse(ContainerRuntimeResponse):
    """完整云端沙箱信息"""

    image: str = Field(description="完整镜像名称")
    user_id: str = Field(description="用户 ID")
    gitee_user: str = Field(description="码云用户名")
    gitee_repository: str = Field(description="码云仓库")
    gitee_branch: Optional[str] = Field(default=None, description="码云分支，未设置时为空")
    gitee_url: str = Field(description="码云仓库地址")
    created_at: str = Field(description="云端沙箱创建时间")
    expiration_hours: int = Field(description="云端沙箱运行时长，单位小时")
    authorize_general_account: bool = Field(description="是否授权通用码云账户登录")
    deleted_at: Optional[str] = Field(default=None, description="业务删除时间，未业务删除时为空")
    business_deleted: bool = Field(description="是否已业务删除")


class AdminCreateContainerResponse(BaseModel):
    """管理员创建云端沙箱响应"""

    container_id: str = Field(description="云端沙箱 ID")
    type: str = Field(
        default=ContainerType.TESTAGENT_CLOUD.value,
        description="云端沙箱类型: testagent_cloud / autotest_cloud",
    )
    novnc_url: Optional[str] = Field(
        default=None,
        description="autotest_cloud 云端沙箱的 noVNC 访问地址；其他类型为空",
    )
    status: str = Field(description="云端沙箱状态")
    endpoint: Optional[str] = Field(default=None, description="云端沙箱 SSH 访问端点")
    started_at: Optional[str] = Field(default=None, description="云端沙箱启动时间")
    expires_at: Optional[str] = Field(default=None, description="云端沙箱预计删除时间")
    cpu_usage: Optional[float] = Field(default=None, description="云端沙箱 CPU 使用率")
    memory_usage: Optional[float] = Field(default=None, description="云端沙箱内存使用率")
    image: str = Field(description="完整镜像名称")
    user_id: str = Field(description="用户 ID")
    gitee_user: str = Field(description="码云用户名")
    gitee_repository: str = Field(description="码云仓库")
    gitee_branch: Optional[str] = Field(default=None, description="码云分支，未设置时为空")
    gitee_url: str = Field(description="码云仓库地址")
    created_at: str = Field(description="云端沙箱创建时间")
    expiration_hours: int = Field(description="云端沙箱运行时长，单位小时")
    authorize_general_account: bool = Field(description="是否授权通用码云账户登录")
    deleted_at: Optional[str] = Field(default=None, description="业务删除时间，未业务删除时为空")
    business_deleted: bool = Field(description="是否已业务删除")
    service_id: str = Field(description="云端沙箱初始化会话 ID")


class AdminContainerListResponse(BaseModel):
    """全部云端沙箱信息响应"""

    containers: list[AdminContainerResponse] = Field(description="全部云端沙箱完整信息")


class OrphanContainerListResponse(BaseModel):
    """孤儿云端沙箱 ID 列表响应"""

    container_ids: list[str] = Field(description="云端沙箱 ID 列表")


class OrphanContainerDeleteRequest(BaseModel):
    """孤儿云端沙箱批量删除请求"""

    model_config = ConfigDict(extra="forbid")

    container_ids: list[str] = Field(
        min_length=1,
        description="云端沙箱 ID 列表",
    )


class PodDeleteRequest(BaseModel):
    """按 K8s Pod 名称物理删除云端沙箱请求"""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {"pod_names": ["3f2a9c1b-5d4e-4a6f-8b1c-2e7d9a0f4b3c-abcde"]}
            ]
        },
    )

    pod_names: list[str] = Field(
        min_length=1,
        description="kubectl get pod -n <命名空间> 查出的 Pod 名称列表",
    )


class AdminStateResponse(BaseModel):
    """云端沙箱状态响应"""

    container_count: int = Field(description="活跃云端沙箱数")
    whitelist_container_count: int = Field(description="白名单用户云端沙箱数")
    admin_container_count: int = Field(description="管理员用户云端沙箱数")
    whitelist_count: int = Field(description="白名单用户数")
    admin_count: int = Field(description="管理员用户数")


class ContainerLimitRequest(BaseModel):
    """云端沙箱数量及资源限制变更请求"""

    model_config = ConfigDict(extra="forbid")

    container_limit: int = Field(ge=0, description="云端沙箱数量上限，0 表示取消数量限制")
    cpu: float = Field(gt=0, description="全部云端沙箱 CPU 限制，单位为核数")
    memory: int = Field(gt=0, description="全部云端沙箱内存限制，单位为 Gi")


class ContainerLimitResponse(BaseModel):
    """云端沙箱数量及资源限制响应"""

    container_limit: int = Field(description="当前云端沙箱数量限制")
    cpu: float = Field(description="全部云端沙箱 CPU 限制，单位为核数")
    memory: int = Field(description="全部云端沙箱内存限制，单位为 Gi")
