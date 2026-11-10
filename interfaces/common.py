"""
REST 接口公共模型与 OpenAPI 响应描述。
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from config import MAX_CONTAINER_EXPIRATION_HOURS, settings
from domain.models import ContainerType

__all__ = [
    "ErrorResponse",
    "ContainerCreateRequestBase",
    "ContainerRuntimeResponse",
    "ExpirationRequest",
    "ExpirationResponse",
    "api_responses",
    "admin_router_responses",
]


class ErrorResponse(BaseModel):
    """错误响应"""

    code: str = Field(description="错误码")
    message: str = Field(description="错误详情")


class ContainerCreateRequestBase(BaseModel):
    """云端沙箱创建参数"""

    user_id: str = Field(description="用户 ID")
    from_: str = Field(
        default="",
        alias="from",
        description="待继承的沙箱的服务 ID",
    )
    skip_git: bool = Field(
        default=False,
        description="是否跳过 git 验证",
    )
    type: ContainerType = Field(
        default=ContainerType.TESTAGENT_CLOUD,
        description=(
            "云端沙箱类型：testagent_cloud、testagent_cloud_chrome、autotest_cloud、"
            "evaluation_cloud。testagent_cloud_chrome 与 testagent_cloud 共用默认镜像；"
            "testagent_cloud_chrome 和 autotest_cloud 启用 Chrome/VNC。"
        ),
    )
    gitee_user: Optional[str] = Field(default=None, description="码云用户名")
    gitee_repository: Optional[str] = Field(default=None, description="码云仓库")
    gitee_branch: Optional[str] = Field(default=None, description="仓库分支 (可选)")
    gitee_url: Optional[str] = Field(default="", description="码云仓库地址前缀")
    authorize_general_account: Optional[bool] = Field(
        default=False,
        description="是否授权通用码云账户登录",
    )


class ContainerRuntimeResponse(BaseModel):
    """云端沙箱基本属性字段"""

    service_id: str = Field(description="云端沙箱 ID")
    type: str = Field(
        default=ContainerType.TESTAGENT_CLOUD.value,
        description="云端沙箱类型：testagent_cloud / testagent_cloud_chrome / autotest_cloud / evaluation_cloud",
    )
    novnc_url: Optional[str] = Field(
        default=None,
        description=(
            "Chrome/VNC 类型（testagent_cloud_chrome、autotest_cloud）的 noVNC 地址；"
            "SSH-only 类型为空，endpoint 不可用时也为空"
        ),
    )
    status: str = Field(description="云端沙箱状态")
    git_fin_status: str = Field(
        default="pending",
        description="Git 初始化最终状态",
    )
    endpoint: Optional[str] = Field(default=None, description="云端沙箱 SSH 访问端点")
    started_at: Optional[str] = Field(default=None, description="云端沙箱启动时间")
    expires_at: Optional[str] = Field(default=None, description="云端沙箱预计删除时间")
    api_token: str = Field(default="", description="云端沙箱中 TestAgent 的 API Token")
    cpu_usage: Optional[float] = Field(
        default=None,
        description="云端沙箱 CPU 使用率",
    )
    memory_usage: Optional[float] = Field(
        default=None,
        description="云端沙箱内存使用率",
    )


_EXPIRATION_REQUEST_SCHEMA_EXTRA: dict[str, Any] = {
    "examples": [{"expiration_hours": settings.container_default_expiration_hours}]
}


class ExpirationRequest(BaseModel):
    """设置云端沙箱创建后的剩余过期时间请求"""

    model_config = ConfigDict(json_schema_extra=_EXPIRATION_REQUEST_SCHEMA_EXTRA)

    expiration_hours: int = Field(
        ge=0,
        le=MAX_CONTAINER_EXPIRATION_HOURS,
        description="云端沙箱创建后的剩余过期时间，0 表示永不过期",
    )


class ExpirationResponse(BaseModel):
    """设置云端沙箱创建后的剩余过期时间响应"""

    service_id: str = Field(description="云端沙箱 ID")
    expires_at: Optional[str] = Field(description="预计创建后的业务删除时间，云端沙箱永不过期时为空")


_ERROR_LABELS = {
    400: "非法参数 / 配置错误",
    401: "未认证",
    403: "用户被禁止",
    404: "资源不存在",
    409: "业务冲突 / 重复新增",
    500: "服务内部错误",
    502: "外部服务错误",
}


def api_responses(
    success_description: str,
    success_code: int,
    *error_codes: int,
) -> dict[int | str, dict[str, Any]]:
    """生成统一的成功和错误响应文档。"""
    result: dict[int | str, dict[str, Any]] = {
        success_code: {"description": success_description},
        400: {"model": ErrorResponse, "description": "参数校验失败"},
        500: {"model": ErrorResponse, "description": _ERROR_LABELS[500]},
    }
    for code in error_codes:
        result[code] = {
            "model": ErrorResponse,
            "description": _ERROR_LABELS.get(code, "请求失败"),
        }
    return result


def admin_router_responses() -> dict[int | str, dict[str, Any]]:
    """提供管理员路由共享的认证错误响应文档。"""
    return {
        401: {"model": ErrorResponse, "description": "未认证"},
        403: {"model": ErrorResponse, "description": "用户被禁止"},
    }
