"""
容器管理应用层（v4 §11、§14.5~§14.9）。

- 后端业务逻辑集中于此：创建（含创建限制原子校验）、操作（Start/Stop/Restart）、
  业务删除、恢复、立即删除、状态查询与剩余时间、日志查询、设置业务有效时长、业务条件查询、
  孤儿容器查询与清理。
- REST 接口仅承担必要输入/输出，不重复业务判断。
- 运行时状态来自 OpenSandbox（不落库）；业务数据写入 SQLite。
- 创建限制在进程内互斥锁 + 事务中执行（v4 §11.2、§6.3 语义；SQLite 单写者 + 进程互斥，单实例部署）。
"""

from __future__ import annotations

import asyncio
import logging
import math
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, AsyncIterator, NoReturn, Optional, cast
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from application.blacklist import ensure_user_not_blacklisted
from application.container_source import resolve_container_source
from application.git_api import report_git_status
from application.volume_copy import (
    copy_service_volumes_best_effort,
    restore_volume_directories_best_effort,
)
from application.volume_paths import (
    build_sandbox_volumes,
    build_volume_path_plan,
)
from application.volume_storage import (
    PreparedVolumeDirectories,
    prepare_volume_directories,
    rollback_volume_directories,
)
from config import Constants, MAX_CONTAINER_EXPIRATION_HOURS, settings
from config import get_container_count_limit as _cfg_count_limit
from config import get_default_image as _cfg_default_image
from config import get_container_resource_limits as _cfg_resource_limits
from config import set_container_count_limit as _cfg_set_count_limit
from config import set_container_resource_limits as _cfg_set_resource_limits
from config import get_delete_failed_container as _cfg_get_delete_failed_container
from config import set_delete_failed_container as _cfg_set_delete_failed_container
from domain.errors import (
    BusinessConflictError,
    ContainerNotFoundError,
    DefaultImageNotConfiguredError,
    ExternalDependencyError,
    InvalidArgumentError,
    LimitReachedError,
    VolumePathConflictError,
)
from domain.models import (
    ContainerStatus,
    ContainerType,
    GitStatus,
    add_hours_to_iso,
    get_public_git_fin_status,
    resolve_container_status,
)
from infra.db import session_scope
from infra.opensandbox.client import (
    OpenSandboxError,
    SandboxFailedError,
    SandboxNotFoundError,
    normalize_log_text,
)
from infra.opensandbox.types import (
    SandboxEndpoint,
    SandboxMetrics,
    SandboxStatus,
    SandboxVolume,
)
from infra.filebrowser import (
    FileBrowserClient,
    FileBrowserConflictError,
    FileBrowserNotFoundError,
)
from infra.orm import Container as ContainerRow
from infra.repositories import (
    AdminUserRepository,
    ContainerRepository,
    WhitelistUserRepository,
)
from application.git_sessions import get_git_session_store

if TYPE_CHECKING:
    from infra.opensandbox.client import OpenSandboxClient

logger = logging.getLogger(__name__)

__all__ = [
    "CreateContainerParams",
    "CreatedContainer",
    "ContainerStatusView",
    "ExpirationView",
    "AdminContainerView",
    "ContainerLimitView",
    "AdminStateView",
    "get_status",
    "get_container_logs",
    "create_container",
    "get_opensandbox_client",
    "get_filebrowser_client",
    "lifecycle_guard",
    "cleanup_volume_for_container",
    "delete_missing_container_record",
    "start",
    "stop",
    "restart",
    "business_delete",
    "business_delete_failed_container",
    "restore",
    "permanent_delete",
    "set_expiration",
    "map_container_id",
    "query_container_ids",
    "list_orphan_container_ids",
    "delete_orphan_containers",
    "cleanup_orphan_containers",
    "delete_sandboxes_by_pod_names",
    "list_admin_containers",
    "get_admin_container",
    "get_container_limit",
    "set_container_limit",
]

_TZ = ZoneInfo(Constants.TIMEZONE.value)

#: 创建限制临界区互斥（单实例部署，配合 SQLite 单写者保证原子性，v4 §13.1/§11.2）
_create_lock = asyncio.Lock()
#: 恢复与 Scheduler 物理清理共用的生命周期临界区（单实例部署）
_lifecycle_lock = asyncio.Lock()
_SOURCE_METADATA_KEY = "testagent-cloud"
_SOURCE_METADATA_VALUE = "true"
_CONTAINER_TYPE_METADATA_KEY = "container-type"
_TESTAGENT_CLOUD_TYPE_METADATA_KEY = "testagent-cloud-type"
_CONTAINER_START_WAIT_TIMEOUT_SECONDS = 120.0
_CONTAINER_START_POLL_INTERVAL_SECONDS = 0.25
#: 镜像内 noVNC/websockify 监听端口（tscode-server 镜像内固定 6080）
_NOVNC_PORT = 6080


# noinspection HttpUrlsUsage
def _build_novnc_url(endpoint: Optional[str]) -> Optional[str]:
    """由容器内 noVNC(6080) 的 endpoint 构造简洁的浏览器访问地址。

    `endpoint` 是 OpenSandbox 对容器内 6080 端口的解析结果：
    - K8s 直连：`<pod-ip>:6080` → 生成 `http://<pod-ip>:6080/vnc.html`
    - docker 直连：`host:port/proxy/6080` → 生成 `http://host:port/proxy/6080/vnc.html`
    - K8s 网关路由：`host/sandboxes/<id>/6080`（或带签名路径）→ 原路径下追加 `/vnc.html`

    host/port 直接从 6080 endpoint 解析（不再经 SSH/2222 或 /proxy 转发推导），
    不带任何查询参数。仅 autotest_cloud 场景使用。
    """
    if not endpoint:
        return None
    candidate = endpoint.strip()
    if not candidate:
        return None
    scheme = "http"
    lowered = candidate.lower()
    if lowered.startswith("https://"):
        scheme = "https"
        candidate = candidate[len("https://"):]
    elif lowered.startswith("http://"):
        candidate = candidate[len("http://"):]
    parsed = urlsplit(f"{scheme}://{candidate}")
    hostname = parsed.hostname
    if not hostname:
        return None
    port = parsed.port or (443 if scheme == "https" else 80)
    page_path = parsed.path.rstrip("/") + "/vnc.html"
    return urlunsplit((scheme, f"{hostname}:{port}", page_path, "", ""))


async def _autotest_novnc_url(container_id: str, container_type_value: str) -> Optional[str]:
    """autotest_cloud 容器取其自身 6080 endpoint 生成 noVNC 地址；其余返回 None。

    读取 6080 失败时返回 None（不影响 SSH/状态查询）。
    """
    if container_type_value != ContainerType.AUTOTEST_CLOUD.value:
        return None
    # noinspection broad-exception
    try:
        ep = await get_opensandbox_client().get_endpoint(container_id, _NOVNC_PORT)
    except Exception:
        return None
    endpoint = getattr(ep, "endpoint", None)
    if not endpoint:
        return None
    return _build_novnc_url(endpoint)


@asynccontextmanager
async def lifecycle_guard() -> AsyncIterator[None]:
    """串行化业务恢复与物理清理，避免两者对同一记录产生竞态。"""
    async with _lifecycle_lock:
        yield


async def delete_missing_container_record(container_id: str) -> None:
    """删除已确认不存在的远端容器对应的本地活跃记录。"""
    service_id: Optional[str] = None
    async with lifecycle_guard():
        async with session_scope() as session:
            repo = ContainerRepository(session)
            row = await repo.get(container_id)
            if row is None or row.deleted_at is not None or row.stop_at is not None:
                return
            service_id = row.service_id
            await cleanup_volume_for_container(row.user_id, row.service_id)
            await repo.delete(container_id)
    if service_id is not None:
        get_git_session_store().discard_session(service_id)
    logger.info("远端容器不存在，已删除数据库记录: %s", container_id)


async def cleanup_volume_for_container(user_id: str, service_id: str) -> None:
    """在线程池执行同步 FileBrowser 清理，避免阻塞 asyncio 事件循环。"""
    await asyncio.to_thread(_cleanup_volume_for_container_sync, user_id, service_id)


def _cleanup_volume_for_container_sync(user_id: str, service_id: str) -> None:
    """尽力清理物理删除容器对应的 FileBrowser service/用户目录。

    FileBrowser 清理失败不阻断调用方删除数据库记录；只有确认 service 目录
    删除成功后才会检查并删除空的用户父目录。
    """
    if not settings.filebrowser_enabled:
        return
    try:
        plan = build_volume_path_plan(user_id, service_id)
        client = get_filebrowser_client()
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "初始化 FileBrowser 卷清理失败: %s",
            type(exc).__name__,
        )
        return

    try:
        try:
            service_deleted = client.delete_resource(plan.service_path)
        except FileBrowserNotFoundError:
            service_deleted = True
        if not service_deleted:
            logger.error("FileBrowser service 目录删除未成功: %s", plan.service_path)
            return
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "FileBrowser service 目录清理失败: path=%s, %s",
            plan.service_path,
            type(exc).__name__,
        )
        return

    try:
        try:
            items = client.list_directory(plan.user_path)
        except FileBrowserNotFoundError:
            return
        if not items.is_empty:
            return
        try:
            user_deleted = client.delete_resource(plan.user_path)
        except FileBrowserNotFoundError:
            user_deleted = True
        if not user_deleted:
            logger.error("FileBrowser 空用户目录删除未成功: %s", plan.user_path)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "FileBrowser 用户目录清理失败: path=%s, %s",
            plan.user_path,
            type(exc).__name__,
        )


@dataclass(frozen=True)
class CreateContainerParams:
    user_id: str
    source_service_id: str = ""
    skip_git: bool = False
    explicit_fields: frozenset[str] = frozenset()
    image: Optional[str] = None
    gitee_user: Optional[str] = None
    gitee_repository: Optional[str] = None
    gitee_branch: Optional[str] = None
    gitee_url: Optional[str] = ""
    authorize_general_account: Optional[bool] = None
    expiration_hours: Optional[int] = None
    #: CPU 核数，无单位，例如 0.5 / 1
    cpu: Optional[float] = None
    #: 内存大小，单位固定 Gi，例如 1 / 2
    memory: Optional[int] = None
    #: 容器类型：testagent_cloud（默认，仅 SSH）或 autotest_cloud（启用 Chrome/VNC）
    container_type: ContainerType = ContainerType.TESTAGENT_CLOUD


@dataclass(frozen=True)
class CreatedContainer:
    container_id: str
    image: str
    container_type: ContainerType
    expiration_hours: int
    authorize_general_account: bool
    created_at: str
    status: ContainerStatus
    service_id: str


@dataclass(frozen=True)
class ContainerStatusView:
    container_id: str
    service_id: str
    status: ContainerStatus
    container_type: str = ContainerType.TESTAGENT_CLOUD.value
    endpoint: Optional[str] = None
    started_at: Optional[str] = None
    expires_at: Optional[str] = None
    cpu_usage: Optional[float] = None
    memory_usage: Optional[float] = None
    gitee_user: str = ""
    gitee_repository: str = ""
    gitee_url: str = ""
    gitee_branch: Optional[str] = None
    api_token: str = ""
    novnc_url: Optional[str] = None
    git_fin_status: str = "pending"


@dataclass(frozen=True)
class ExpirationView:
    container_id: str
    service_id: str
    expires_at: Optional[str]


@dataclass(frozen=True)
class AdminContainerView:
    """管理端容器完整视图，合并持久化业务字段和运行时字段。"""

    container_id: str
    service_id: str
    image: str
    user_id: str
    gitee_user: str
    gitee_repository: str
    gitee_branch: Optional[str]
    gitee_url: str
    api_token: str
    created_at: str
    expiration_hours: int
    authorize_general_account: bool
    status: ContainerStatus
    endpoint: Optional[str]
    started_at: Optional[str]
    expires_at: Optional[str]
    cpu_usage: Optional[float]
    memory_usage: Optional[float]
    deleted_at: Optional[str]
    business_deleted: bool
    container_type: str = ContainerType.TESTAGENT_CLOUD.value
    novnc_url: Optional[str] = None
    git_fin_status: str = "pending"


@dataclass(frozen=True)
class ContainerLimitView:
    """管理端容器数量及资源限制视图。"""

    container_limit: int
    cpu: float
    memory: int
    delete_failed_container: bool


@dataclass(frozen=True)
class AdminStateView:
    """管理员首页基础统计视图。"""

    container_count: int
    whitelist_container_count: int
    admin_container_count: int
    whitelist_count: int
    admin_count: int


# ---------------------------------------------------------------------------
# 客户端惰性单例（供测试注入）
# ---------------------------------------------------------------------------
_opensandbox_client: Optional[OpenSandboxClient] = None
_filebrowser_client: Optional[FileBrowserClient] = None


def get_opensandbox_client() -> OpenSandboxClient:
    """获取（惰性创建的）OpenSandbox 客户端；供业务与 Scheduler 复用，测试可注入替身。"""
    global _opensandbox_client
    client = _opensandbox_client
    if client is None:
        from infra.opensandbox.client import OpenSandboxClient

        client = OpenSandboxClient()
        _opensandbox_client = client
    return client


def get_filebrowser_client() -> FileBrowserClient:
    """获取惰性 FileBrowser 客户端；卷功能关闭时禁止调用。"""
    global _filebrowser_client
    if not settings.filebrowser_enabled:
        raise ExternalDependencyError("FileBrowser 卷功能未启用")
    client = _filebrowser_client
    if client is None:
        client = FileBrowserClient()
        _filebrowser_client = client
    return client


def _now() -> datetime:
    return datetime.now(_TZ)


def _now_iso() -> str:
    return _now().isoformat()


def _validate_expiration_hours(expiration_hours: int) -> None:
    if not 0 <= expiration_hours <= MAX_CONTAINER_EXPIRATION_HOURS:
        raise InvalidArgumentError(
            "expiration_hours 必须在 0 到 "
            f"{MAX_CONTAINER_EXPIRATION_HOURS} 之间"
        )


# ---------------------------------------------------------------------------
# 容器创建（T6.1 + T6.2）
# ---------------------------------------------------------------------------
# noinspection broad-exception
async def create_container(params: CreateContainerParams) -> CreatedContainer:
    """创建并自动启动容器（v4 §11.1）。

    - 镜像：`params.image` 为空则使用**该容器类型**的默认镜像；默认镜像未配置抛 400 语义错误。
    - 创建限制（模式 / 数量）在此校验，白名单用户跳过全部；并发通过进程互斥 + SQLite 单写者保证。
    - 容器名：随机字符串（仅表示容器本身，不承载业务信息）；SSH 端口固定 2222；
      环境变量注入 `TESTAGENT_CLOUD_SERVICE_USER` /
      `TESTAGENT_CLOUD_SERVICE_ID` / `TESTAGENT_CLOUD_SERVICE_URL` /
      `TESTAGENT_CLOUD_GITEE_USER` / `TESTAGENT_CLOUD_GITEE_REPOSITORY` /
      `TESTAGENT_CLOUD_GITEE_BRANCH`（为空也注入空值）
      及 `TESTAGENT_CLOUD_AUTHORIZE_GENERAL_ACCOUNT`（true/false），并注入
      `PIP_INDEX_URL` / `NPM_CONFIG_REGISTRY` 代理源；CPU / 内存可选覆盖默认资源限制。
      OpenSandbox metadata 额外注入 `testagent-cloud=true`（来源识别）与
      `container-type=<type>`、`testagent-cloud-type=<type>`（类型识别）。
    - 类型：`testagent_cloud`（默认）只起 SSH；`autotest_cloud` 额外注入
        `TESTAGENT_ENABLE_CHROME=1`（镜像内拉起 Chrome/VNC，sshd 仍可 SSH 连入），
        两者底层为同一镜像、仅启动方式不同。
    """
    _validate_required(params, validate_gitee=not params.source_service_id)
    if params.source_service_id:
        source = await resolve_container_source(
            params.source_service_id,
            user_id=params.user_id,
            gitee_user=params.gitee_user,
            gitee_repository=params.gitee_repository,
            gitee_branch=params.gitee_branch,
            gitee_url=params.gitee_url,
            authorize_general_account=params.authorize_general_account,
            expiration_hours=params.expiration_hours,
            container_type=params.container_type,
            explicit_fields=params.explicit_fields,
        )
        params = replace(
            params,
            gitee_user=source.gitee_user,
            gitee_repository=source.gitee_repository,
            gitee_branch=source.gitee_branch,
            gitee_url=source.gitee_url,
            authorize_general_account=source.authorize_general_account,
            expiration_hours=source.expiration_hours,
            container_type=source.container_type,
        )
        _validate_required(params)
    gitee_url = _normalise_optional_gitee_value(params.gitee_url)
    gitee_user = _normalise_optional_gitee_value(params.gitee_user)
    gitee_repository = _normalise_optional_gitee_value(params.gitee_repository)
    # 未指定时长时，使用创建后自动业务删除的默认有效时长。
    expiration_hours = params.expiration_hours if params.expiration_hours is not None \
        else settings.container_default_expiration_hours
    _validate_expiration_hours(expiration_hours)
    image = await _resolve_image(params)
    async with _create_lock:
        async with session_scope() as session:
            repo = ContainerRepository(session)
            await _check_creation_limits(
                repo,
                params.user_id,
                gitee_user,
                gitee_repository,
                params.container_type,
            )

        git_session = get_git_session_store().create_session(
            params.user_id,
            wait_for_container=True,
        )
        service_id = git_session.service_id
        if service_id is None:
            raise ExternalDependencyError("创建 Git 初始化会话失败")

        volume_client: Optional[FileBrowserClient] = None
        prepared_volume_directories: Optional[PreparedVolumeDirectories] = None
        volumes: Optional[list[SandboxVolume]] = None
        if settings.filebrowser_enabled:
            try:
                volume_plan = build_volume_path_plan(params.user_id, service_id)
                assert volume_plan is not None
                volume_client = get_filebrowser_client()
                assert volume_client is not None
                prepared_volume_directories = await asyncio.to_thread(
                    prepare_volume_directories,
                    volume_client,
                    volume_plan,
                    create_mount_directories=not bool(params.source_service_id),
                )
                volumes = build_sandbox_volumes(volume_plan, settings.pvc_name or "")
                if params.source_service_id:
                    try:
                        source_plan = build_volume_path_plan(
                            params.user_id,
                            params.source_service_id,
                        )
                        await asyncio.to_thread(
                            copy_service_volumes_best_effort,
                            volume_client,
                            source_plan,
                            volume_plan,
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "FileBrowser 来源卷路径处理失败，继续创建: source=%s, target=%s, error=%s",
                            params.source_service_id,
                            service_id,
                            type(exc).__name__,
                        )
                        await asyncio.to_thread(
                            restore_volume_directories_best_effort,
                            volume_client,
                            volume_plan,
                        )
            except Exception as exc:  # noqa: BLE001
                await asyncio.to_thread(
                    rollback_volume_directories,
                    volume_client,
                    prepared_volume_directories,
                )
                get_git_session_store().discard_session(service_id)
                _raise_volume_creation_error("准备卷目录", exc)

        container_type_value = params.container_type.value
        container_name = uuid.uuid4().hex[:12]
        try:
            opensandbox_client = get_opensandbox_client()
            create_kwargs = _sandbox_create_kwargs(
                name=container_name,
                service_id=service_id,
                user_id=params.user_id,
                container_type=container_type_value,
                gitee_user=gitee_user,
                gitee_repository=gitee_repository,
                gitee_branch=params.gitee_branch,
                gitee_url=gitee_url,
                authorize_general_account=params.authorize_general_account is True,
                resource_limits=await _resource_limits(params),
                volumes=volumes,
            )
            created = await opensandbox_client.create(image, **create_kwargs)
        except Exception as exc:
            # 创建响应丢失但远端已完成创建时，SDK 无法提供容器 ID；使用本次唯一的
            # metadata name 找回并回收这个无法直接寻址的远端资源。
            try:
                await _cleanup_failed_remote_create(container_name)
            finally:
                try:
                    await asyncio.to_thread(
                        rollback_volume_directories,
                        volume_client,
                        prepared_volume_directories,
                    )
                finally:
                    get_git_session_store().discard_session(service_id)
            if _is_image_not_found_error(exc):
                if params.image is None:
                    raise DefaultImageNotConfiguredError(
                        "默认镜像不存在，请联系管理员解决"
                    ) from exc
                raise InvalidArgumentError(
                    "镜像不存在，请检查镜像字段或者默认镜像设置后再创建云端沙箱"
                ) from exc
            _raise_backend_service_error("创建容器", exc)

        container_id = created.container_id
        try:
            get_git_session_store().bind_container_id(service_id, container_id)
        except Exception as exc:
            await _cleanup_created_container(
                container_id,
                service_id,
                prepared_volume_directories,
                volume_client,
            )
            raise ExternalDependencyError("绑定 Git 初始化会话失败") from exc
        created_at = _now_iso()
        try:
            async with session_scope() as session:
                ContainerRepository(session).add(
                    ContainerRow(
                        container_id=container_id,
                        service_id=service_id,
                        user_id=params.user_id,
                        container_type=container_type_value,
                        gitee_url=gitee_url,
                        gitee_user=gitee_user,
                        gitee_repository=gitee_repository,
                        gitee_branch=params.gitee_branch,
                        image=image,
                        created_at=created_at,
                        expiration_hours=expiration_hours,
                        authorize_general_account=params.authorize_general_account is True,
                    )
                )
        except Exception as exc:
            logger.exception("容器已创建但保存数据库记录失败: %s", container_id)
            await _cleanup_created_container(
                container_id,
                service_id,
                prepared_volume_directories,
                volume_client,
            )
            raise ExternalDependencyError("保存云端沙箱记录失败") from exc

        try:
            await _wait_for_container_running(opensandbox_client, container_id)
            get_git_session_store().mark_container_running(service_id)
        except Exception as exc:  # noqa: BLE001
            await _cleanup_created_container(
                container_id,
                service_id,
                prepared_volume_directories,
                volume_client,
            )
            # noinspection broad-exception
            try:
                async with session_scope() as session:
                    await ContainerRepository(session).delete(container_id)
            except Exception:  # noqa: BLE001
                logger.exception("容器未能启动且数据库记录清理失败: %s", container_id)
            _raise_backend_service_error("等待容器运行", exc)

        if params.skip_git:
            try:
                await report_git_status(
                    service_id,
                    params.user_id,
                    GitStatus.INITIALIZED,
                )
            except Exception as exc:  # noqa: BLE001
                logger.error("提交 Git 初始化最终状态失败: %s", type(exc).__name__)
                await _cleanup_created_container(
                    container_id,
                    service_id,
                    prepared_volume_directories,
                    volume_client,
                )
                try:
                    async with session_scope() as session:
                        await ContainerRepository(session).delete(container_id)
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "Git 最终状态提交失败且数据库记录清理失败: %s",
                        container_id,
                    )
                raise ExternalDependencyError("提交 Git 初始化最终状态失败") from exc

        return CreatedContainer(
            container_id=container_id,
            image=image,
            container_type=params.container_type,
            expiration_hours=expiration_hours,
            authorize_general_account=params.authorize_general_account is True,
            created_at=created_at,
            status=ContainerStatus.PENDING,
            service_id=service_id,
        )


def _sandbox_create_kwargs(
    *,
    name: str,
    service_id: str,
    user_id: str,
    container_type: str,
    gitee_user: str,
    gitee_repository: str,
    gitee_branch: Optional[str],
    gitee_url: str,
    authorize_general_account: bool,
    resource_limits: dict[str, str],
    volumes: Optional[list[SandboxVolume]],
) -> dict[str, Any]:
    """Build common OpenSandbox metadata, environment, resources, and volumes."""
    env = {
        "TESTAGENT_CLOUD_MODE": "1",
        "TESTAGENT_CLOUD_SERVICE_USER": user_id,
        "TESTAGENT_CLOUD_SERVICE_ID": service_id,
        "TESTAGENT_CLOUD_SERVICE_URL": settings.service_url,
        "TESTAGENT_CLOUD_GITEE_USER": gitee_user,
        "TESTAGENT_CLOUD_GITEE_REPOSITORY": gitee_repository,
        "TESTAGENT_CLOUD_GITEE_BRANCH": gitee_branch or "",
        "TESTAGENT_CLOUD_GITEE_URL": gitee_url,
        "TESTAGENT_CLOUD_AUTHORIZE_GENERAL_ACCOUNT": (
            "true" if authorize_general_account else "false"
        ),
        "TESTAGENT_CLOUD_PIP_URL": settings.container_pip_index_url,
        "TESTAGENT_CLOUD_NPM_URL": settings.container_npm_registry,
    }
    if container_type == ContainerType.AUTOTEST_CLOUD.value:
        env["TESTAGENT_ENABLE_CHROME"] = "1"
    create_kwargs: dict[str, Any] = {
        "name": name,
        "env": env,
        "metadata": {
            "name": name,
            _SOURCE_METADATA_KEY: _SOURCE_METADATA_VALUE,
            _CONTAINER_TYPE_METADATA_KEY: container_type,
            _TESTAGENT_CLOUD_TYPE_METADATA_KEY: container_type,
        },
        "resource_limits": resource_limits,
    }
    if volumes is not None:
        create_kwargs["volumes"] = volumes
    return create_kwargs


async def _wait_for_container_running(
    client: "OpenSandboxClient",
    container_id: str,
) -> None:
    """轮询 OpenSandbox，直到新容器真实进入 RUNNING。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _CONTAINER_START_WAIT_TIMEOUT_SECONDS
    terminal_states = {"FAILED", "PAUSED", "EXITED", "STOPPED", "TERMINATED", "DEAD"}
    while True:
        status = await client.get_status(container_id)
        state = (status.state or "").strip().upper()
        if state == "RUNNING":
            return
        if state in terminal_states:
            raise RuntimeError(f"容器未能启动，OpenSandbox 状态为 {state}")
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError("等待容器进入 RUNNING 状态超时")
        await asyncio.sleep(min(_CONTAINER_START_POLL_INTERVAL_SECONDS, remaining))


async def _cleanup_created_container(
    container_id: str,
    service_id: str,
    prepared_volume_directories: Optional[PreparedVolumeDirectories] = None,
    volume_client: Optional[FileBrowserClient] = None,
) -> None:
    """回收已创建的远端容器、卷目录和 Git 内存会话。"""
    # noinspection broad-exception
    try:
        # 数据库写入失败或会话绑定失败时尽力回收远端容器，避免留下孤儿资源。
        await get_opensandbox_client().delete(container_id)
    except Exception:  # noqa: BLE001
        logger.exception("创建后回收远端容器失败: %s", container_id)
    finally:
        try:
            if prepared_volume_directories is not None and volume_client is not None:
                await asyncio.to_thread(
                    rollback_volume_directories,
                    volume_client,
                    prepared_volume_directories,
                )
        finally:
            get_git_session_store().discard_session(service_id)


async def _cleanup_failed_remote_create(container_name: str) -> None:
    """回收创建请求失败后可能已存在的远端容器。"""
    try:
        client = get_opensandbox_client()
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "创建失败后初始化 OpenSandbox 清理客户端失败: %s: %s",
            type(exc).__name__,
            exc,
        )
        return
    try:
        remote_ids = await client.list_container_ids(
            metadata={
                _SOURCE_METADATA_KEY: _SOURCE_METADATA_VALUE,
                "name": container_name,
            }
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "创建失败后查找远端容器失败: %s: %s: %s",
            container_name,
            type(exc).__name__,
            exc,
        )
        return

    seen_ids: set[str] = set()
    for container_id in remote_ids:
        if not isinstance(container_id, str) or not container_id or container_id in seen_ids:
            continue
        seen_ids.add(container_id)
        try:
            await client.delete(container_id)
        except SandboxNotFoundError:
            continue
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "创建失败后回收远端容器失败: %s: %s: %s",
                container_id,
                type(exc).__name__,
                exc,
            )


async def _resolve_image(params: CreateContainerParams) -> str:
    if params.image:
        return params.image
    image = await _cfg_default_image(params.container_type)
    if not image:
        raise DefaultImageNotConfiguredError("没有提供默认镜像，请联系管理员解决")
    return image


def _validate_required(
    params: CreateContainerParams,
    *,
    validate_gitee: bool = True,
) -> None:
    if not params.user_id or not params.user_id.strip():
        raise InvalidArgumentError("user_id 不能为空")
    ensure_user_not_blacklisted(params.user_id)
    gitee_values = (
        _normalise_optional_gitee_value(params.gitee_url),
        _normalise_optional_gitee_value(params.gitee_user),
        _normalise_optional_gitee_value(params.gitee_repository),
    )
    if validate_gitee and any(gitee_values) and not all(gitee_values):
        raise InvalidArgumentError(
            "gitee_url、gitee_user、gitee_repository 必须同时填写或同时为空"
        )
    if params.cpu is not None and (
        isinstance(params.cpu, bool)
        or not isinstance(params.cpu, (int, float))
        or not math.isfinite(params.cpu)
        or params.cpu <= 0
    ):
        raise InvalidArgumentError("cpu 必须为正数")
    if params.memory is not None and (
        isinstance(params.memory, bool)
        or not isinstance(params.memory, int)
        or params.memory <= 0
    ):
        raise InvalidArgumentError("memory 必须为正整数")


def _normalise_optional_gitee_value(value: Optional[str]) -> str:
    """将未填写或仅含空白的码云字段统一为空字符串。"""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise InvalidArgumentError("码云字段必须为字符串")
    if not value.strip():
        return ""
    return value


async def _resource_limits(params: CreateContainerParams) -> dict[str, str]:
    """解析最终资源值；调用方未覆盖时使用 limit 配置。"""
    default_cpu, default_memory = await _cfg_resource_limits()
    cpu = params.cpu if params.cpu is not None else default_cpu
    memory = params.memory if params.memory is not None else default_memory
    limits: dict[str, str] = {"cpu": format(cpu, "g"), "memory": f"{memory}Gi"}
    return limits


async def _check_creation_limits(
    repo: ContainerRepository,
    user_id: str,
    gitee_user: str,
    gitee_repository: str,
    container_type: ContainerType,
) -> None:
    """创建限制（v4 §11.2）：白名单跳过；模式限制 + 数量限制。

    计数口径：`running`/`pending` 计入、`stopped`/`business_deleted` 不计；
    当前以「非业务删除记录」计数（业务过期会先置 deleted_at 再停容器），
    手动停止的容器仍视作占用预留槽位。repository 模式按
    `user_id + (gitee_user, gitee_repository)` 区分仓库。
    数量/模式限制均按容器类型分别计数：同一用户可同时持有
    testagent_cloud 与 autotest_cloud 各一个容器。
    """
    from application.whitelist import is_whitelisted

    if await is_whitelisted(user_id):
        return

    mode = settings.container_create_limit_mode
    if mode == "user":
        if await repo.count_active(user_id=user_id, container_type=container_type.value) >= 1:
            raise LimitReachedError("当前不允许同一用户创建多个同类型云端沙箱")
    elif mode == "repository":
        if await repo.count_active(
            user_id=user_id,
            gitee_repository=gitee_repository,
            gitee_user=gitee_user,
            container_type=container_type.value,
        ) >= 1:
            raise LimitReachedError("当前不允许同一用户为单个仓库创建多个同类型云端沙箱")
    else:
        raise BusinessConflictError(f"不支持的创建限制模式: {mode}")

    limit = await _cfg_count_limit()
    if 0 < limit <= await repo.count_active():
        raise LimitReachedError("可用云端沙箱数量已达到上限")


# ---------------------------------------------------------------------------
# 状态查询与剩余时间（T6.7）
# ---------------------------------------------------------------------------
async def get_status(
    service_id: str,
    *,
    enforce_user_policy: bool = True,
) -> ContainerStatusView:
    """实时查询 OpenSandbox 获取状态信息；资源指标不可用时返回空值。"""
    row = await _require_active_service_record(
        service_id,
        enforce_user_policy=enforce_user_policy,
    )
    return await _get_status_for_row(row)


async def _get_status_for_row(row: ContainerRow) -> ContainerStatusView:
    """根据已解析的业务记录查询运行态，内部仍以真实容器 ID 访问后端。"""
    container_id = row.container_id
    if row.stop_at is not None:
        return _stopped_status_view(row)
    try:
        status: SandboxStatus = await get_opensandbox_client().get_status(container_id)
    except SandboxNotFoundError as exc:
        await delete_missing_container_record(container_id)
        current = await _get_service_record_snapshot(row.service_id)
        if current is not None and current.stop_at is not None:
            return _stopped_status_view(current)
        raise ContainerNotFoundError("云端沙箱后端不存在") from exc
    except Exception as exc:
        _raise_backend_service_error("获取容器状态", exc)

    business = resolve_container_status(row.git_fin_status, status.state)
    endpoint: Optional[str] = None
    # noinspection broad-exception
    try:
        ep: SandboxEndpoint = await get_opensandbox_client().get_endpoint(
            container_id,
            Constants.CONTAINER_SSH_PORT.value,
        )
        endpoint = ep.endpoint
    except SandboxNotFoundError as exc:
        await delete_missing_container_record(container_id)
        current = await _get_service_record_snapshot(row.service_id)
        if current is not None and current.stop_at is not None:
            return _stopped_status_view(current)
        raise ContainerNotFoundError("云端沙箱后端不存在") from exc
    except Exception as exc:  # noqa: BLE001
        _raise_backend_service_error("获取容器端点", exc)

    # A paused/transitioning sandbox cannot serve execd metrics.  Metrics are
    # optional, so do not delay the lifecycle status response for that probe.
    if business is ContainerStatus.RUNNING:
        cpu_usage, memory_usage = await _get_metrics(container_id, row.service_id)
    else:
        cpu_usage, memory_usage = None, None

    return ContainerStatusView(
        container_id=container_id,
        service_id=row.service_id,
        status=business,
        git_fin_status=get_public_git_fin_status(row.git_fin_status),
        container_type=row.container_type,
        endpoint=endpoint,
        novnc_url=await _autotest_novnc_url(container_id, row.container_type),
        started_at=status.transitioned_at,
        expires_at=add_hours_to_iso(row.created_at, row.expiration_hours),
        cpu_usage=cpu_usage,
        memory_usage=memory_usage,
        gitee_url=row.gitee_url,
        gitee_branch=row.gitee_branch,
        api_token=row.api_token,
        gitee_user=row.gitee_user,
        gitee_repository=row.gitee_repository,
    )


def _stopped_status_view(row: ContainerRow) -> ContainerStatusView:
    """Build a stopped view without probing a deliberately deleted backend."""
    from scheduler.lifecycle import get_cached_status

    cached_status = get_cached_status(row.container_id)
    lifecycle_status = (
        ContainerStatus.PENDING
        if cached_status is ContainerStatus.PENDING
        else ContainerStatus.STOPPED
    )
    return ContainerStatusView(
        container_id=row.container_id,
        service_id=row.service_id,
        status=resolve_container_status(row.git_fin_status, lifecycle_status),
        container_type=row.container_type,
        started_at=None,
        expires_at=add_hours_to_iso(row.created_at, row.expiration_hours),
        gitee_url=row.gitee_url,
        gitee_branch=row.gitee_branch,
        api_token=row.api_token,
        gitee_user=row.gitee_user,
        gitee_repository=row.gitee_repository,
        git_fin_status=get_public_git_fin_status(row.git_fin_status),
    )


async def _get_service_record_snapshot(service_id: str) -> Optional[ContainerRow]:
    async with session_scope() as session:
        return await ContainerRepository(session).get_by_service_id(service_id)


async def get_container_logs(service_id: str) -> str:
    """读取指定容器日志；管理员可读取仍保留的业务删除记录。"""
    async with session_scope() as session:
        row = await _require_service_record(
            ContainerRepository(session),
            service_id,
            include_deleted=True,
        )
        container_id = row.container_id

    try:
        logs: str | bytes = await get_opensandbox_client().get_logs(container_id)
        return normalize_log_text(logs)
    except SandboxNotFoundError as exc:
        raise ContainerNotFoundError("云端沙箱后端不存在") from exc
    except Exception as exc:
        _raise_backend_service_error("获取容器日志错误", exc)


# ---------------------------------------------------------------------------
# 容器操作（T6.3）
# ---------------------------------------------------------------------------
async def start(service_id: str) -> None:
    """启动容器；后端已被删除时按原服务记录重建。"""
    async with lifecycle_guard():
        row = await _require_active_service_record(service_id)
        container_id = row.container_id
        try:
            await get_opensandbox_client().start(container_id)
        except SandboxNotFoundError:
            await _persist_stop_intent(service_id, container_id)
            from scheduler.lifecycle import mark_container_start_requested, mark_container_stopped

            mark_container_start_requested(container_id)
            try:
                new_container_id = await _rebuild_backend(service_id)
            except Exception:
                mark_container_stopped(container_id)
                raise
            mark_container_start_requested(new_container_id)
            return
        except SandboxFailedError as exc:
            raise BusinessConflictError("失败状态的云端沙箱不能直接启动，请先删除后重新创建") from exc
        except Exception as exc:
            _raise_backend_service_error("启动容器", exc)
        await _clear_stop_intent(service_id, container_id)
        from scheduler.lifecycle import mark_container_start_requested

        mark_container_start_requested(container_id)


async def stop(service_id: str) -> None:
    """物理删除运行后端并保留业务记录和 PVC 卷。"""
    async with lifecycle_guard():
        row = await _require_active_service_record(service_id)
        container_id = row.container_id
        await _persist_stop_intent(service_id, container_id)
        from scheduler.lifecycle import mark_container_stop_requested

        mark_container_stop_requested(container_id)
        try:
            await get_opensandbox_client().delete(container_id)
        except SandboxNotFoundError:
            pass
        except Exception as exc:
            _raise_backend_service_error("停止容器", exc)
        from scheduler.lifecycle import mark_container_stopped

        mark_container_stopped(container_id)


async def restart(service_id: str) -> None:
    """重启容器；仅缺失后端时重建，正常 Restart 保持原 ID。"""
    async with lifecycle_guard():
        row = await _require_active_service_record(service_id)
        container_id = row.container_id
        try:
            await get_opensandbox_client().restart(container_id)
        except SandboxNotFoundError:
            await _persist_stop_intent(service_id, container_id)
            from scheduler.lifecycle import mark_container_start_requested, mark_container_stopped

            mark_container_start_requested(container_id)
            try:
                new_container_id = await _rebuild_backend(service_id)
            except Exception:
                mark_container_stopped(container_id)
                raise
            mark_container_start_requested(new_container_id)
            return
        except SandboxFailedError as exc:
            raise BusinessConflictError("失败状态的云端沙箱不能直接重启，请先删除后重新创建") from exc
        except Exception as exc:
            _raise_backend_service_error("重启容器", exc)
        await _clear_stop_intent(service_id, container_id)
        from scheduler.lifecycle import mark_container_start_requested

        mark_container_start_requested(container_id)


# ---------------------------------------------------------------------------
# 业务删除（T6.4）
# ---------------------------------------------------------------------------
async def business_delete(service_id: str) -> None:
    """业务删除：物理删除后端并将记录和卷保留到 retention 到期。

    已业务删除的容器再次删除一律视为不存在（404 语义，v4 §14.9 由最新约定覆盖）。
    """
    async with lifecycle_guard():
        await _business_delete_locked(service_id)


async def business_delete_failed_container(
    service_id: str,
    expected_created_at: str,
) -> bool:
    """Recheck and delete a failed container whose creation grace period elapsed."""
    async with lifecycle_guard():
        if not await _cfg_get_delete_failed_container():
            return False
        return await _business_delete_locked(
            service_id,
            expected_created_at=expected_created_at,
        )


async def _business_delete_locked(
    service_id: str,
    *,
    expected_created_at: Optional[str] = None,
) -> bool:
    async with session_scope() as session:
        repo = ContainerRepository(session)
        if expected_created_at is None:
            row = await _require_service_record(repo, service_id)
        else:
            candidate = await repo.get_by_service_id(service_id)
            if (
                candidate is None
                or candidate.deleted_at is not None
                or not isinstance(candidate.git_fin_status, str)
                or not candidate.git_fin_status.startswith("failed_")
                or candidate.created_at != expected_created_at
                or not _container_creation_grace_elapsed(candidate.created_at)
            ):
                return False
            row = candidate
        container_id = row.container_id
        await repo.business_delete(container_id, _now_iso())
    get_git_session_store().suspend_session(service_id)
    try:
        await get_opensandbox_client().delete(container_id)
    except SandboxNotFoundError:
        pass
    except Exception as exc:
        _raise_backend_service_error("删除容器", exc)
    async with session_scope() as session:
        await ContainerRepository(session).clear_stop_intent(container_id)
    from scheduler.lifecycle import discard_container_status

    discard_container_status(container_id)
    return True


def _container_creation_grace_elapsed(created_at: str) -> bool:
    """Return whether a container has existed for at least fifteen minutes."""
    try:
        created = datetime.fromisoformat(created_at)
    except (TypeError, ValueError):
        return False
    if created.tzinfo is None:
        created = created.replace(tzinfo=_TZ)
    return created + timedelta(minutes=15) <= _now()


# ---------------------------------------------------------------------------
# 恢复（T6.5，仅管理 API）
# ---------------------------------------------------------------------------
async def restore(service_id: str, expiration_hours: int) -> ContainerStatusView:
    """恢复：清除 `deleted_at`、重写时间并启动或重建后端。

    `authorize_general_account` 不重新指定，保持原值（变更 #2）。
    """
    _validate_expiration_hours(expiration_hours)
    async with lifecycle_guard():
        async with session_scope() as session:
            repo = ContainerRepository(session)
            row = await _require_service_record(
                repo,
                service_id,
                include_deleted=True,
            )
            if row.deleted_at is None:
                raise BusinessConflictError("云端沙箱未处于业务删除状态，无法恢复")
            container_id = row.container_id
            restored_at = _now_iso()
        try:
            await get_opensandbox_client().start(container_id)
        except SandboxNotFoundError:
            await _rebuild_backend(
                service_id,
                restore_data=(restored_at, expiration_hours),
            )
        except SandboxFailedError as exc:
            raise BusinessConflictError("失败状态的云端沙箱不能恢复") from exc
        except Exception as exc:
            _raise_backend_service_error("启动容器", exc)
        else:
            async with session_scope() as session:
                await ContainerRepository(session).business_restore(
                    container_id,
                    restored_at,
                    expiration_hours,
                )
            get_git_session_store().resume_suspended_session(service_id)
            from scheduler.lifecycle import mark_container_start_requested

            mark_container_start_requested(container_id)

    return await get_status(service_id)


async def _get_metrics(
    container_id: str,
    service_id: str,
) -> tuple[Optional[float], Optional[float]]:
    """读取容器资源使用率；指标不可用时返回空值且不影响状态查询。"""
    get_metrics = getattr(get_opensandbox_client(), "get_metrics", None)
    if not callable(get_metrics):
        return None, None
    # noinspection broad-exception
    try:
        # noinspection calling-non-callable
        metrics = await get_metrics(container_id)
    except SandboxNotFoundError as exc:
        await delete_missing_container_record(container_id)
        current = await _get_service_record_snapshot(service_id)
        if current is not None and current.stop_at is not None:
            return None, None
        raise ContainerNotFoundError("云端沙箱后端不存在") from exc
    except Exception:
        return None, None
    if not isinstance(metrics, SandboxMetrics):
        return None, None
    return metrics.cpu_usage, metrics.memory_usage


async def _persist_stop_intent(service_id: str, container_id: str) -> None:
    """Commit the intent before an expected backend deletion or reconstruction."""
    async with session_scope() as session:
        repo = ContainerRepository(session)
        row = await repo.get_by_service_id(service_id)
        if row is None or row.deleted_at is not None or row.container_id != container_id:
            raise ContainerNotFoundError("云端沙箱不存在")
        if row.stop_at is None and not await repo.set_stop_intent(container_id, _now_iso()):
            raise ContainerNotFoundError("云端沙箱不存在")


async def _clear_stop_intent(service_id: str, container_id: str) -> None:
    async with session_scope() as session:
        row = await ContainerRepository(session).get_by_service_id(service_id)
        if row is not None and row.container_id == container_id and row.deleted_at is None:
            await ContainerRepository(session).clear_stop_intent(container_id)


# noinspection broad-exception
async def _rebuild_backend(
    service_id: str,
    *,
    restore_data: Optional[tuple[str, int]] = None,
) -> str:
    """Create a backend for an existing service without cloning or reinitializing Git."""
    async with _create_lock:
        async with session_scope() as session:
            row = await _require_service_record(
                ContainerRepository(session),
                service_id,
                include_deleted=restore_data is not None,
            )
            if restore_data is None and row.deleted_at is not None:
                raise ContainerNotFoundError("云端沙箱不存在")
            previous_container_id = row.container_id
            user_id = row.user_id
            container_type = row.container_type
            service_image = row.image
            git_user = row.gitee_user
            git_repository = row.gitee_repository
            git_branch = row.gitee_branch or ""
            git_url = row.gitee_url
            authorize_general_account = bool(row.authorize_general_account)

        name = uuid.uuid4().hex[:12]
        cpu, memory = await _cfg_resource_limits()
        volumes: Optional[list[SandboxVolume]] = None
        if settings.filebrowser_enabled:
            plan = build_volume_path_plan(user_id, service_id)
            volumes = build_sandbox_volumes(plan, settings.pvc_name or "")
        create_kwargs = _sandbox_create_kwargs(
            name=name,
            service_id=service_id,
            user_id=user_id,
            container_type=container_type,
            gitee_user=git_user,
            gitee_repository=git_repository,
            gitee_branch=git_branch,
            gitee_url=git_url,
            authorize_general_account=authorize_general_account,
            resource_limits={"cpu": format(cpu, "g"), "memory": f"{memory}Gi"},
            volumes=volumes,
        )

        client = get_opensandbox_client()
        try:
            created = await client.create(service_image, **create_kwargs)
        except Exception as exc:  # noqa: BLE001
            await _cleanup_failed_remote_create(name)
            _raise_backend_service_error("重建容器", exc)

        new_container_id = created.container_id
        store = get_git_session_store()
        session_rebound = False
        try:
            await _wait_for_container_running(client, new_container_id)
            session_rebound = store.replace_container_id(
                service_id,
                previous_container_id,
                new_container_id,
            )
            async with session_scope() as session:
                repo = ContainerRepository(session)
                if not await repo.replace_container_id(
                    service_id,
                    previous_container_id,
                    new_container_id,
                    restore=restore_data,
                ):
                    raise ContainerNotFoundError("云端沙箱记录已变化，无法更新后端 ID")
        except Exception as exc:  # noqa: BLE001
            if session_rebound:
                try:
                    store.replace_container_id(
                        service_id,
                        new_container_id,
                        previous_container_id,
                    )
                except Exception:
                    logger.exception("重建失败后恢复 Git 会话绑定失败: %s", service_id)
            try:
                await client.delete(new_container_id)
            except Exception:  # noqa: BLE001
                logger.exception("重建失败后清理新后端容器失败: %s", new_container_id)
            if isinstance(exc, (ContainerNotFoundError, BusinessConflictError)):
                raise
            _raise_backend_service_error("重建容器", exc)

        from scheduler.lifecycle import discard_container_status

        discard_container_status(previous_container_id)
        if restore_data is not None:
            store.resume_suspended_session(service_id)
        return new_container_id


# ---------------------------------------------------------------------------
# 立即删除（T6.6，仅管理 API）
# ---------------------------------------------------------------------------
async def permanent_delete(service_id: str) -> None:
    """立即删除：OpenSandbox Delete 物理删除底层容器 + 删除 SQLite 记录（v4 §11.4）。"""
    async with lifecycle_guard():
        async with session_scope() as session:
            repo = ContainerRepository(session)
            row = await _require_service_record(
                repo,
                service_id,
                include_deleted=True,
            )
            container_id = row.container_id
            service_id = row.service_id
            try:
                await get_opensandbox_client().delete(container_id)
            except SandboxNotFoundError:
                # 远端已不存在，仍需清理本地卷和记录。
                pass
            except Exception as exc:
                _raise_backend_service_error("删除容器", exc)
            await cleanup_volume_for_container(row.user_id, row.service_id)
            await repo.delete(container_id)
    get_git_session_store().discard_session(service_id)


# ---------------------------------------------------------------------------
# 设置业务有效时长（T6.8）
# ---------------------------------------------------------------------------
async def set_expiration(service_id: str, expiration_hours: int) -> ExpirationView:
    """仅修改 `expiration_hours`，不重置 `created_at`；0 表示永不过期（v4 §14.8）。"""
    _validate_expiration_hours(expiration_hours)
    async with lifecycle_guard():
        async with session_scope() as session:
            repo = ContainerRepository(session)
            row = await _require_service_record(repo, service_id)
            container_id = row.container_id
            await repo.update_expiration(container_id, expiration_hours)
            expiration = add_hours_to_iso(row.created_at, expiration_hours)
    return ExpirationView(
        container_id=container_id,
        service_id=service_id,
        expires_at=expiration,
    )


# ---------------------------------------------------------------------------
# 用户/容器映射
# ---------------------------------------------------------------------------
async def map_container_id(user_id: str, service_id: str) -> str:
    """按用户和服务 ID 返回当前可用后端 ID；已停止服务没有可映射后端。"""
    if not user_id or not user_id.strip():
        raise InvalidArgumentError("user_id 不能为空")
    if not service_id or not service_id.strip():
        raise InvalidArgumentError("service_id 不能为空")
    ensure_user_not_blacklisted(user_id)

    async with session_scope() as session:
        row = await ContainerRepository(session).get_by_service_id(service_id)
        if row is None or row.deleted_at is not None or row.stop_at is not None:
            raise ContainerNotFoundError("云端沙箱不存在")
        ensure_user_not_blacklisted(row.user_id)
        if row.user_id != user_id:
            raise ContainerNotFoundError("云端沙箱不存在")
        return row.container_id


# ---------------------------------------------------------------------------
# 业务条件查询（REST GET /containers）
# ---------------------------------------------------------------------------
async def query_container_ids(
    user_id: str,
    *,
    gitee_user: Optional[str] = None,
    gitee_repository: Optional[str] = None,
    gitee_branch: Optional[str] = None,
    container_type: Optional[ContainerType] = None,
) -> list[str]:
    """按业务条件查询 service ID（AND 组合，不含业务已删除）。

    `user_id` 必填：REST 端点无认证，禁止不带用户标识枚举全部容器。
    """
    if not user_id or not user_id.strip():
        raise InvalidArgumentError("user_id 不能为空")
    ensure_user_not_blacklisted(user_id)
    async with session_scope() as session:
        rows = await ContainerRepository(session).list_active(
            user_id=user_id,
            gitee_user=gitee_user,
            gitee_repository=gitee_repository,
            gitee_branch=gitee_branch,
            container_type=container_type.value if container_type is not None else None,
        )
        return [r.service_id for r in rows]


async def query_container_statuses(
    user_id: str,
    *,
    gitee_user: Optional[str] = None,
    gitee_repository: Optional[str] = None,
    gitee_branch: Optional[str] = None,
    container_type: Optional[ContainerType] = None,
) -> list[ContainerStatusView]:
    """按业务条件一次性批量查询容器状态视图（不含业务已删除）。

    `user_id` 必填：REST 端点无认证，禁止不带用户标识枚举全部容器。
    供客户端避免「先查 ID 列表、再逐个查状态」的多次往返。
    """
    if not user_id or not user_id.strip():
        raise InvalidArgumentError("user_id 不能为空")
    ensure_user_not_blacklisted(user_id)
    async with session_scope() as session:
        rows = await ContainerRepository(session).list_active(
            user_id=user_id,
            gitee_user=gitee_user,
            gitee_repository=gitee_repository,
            gitee_branch=gitee_branch,
            container_type=container_type.value if container_type is not None else None,
        )
        service_ids = [row.service_id for row in rows]
    return [await get_status(service_id) for service_id in service_ids]


async def list_orphan_container_ids() -> list[str]:
    """查询带本服务来源标记、但数据库中没有记录的远端容器。"""
    async with _create_lock:
        return await _list_orphan_container_ids_locked()


async def delete_orphan_containers(container_ids: list[str]) -> None:
    """删除指定孤儿容器；单个失败不影响其他合法 ID 的处理。"""
    requested_ids = _normalise_orphan_container_ids(container_ids)
    # 即使请求声称目标是孤儿容器，也先拒绝已登记黑名单用户的资源，避免
    # 以孤儿校验路径绕过 container_id 归属策略。
    for container_id in requested_ids:
        await _ensure_container_owner_allowed_if_present(container_id)
    not_orphan_ids: list[str] = []
    failed_ids: list[str] = []

    # 将重新比对和删除放在同一创建锁中，避免新建容器在快照之后尚未
    # 落库时被误判为孤儿。
    async with _create_lock:
        orphan_ids = set(await _list_orphan_container_ids_locked())
        client = get_opensandbox_client()
        for container_id in requested_ids:
            if container_id not in orphan_ids:
                not_orphan_ids.append(container_id)
                continue
            try:
                await client.delete(container_id)
            except SandboxNotFoundError:
                # 目标在列表和删除之间消失时，最终状态已经满足。
                continue
            except Exception as exc:  # noqa: BLE001
                failed_ids.append(container_id)
                logger.error(
                    "删除孤儿容器失败: %s: %s: %s",
                    container_id,
                    type(exc).__name__,
                    exc,
                )

    if not_orphan_ids:
        details = ", ".join(not_orphan_ids)
        if failed_ids:
            details += f"；删除失败: {', '.join(failed_ids)}"
        raise InvalidArgumentError(f"以下云端沙箱不是孤儿容器: {details}")
    if failed_ids:
        raise ExternalDependencyError(
            f"以下孤儿容器删除失败: {', '.join(failed_ids)}"
        )


async def cleanup_orphan_containers() -> list[str]:
    """Delete all remote service containers with no DB row under the create lock."""
    deleted: list[str] = []
    async with _create_lock:
        orphan_ids = await _list_orphan_container_ids_locked()
        if not orphan_ids:
            return deleted
        client = get_opensandbox_client()
        for container_id in orphan_ids:
            try:
                await client.delete(container_id)
            except SandboxNotFoundError:
                deleted.append(container_id)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Scheduler 删除孤儿容器失败: %s: %s: %s",
                    container_id,
                    type(exc).__name__,
                    exc,
                )
            else:
                deleted.append(container_id)
    if deleted:
        logger.info("Scheduler 孤儿清理: 删除 %d 个远端容器", len(deleted))
    return deleted


async def delete_sandboxes_by_pod_names(pod_names: list[str]) -> None:
    """按 K8s Pod 名称物理删除容器（仅管理 API）。

    直接 `kubectl delete pod` 会被 BatchSandbox 控制器重建（表现为「自动重启」）。
    本接口把 Pod 名称解析回 OpenSandbox 容器 ID 后调用管理面删除，连带删除
    BatchSandbox CR，Pod 不会被重建。已不存在的容器按幂等成功处理，并清理同 ID
    的本地数据库记录。
    """
    requested_names = _normalise_pod_names(pod_names)
    client = get_opensandbox_client()
    known_ids = await _list_managed_container_ids(client)
    invalid_names: list[str] = []
    failed_names: list[str] = []
    processed_ids: set[str] = set()
    resolved_ids: list[tuple[str, str]] = []

    for pod_name in requested_names:
        container_id = _resolve_container_id_from_pod_name(pod_name, known_ids)
        if container_id is None:
            invalid_names.append(pod_name)
            continue
        if container_id in processed_ids:
            continue
        processed_ids.add(container_id)
        resolved_ids.append((pod_name, container_id))

    # 先检查所有可解析的数据库归属，再执行任何远端删除，避免批量请求在
    # 命中黑名单资源后已经产生部分副作用。
    for _, container_id in resolved_ids:
        await _ensure_container_owner_allowed_if_present(container_id)

    for pod_name, container_id in resolved_ids:
        async with lifecycle_guard():
            try:
                await client.delete(container_id)
            except SandboxNotFoundError:
                # 目标在解析和删除之间消失时，最终状态已经满足。
                pass
            except Exception as exc:  # noqa: BLE001
                failed_names.append(pod_name)
                logger.error(
                    "按 Pod 名称删除容器失败: %s (%s): %s: %s",
                    pod_name,
                    container_id,
                    type(exc).__name__,
                    exc,
                )
                continue
            async with session_scope() as session:
                repo = ContainerRepository(session)
                row = await repo.get(container_id)
                if row is not None:
                    await cleanup_volume_for_container(row.user_id, row.service_id)
                    await repo.delete(container_id)

    if invalid_names:
        raise InvalidArgumentError(
            f"以下 Pod 名称无法解析为容器 ID: {', '.join(invalid_names)}"
        )
    if failed_names:
        raise ExternalDependencyError(
            f"以下 Pod 删除失败: {', '.join(failed_names)}"
        )


# ---------------------------------------------------------------------------
# 管理端容器完整查询与数量限制
# ---------------------------------------------------------------------------
async def list_admin_containers() -> list[AdminContainerView]:
    """列出全部容器，包括业务已删除记录。"""
    async with session_scope() as session:
        rows = await ContainerRepository(session).list_all()
    views: list[AdminContainerView] = []
    for row in rows:
        try:
            views.append(await _to_admin_view(row))
        except ContainerNotFoundError:
            # 状态查询已同步清理远端缺失的本地活跃记录，不再返回该条目。
            continue
    return views


async def get_admin_container(service_id: str) -> AdminContainerView:
    """查询管理端容器完整信息；业务已删除记录仍可查询。"""
    async with session_scope() as session:
        row = await _require_service_record(
            ContainerRepository(session),
            service_id,
            include_deleted=True,
        )
    return await _to_admin_view(row)


async def get_container_limit() -> ContainerLimitView:
    """读取容器数量及资源限制配置。"""
    cpu, memory = await _cfg_resource_limits()
    return ContainerLimitView(
        container_limit=await _cfg_count_limit(),
        cpu=cpu,
        memory=memory,
        delete_failed_container=await _cfg_get_delete_failed_container(),
    )


async def set_container_limit(
    container_limit: int,
    *,
    cpu: float,
    memory: int,
    delete_failed_container: Optional[bool] = None,
) -> ContainerLimitView:
    """设置容器数量及资源限制并返回最新限制视图。"""
    if (
        isinstance(container_limit, bool)
        or not isinstance(container_limit, int)
        or container_limit < 0
    ):
        raise InvalidArgumentError("container_limit 必须为非负整数")
    if (
        isinstance(cpu, bool)
        or not isinstance(cpu, (int, float))
        or not math.isfinite(cpu)
        or cpu <= 0
    ):
        raise InvalidArgumentError("cpu 必须为正数")
    if (
        isinstance(memory, bool)
        or not isinstance(memory, int)
        or memory <= 0
    ):
        raise InvalidArgumentError("memory 必须为正整数")
    if delete_failed_container is not None and not isinstance(delete_failed_container, bool):
        raise InvalidArgumentError("delete_failed_container 必须为布尔值")
    await _cfg_set_count_limit(container_limit)
    await _cfg_set_resource_limits(cpu, memory)
    if delete_failed_container is not None:
        async with lifecycle_guard():
            await _cfg_set_delete_failed_container(delete_failed_container)
    return await get_container_limit()


async def get_admin_state() -> AdminStateView:
    """读取未业务删除容器及白名单/管理员清单的基础统计。"""
    async with session_scope() as session:
        container_repo = ContainerRepository(session)
        whitelist_rows = await WhitelistUserRepository(session).list_all()
        admin_rows = await AdminUserRepository(session).list_all()
        containers = await container_repo.list_active()
        whitelist_ids = {row.user_id for row in whitelist_rows}
        admin_ids = {row.user_id for row in admin_rows}

    return AdminStateView(
        container_count=len(containers),
        whitelist_container_count=sum(row.user_id in whitelist_ids for row in containers),
        admin_container_count=sum(row.user_id in admin_ids for row in containers),
        whitelist_count=len(whitelist_ids),
        admin_count=len(admin_ids),
    )


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------
async def _require_active_service_record(
    service_id: str,
    *,
    enforce_user_policy: bool = True,
) -> ContainerRow:
    """按公开 service ID 解析业务有效记录；不得回退匹配真实 container ID。"""
    async with session_scope() as session:
        return await _require_service_record(
            ContainerRepository(session),
            service_id,
            enforce_user_policy=enforce_user_policy,
        )


async def _require_service_record(
    repo: ContainerRepository,
    service_id: str,
    *,
    include_deleted: bool = False,
    enforce_user_policy: bool = True,
) -> ContainerRow:
    """按公开 service ID 取记录并统一执行黑名单/活跃状态校验。"""
    row = await repo.get_by_service_id(service_id)
    if row is None:
        raise ContainerNotFoundError("云端沙箱不存在")
    if enforce_user_policy:
        ensure_user_not_blacklisted(row.user_id)
    if not include_deleted and row.deleted_at is not None:
        raise ContainerNotFoundError("云端沙箱不存在")
    return row


async def _require_active_record(
    container_id: str,
    *,
    enforce_user_policy: bool = True,
) -> ContainerRow:
    """取业务有效（非业务已删除）容器记录；不存在或已业务删除抛 404 语义错误。"""
    async with session_scope() as session:
        row = await ContainerRepository(session).get(container_id)
    if row is None:
        raise ContainerNotFoundError("云端沙箱不存在")
    if enforce_user_policy:
        ensure_user_not_blacklisted(row.user_id)
    if row.deleted_at is not None:
        raise ContainerNotFoundError("云端沙箱不存在")
    return row


async def _ensure_container_owner_allowed_if_present(container_id: str) -> None:
    """校验已登记容器的用户；孤儿容器没有可确认的业务归属。"""
    async with session_scope() as session:
        row = await ContainerRepository(session).get(container_id)
    if row is not None:
        ensure_user_not_blacklisted(row.user_id)


def _normalise_pod_names(pod_names: list[str]) -> list[str]:
    """校验并去重 K8s Pod 名称，保留请求顺序。"""
    if not isinstance(pod_names, list) or not pod_names:
        raise InvalidArgumentError("pod_names 不能为空")

    result: list[str] = []
    seen: set[str] = set()
    for pod_name in pod_names:
        if not isinstance(pod_name, str) or not pod_name.strip():
            raise InvalidArgumentError("pod_name 不能为空")
        normalized = pod_name.strip()
        if normalized not in seen:
            result.append(normalized)
            seen.add(normalized)
    return result


async def _list_managed_container_ids(client: OpenSandboxClient) -> set[str]:
    """尽力获取带本服务来源标记的容器 ID；失败时返回空集合（仍可用 UUID 解析）。"""
    try:
        return set(
            await client.list_container_ids(
                metadata={_SOURCE_METADATA_KEY: _SOURCE_METADATA_VALUE}
            )
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "按 Pod 名称删除时查询容器列表失败（改用名称解析）: %s: %s",
            type(exc).__name__,
            exc,
        )
        return set()


def _resolve_container_id_from_pod_name(
    pod_name: str,
    known_ids: set[str],
) -> Optional[str]:
    """将 K8s Pod 名称解析为 OpenSandbox 容器 ID。

    OpenSandbox 用随机 UUID4 作为容器 ID，其 BatchSandbox CR 与 Pod 名称前缀一致，
    Pod 名称形如 `<sandbox-id>-<随机后缀>`（多副本时可能还有索引段）。解析顺序：
    1. Pod 名称本身即容器 ID（已记录或本身为合法 UUID）；
    2. 与已知容器 ID 前缀匹配；
    3. 逐段去掉尾部生成后缀直到得到合法 UUID。
    """
    if pod_name in known_ids:
        return pod_name
    for known_id in known_ids:
        if pod_name.startswith(f"{known_id}-"):
            return known_id

    candidate = pod_name
    while candidate:
        if _is_uuid(candidate):
            return candidate
        head, separator, _ = candidate.rpartition("-")
        if not separator:
            return None
        candidate = head
    return None


def _is_uuid(value: str) -> bool:
    """判断字符串是否为合法 UUID（OpenSandbox 容器 ID 形态）。"""
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return False
    return True


def _normalise_orphan_container_ids(container_ids: list[str]) -> list[str]:
    """校验并去重孤儿容器 ID，保留请求顺序。"""
    if not isinstance(container_ids, list) or not container_ids:
        raise InvalidArgumentError("container_ids 不能为空")

    result: list[str] = []
    seen: set[str] = set()
    for container_id in container_ids:
        if not isinstance(container_id, str) or not container_id.strip():
            raise InvalidArgumentError("container_id 不能为空")
        normalized = container_id.strip()
        if normalized not in seen:
            result.append(normalized)
            seen.add(normalized)
    return result


async def _list_orphan_container_ids_locked() -> list[str]:
    """在 `_create_lock` 内查询并返回孤儿 ID。"""
    try:
        remote_ids = await get_opensandbox_client().list_container_ids(
            metadata={
                _SOURCE_METADATA_KEY: _SOURCE_METADATA_VALUE,
            }
        )
    except Exception as exc:
        _raise_backend_service_error("查询孤儿容器", exc)

    async with session_scope() as session:
        stored_ids = set(await ContainerRepository(session).list_all_ids())

    orphan_ids: list[str] = []
    seen_ids: set[str] = set()
    for container_id in remote_ids:
        if (
            container_id
            and container_id not in stored_ids
            and container_id not in seen_ids
        ):
            orphan_ids.append(container_id)
            seen_ids.add(container_id)
    return orphan_ids


def _raise_backend_service_error(operation: str, exc: Exception) -> NoReturn:
    """将 OpenSandbox 故障记录为一条简洁错误，并统一映射为 HTTP 502。"""
    if not isinstance(exc, OpenSandboxError):
        logger.error(
            "OpenSandbox %s失败: %s: %s",
            operation,
            type(exc).__name__,
            exc,
        )
    raise ExternalDependencyError("云端沙箱后端依赖错误") from exc


def _raise_volume_creation_error(operation: str, exc: Exception) -> NoReturn:
    """将卷目录准备失败转换为安全的业务错误。"""
    if isinstance(exc, VolumePathConflictError):
        raise exc
    if isinstance(exc, FileBrowserConflictError):
        raise VolumePathConflictError("FileBrowser 卷路径已存在或发生冲突") from None
    logger.error("FileBrowser %s失败: %s", operation, type(exc).__name__)
    raise ExternalDependencyError("FileBrowser 依赖错误") from None


def _is_image_not_found_error(exc: Exception) -> bool:
    """判断 OpenSandbox 创建失败是否由镜像不存在导致。"""
    markers = (
        "image not found",
        "image_not_found",
        "image-not-found",
        "镜像不存在",
        "镜像未找到",
        "manifest unknown",
        "manifest_unknown",
        "errimagepull",
        "imagepullbackoff",
        "no such image",
        "pull access denied",
        "failed to pull image",
        "repository does not exist",
    )
    pending: list[BaseException] = [exc]
    visited: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in visited:
            continue
        visited.add(id(current))

        parts = [str(current)]
        error = getattr(current, "error", None)
        if error is not None:
            for attribute in ("code", "message"):
                value = getattr(error, attribute, None)
                if isinstance(value, str) and value:
                    parts.append(value)
        text = " ".join(parts).lower()
        if any(marker in text for marker in markers):
            return True
        if "image" in text and any(
            marker in text for marker in ("not found", "does not exist")
        ):
            return True

        cause = current.__cause__
        context = current.__context__
        if cause is not None:
            pending.append(cause)
        if context is not None:
            pending.append(context)
    return False


async def _to_admin_view(row: ContainerRow) -> AdminContainerView:
    cpu_usage: Optional[float] = None
    memory_usage: Optional[float] = None
    # 有效期来自持久化业务字段，不能依赖运行时状态缓存。
    expires_at = add_hours_to_iso(row.created_at, row.expiration_hours)
    if row.deleted_at is not None:
        status = ContainerStatus.BUSINESS_DELETED
        endpoint: Optional[str] = None
        started_at: Optional[str] = None
    else:
        runtime = await _get_admin_runtime(row)
        status = resolve_container_status(row.git_fin_status, runtime.status)
        endpoint = runtime.endpoint
        started_at = runtime.started_at
        cpu_usage = runtime.cpu_usage
        memory_usage = runtime.memory_usage

    return AdminContainerView(
        container_id=row.container_id,
        service_id=row.service_id,
        image=row.image,
        container_type=row.container_type,
        user_id=row.user_id,
        gitee_url=row.gitee_url,
        api_token=row.api_token,
        gitee_user=row.gitee_user,
        gitee_repository=row.gitee_repository,
        gitee_branch=row.gitee_branch,
        created_at=row.created_at,
        expiration_hours=row.expiration_hours,
        authorize_general_account=bool(row.authorize_general_account),
        status=status,
        git_fin_status=get_public_git_fin_status(row.git_fin_status),
        endpoint=endpoint,
        novnc_url=await _autotest_novnc_url(row.container_id, row.container_type),
        started_at=started_at,
        expires_at=expires_at,
        cpu_usage=cpu_usage,
        memory_usage=memory_usage,
        deleted_at=cast(Optional[str], row.deleted_at),
        business_deleted=row.deleted_at is not None,
    )


async def _get_admin_runtime(row: ContainerRow) -> ContainerStatusView:
    """优先使用 Scheduler 快照，首次刷新前才回退到实时查询。"""
    # 局部导入避免 application.container 与 scheduler.lifecycle 的模块循环依赖。
    from scheduler.lifecycle import get_cached_runtime, get_cached_status

    container_id = row.container_id
    cached = get_cached_runtime(container_id)
    if cached is not None:
        return ContainerStatusView(
            container_id=container_id,
            service_id=row.service_id,
            status=cached.status,
            git_fin_status=cached.git_fin_status,
            endpoint=cached.endpoint,
            started_at=cached.started_at,
            cpu_usage=cached.cpu_usage,
            memory_usage=cached.memory_usage,
        )

    # 保留旧缓存的兼容读取路径：测试或进程升级期间可能只有状态缓存。
    cached_status = get_cached_status(container_id)
    if cached_status is not None:
        return ContainerStatusView(
            container_id=container_id,
            service_id=row.service_id,
            status=cached_status,
        )

    return await get_status(row.service_id, enforce_user_policy=False)
