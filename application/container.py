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

import logging
import math
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Iterator, NoReturn, Optional
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from application.blacklist import ensure_user_not_blacklisted
from application.volume_paths import (
    VolumePathPlan,
    build_sandbox_volumes,
    build_volume_path_plan,
)
from application.volume_storage import (
    PreparedVolumeDirectories,
    prepare_volume_directories,
    rollback_volume_directories,
)
from config import Constants, settings
from config import get_container_count_limit as _cfg_count_limit
from config import get_default_image as _cfg_default_image
from config import get_container_resource_limits as _cfg_resource_limits
from config import set_container_count_limit as _cfg_set_count_limit
from config import set_container_resource_limits as _cfg_set_resource_limits
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
    "restore",
    "permanent_delete",
    "set_expiration",
    "query_container_ids",
    "list_orphan_container_ids",
    "delete_orphan_containers",
    "delete_sandboxes_by_pod_names",
    "list_admin_containers",
    "get_admin_container",
    "get_container_limit",
    "set_container_limit",
]

_TZ = ZoneInfo(Constants.TIMEZONE.value)

#: 创建限制临界区互斥（单实例部署，配合 SQLite 单写者保证原子性，v4 §13.1/§11.2）
_create_lock = threading.Lock()
#: 恢复与 Scheduler 物理清理共用的生命周期临界区（单实例部署）
_lifecycle_lock = threading.Lock()
_SOURCE_METADATA_KEY = "testagent-cloud"
_SOURCE_METADATA_VALUE = "true"
_CONTAINER_TYPE_METADATA_KEY = "container-type"
#: 镜像内 noVNC/websockify 监听端口（tscode-server 镜像内固定 6080）
_NOVNC_PORT = 6080


# noinspection HttpUrlsUsage
def _build_novnc_url(endpoint: Optional[str]) -> Optional[str]:
    """由容器内 noVNC(6080) 的 endpoint 构造简洁的浏览器访问地址。

    `endpoint` 是 OpenSandbox 对容器内 6080 端口的解析结果：
    - K8s 直连：`<pod-ip>:6080` → 生成 `http://<pod-ip>:6080/vnc.html`
    - docker 直连：`host:port/proxy/6080` → 生成 `http://host:port/proxy/6080/vnc.html`
    - K8s 网关路由：`host/sandboxes/<id>/6080`（或带签名路径）→ 原路径下追加 `/vnc.html`

    host/port 直接从 6080 endpoint 解析（不再经 SSH/22 或 /proxy 转发推导），
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


def _autotest_novnc_url(container_id: str, container_type_value: str) -> Optional[str]:
    """autotest_cloud 容器取其自身 6080 endpoint 生成 noVNC 地址；其余返回 None。

    读取 6080 失败时返回 None（不影响 SSH/状态查询）。
    """
    if container_type_value != ContainerType.AUTOTEST_CLOUD.value:
        return None
    # noinspection broad-exception
    try:
        ep = get_opensandbox_client().get_endpoint(container_id, _NOVNC_PORT)
    except Exception:
        return None
    endpoint = getattr(ep, "endpoint", None)
    if not endpoint:
        return None
    return _build_novnc_url(endpoint)


@contextmanager
def lifecycle_guard() -> Iterator[None]:
    """串行化业务恢复与物理清理，避免两者对同一记录产生竞态。"""
    with _lifecycle_lock:
        yield


def delete_missing_container_record(container_id: str) -> None:
    """删除已确认不存在的远端容器对应的本地活跃记录。"""
    with lifecycle_guard():
        with session_scope() as session:
            repo = ContainerRepository(session)
            row = repo.get(container_id)
            if row is None or row.deleted_at is not None:
                return
            cleanup_volume_for_container(row.user_id, row.service_id)
            repo.delete(container_id)
    logger.info("远端容器不存在，已删除数据库记录: %s", container_id)


def cleanup_volume_for_container(user_id: str, service_id: str) -> None:
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
    novnc_url: Optional[str] = None
    git_fin_status: str = "pending"


@dataclass(frozen=True)
class ExpirationView:
    container_id: str
    expires_at: Optional[str]


@dataclass(frozen=True)
class AdminContainerView:
    """管理端容器完整视图，合并持久化业务字段和运行时字段。"""

    container_id: str
    image: str
    user_id: str
    gitee_user: str
    gitee_repository: str
    gitee_branch: Optional[str]
    gitee_url: str
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


# ---------------------------------------------------------------------------
# 容器创建（T6.1 + T6.2）
# ---------------------------------------------------------------------------
def create_container(params: CreateContainerParams) -> CreatedContainer:
    """创建并自动启动容器（v4 §11.1）。

    - 镜像：`params.image` 为空则使用**该容器类型**的默认镜像；默认镜像未配置抛 400 语义错误。
    - 创建限制（模式 / 数量）在此校验，白名单用户跳过全部；并发通过进程互斥 + SQLite 单写者保证。
    - 容器名：随机字符串（仅表示容器本身，不承载业务信息）；端口固定 22；
      环境变量注入 `TESTAGENT_CLOUD_SERVICE_USER` /
      `TESTAGENT_CLOUD_SERVICE_ID` / `TESTAGENT_CLOUD_SERVICE_URL` /
      `TESTAGENT_CLOUD_GITEE_USER` / `TESTAGENT_CLOUD_GITEE_REPOSITORY` /
      `TESTAGENT_CLOUD_GITEE_BRANCH`（为空也注入空值）
      及 `TESTAGENT_CLOUD_AUTHORIZE_GENERAL_ACCOUNT`（true/false），并注入
      `PIP_INDEX_URL` / `NPM_CONFIG_REGISTRY` 代理源；CPU / 内存可选覆盖默认资源限制。
      OpenSandbox metadata 额外注入 `testagent-cloud=true`（来源识别）与
      `container-type=<type>`（类型识别）。
    - 类型：`testagent_cloud`（默认）只起 SSH；`autotest_cloud` 额外注入
        `TESTAGENT_ENABLE_CHROME=1`（镜像内拉起 Chrome/VNC，sshd 仍可 SSH 连入），
        两者底层为同一镜像、仅启动方式不同。
    """
    _validate_required(params)
    gitee_url = _normalise_optional_gitee_value(params.gitee_url)
    gitee_user = _normalise_optional_gitee_value(params.gitee_user)
    gitee_repository = _normalise_optional_gitee_value(params.gitee_repository)
    image = _resolve_image(params)
    # 未指定时长时，使用创建后自动业务删除的默认有效时长。
    expiration_hours = params.expiration_hours if params.expiration_hours is not None \
        else settings.container_default_expiration_hours
    if expiration_hours < 0:
        raise InvalidArgumentError("expiration_hours 不能为负数")
    with _create_lock:
        with session_scope() as session:
            repo = ContainerRepository(session)
            _check_creation_limits(
                repo,
                params.user_id,
                gitee_user,
                gitee_repository,
                params.container_type,
            )

        git_session = get_git_session_store().create_session(params.user_id)
        service_id = git_session.service_id
        if service_id is None:
            raise ExternalDependencyError("创建 Git 初始化会话失败")

        volume_plan: Optional[VolumePathPlan] = None
        volume_client: Optional[FileBrowserClient] = None
        prepared_volume_directories: Optional[PreparedVolumeDirectories] = None
        volumes: Optional[list[SandboxVolume]] = None
        if settings.filebrowser_enabled:
            try:
                volume_plan = build_volume_path_plan(params.user_id, service_id)
                assert volume_plan is not None
                volume_client = get_filebrowser_client()
                assert volume_client is not None
                prepared_volume_directories = prepare_volume_directories(
                    volume_client,
                    volume_plan,
                )
                volumes = build_sandbox_volumes(volume_plan, settings.pvc_name or "")
            except Exception as exc:  # noqa: BLE001
                rollback_volume_directories(volume_client, prepared_volume_directories)
                get_git_session_store().discard_session(service_id)
                _raise_volume_creation_error("准备卷目录", exc)

        container_type_value: str = params.container_type.value
        env: dict[str, str] = {
            "TESTAGENT_CLOUD_MODE": "1",  # 标记容器为云端
            "TESTAGENT_CLOUD_SERVICE_USER": params.user_id,
            "TESTAGENT_CLOUD_SERVICE_ID": service_id,
            "TESTAGENT_CLOUD_SERVICE_URL": settings.service_url,
            "TESTAGENT_CLOUD_GITEE_USER": gitee_user,
            "TESTAGENT_CLOUD_GITEE_REPOSITORY": gitee_repository,
            "TESTAGENT_CLOUD_GITEE_BRANCH": params.gitee_branch or "",
            "TESTAGENT_CLOUD_GITEE_URL": gitee_url,
            "TESTAGENT_CLOUD_AUTHORIZE_GENERAL_ACCOUNT": (
                "true" if params.authorize_general_account is True else "false"
            ),
            "TESTAGENT_CLOUD_PIP_URL": settings.container_pip_index_url,
            "TESTAGENT_CLOUD_NPM_URL": settings.container_npm_registry,
        }
        if params.container_type == ContainerType.AUTOTEST_CLOUD:
            # 自动化跑批容器启用 Chrome/VNC（镜像内 start.sh 据此拉起，sshd 照常运行）
            env["TESTAGENT_ENABLE_CHROME"] = "1"

        container_name = uuid.uuid4().hex[:12]
        metadata: dict[str, str] = {
            "name": container_name,
            _SOURCE_METADATA_KEY: _SOURCE_METADATA_VALUE,
            _CONTAINER_TYPE_METADATA_KEY: container_type_value,
        }
        try:
            opensandbox_client = get_opensandbox_client()
            create_kwargs: dict[str, Any] = {
                "name": container_name,
                "env": env,
                "metadata": metadata,
                "resource_limits": _resource_limits(params),
            }
            if volumes is not None:
                created = opensandbox_client.create(
                    image,
                    **create_kwargs,
                    volumes=volumes,
                )
            else:
                # 卷功能关闭时不向替身或 SDK 传递 volumes 参数。
                created = opensandbox_client.create(image, **create_kwargs)
        except Exception as exc:
            # 创建响应丢失但远端已完成创建时，SDK 无法提供容器 ID；使用本次唯一的
            # metadata name 找回并回收这个无法直接寻址的远端资源。
            try:
                _cleanup_failed_remote_create(container_name)
            finally:
                try:
                    rollback_volume_directories(volume_client, prepared_volume_directories)
                finally:
                    get_git_session_store().discard_session(service_id)
            if _is_image_not_found_error(exc):
                if params.image is None:
                    raise DefaultImageNotConfiguredError(
                        "默认镜像不存在，请联系管理员解决"
                    ) from exc
                raise InvalidArgumentError(
                    "镜像不存在，请检查镜像字段或者默认镜像设置后再创建容器"
                ) from exc
            _raise_backend_service_error("创建容器", exc)

        container_id = created.container_id
        if volume_plan is not None and volume_client is not None:
            try:
                marker_plan = build_volume_path_plan(
                    params.user_id,
                    service_id,
                    container_id,
                )
                if marker_plan.container_marker_path is None:
                    raise ExternalDependencyError("无法生成容器卷标记文件路径")
                volume_client.create_empty_file(marker_plan.container_marker_path)
            except Exception as exc:  # noqa: BLE001
                _cleanup_created_container(
                    container_id,
                    service_id,
                    prepared_volume_directories,
                    volume_client,
                )
                _raise_volume_creation_error("创建容器卷标记文件", exc)

        try:
            get_git_session_store().bind_container_id(service_id, container_id)
        except Exception as exc:
            _cleanup_created_container(
                container_id,
                service_id,
                prepared_volume_directories,
                volume_client,
            )
            raise ExternalDependencyError("绑定 Git 初始化会话失败") from exc
        created_at = _now_iso()
        try:
            with session_scope() as session:
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
            _cleanup_created_container(
                container_id,
                service_id,
                prepared_volume_directories,
                volume_client,
            )
            raise ExternalDependencyError("保存容器记录失败") from exc

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


def _cleanup_created_container(
    container_id: str,
    service_id: str,
    prepared_volume_directories: Optional[PreparedVolumeDirectories] = None,
    volume_client: Optional[FileBrowserClient] = None,
) -> None:
    """回收已创建的远端容器、卷目录和 Git 内存会话。"""
    # noinspection broad-exception
    try:
        # 数据库写入失败或会话绑定失败时尽力回收远端容器，避免留下孤儿资源。
        get_opensandbox_client().delete(container_id)
    except Exception:  # noqa: BLE001
        logger.exception("创建后回收远端容器失败: %s", container_id)
    finally:
        try:
            if prepared_volume_directories is not None and volume_client is not None:
                rollback_volume_directories(volume_client, prepared_volume_directories)
        finally:
            get_git_session_store().discard_session(service_id)


def _cleanup_failed_remote_create(container_name: str) -> None:
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
        remote_ids = client.list_container_ids(
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
            client.delete(container_id)
        except SandboxNotFoundError:
            continue
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "创建失败后回收远端容器失败: %s: %s: %s",
                container_id,
                type(exc).__name__,
                exc,
            )


def _resolve_image(params: CreateContainerParams) -> str:
    if params.image:
        return params.image
    image = _cfg_default_image(params.container_type)
    if not image:
        raise DefaultImageNotConfiguredError("没有提供默认镜像，请联系管理员解决")
    return image


def _validate_required(params: CreateContainerParams) -> None:
    if not params.user_id or not params.user_id.strip():
        raise InvalidArgumentError("user_id 不能为空")
    ensure_user_not_blacklisted(params.user_id)
    gitee_values = (
        _normalise_optional_gitee_value(params.gitee_url),
        _normalise_optional_gitee_value(params.gitee_user),
        _normalise_optional_gitee_value(params.gitee_repository),
    )
    if any(gitee_values) and not all(gitee_values):
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
    """将未填写或仅含空白的 Gitee 字段统一为空字符串。"""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise InvalidArgumentError("Gitee 字段必须为字符串")
    if not value.strip():
        return ""
    return value


def _resource_limits(params: CreateContainerParams) -> dict[str, str]:
    """解析最终资源值；调用方未覆盖时使用 limit 配置。"""
    default_cpu, default_memory = _cfg_resource_limits()
    cpu = params.cpu if params.cpu is not None else default_cpu
    memory = params.memory if params.memory is not None else default_memory
    limits: dict[str, str] = {"cpu": format(cpu, "g"), "memory": f"{memory}Gi"}
    return limits


def _check_creation_limits(
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

    if is_whitelisted(user_id):
        return

    mode = settings.container_create_limit_mode
    if mode == "user":
        if repo.count_active(user_id=user_id, container_type=container_type.value) >= 1:
            raise LimitReachedError("当前不允许同一用户创建多个同类型容器")
    elif mode == "repository":
        if repo.count_active(
            user_id=user_id,
            gitee_repository=gitee_repository,
            gitee_user=gitee_user,
            container_type=container_type.value,
        ) >= 1:
            raise LimitReachedError("当前不允许同一用户为单个仓库创建多个同类型容器")
    else:
        raise BusinessConflictError(f"不支持的创建限制模式: {mode}")

    limit = _cfg_count_limit()
    if 0 < limit <= repo.count_active():
        raise LimitReachedError("可用容器数量已达到上限")


# ---------------------------------------------------------------------------
# 状态查询与剩余时间（T6.7）
# ---------------------------------------------------------------------------
def get_status(
    container_id: str,
    *,
    enforce_user_policy: bool = True,
) -> ContainerStatusView:
    """实时查询 OpenSandbox 获取状态信息；资源指标不可用时返回空值。"""
    row = _require_active_record(container_id, enforce_user_policy=enforce_user_policy)
    try:
        status: SandboxStatus = get_opensandbox_client().get_status(container_id)
    except SandboxNotFoundError as exc:
        delete_missing_container_record(container_id)
        raise ContainerNotFoundError("后端容器不存在") from exc
    except Exception as exc:
        _raise_backend_service_error("获取容器状态", exc)

    business = resolve_container_status(row.git_fin_status, status.state)
    endpoint: Optional[str] = None
    # noinspection broad-exception
    try:
        ep: SandboxEndpoint = get_opensandbox_client().get_endpoint(container_id, Constants.CONTAINER_SSH_PORT.value)
        endpoint = ep.endpoint
    except SandboxNotFoundError as exc:
        delete_missing_container_record(container_id)
        raise ContainerNotFoundError("后端容器不存在") from exc
    except Exception as exc:  # noqa: BLE001
        _raise_backend_service_error("获取容器端点", exc)

    # A paused/transitioning sandbox cannot serve execd metrics.  Metrics are
    # optional, so do not delay the lifecycle status response for that probe.
    if business is ContainerStatus.RUNNING:
        cpu_usage, memory_usage = _get_metrics(container_id)
    else:
        cpu_usage, memory_usage = None, None

    return ContainerStatusView(
        container_id=container_id,
        status=business,
        git_fin_status=get_public_git_fin_status(row.git_fin_status),
        container_type=row.container_type,
        endpoint=endpoint,
        novnc_url=_autotest_novnc_url(container_id, row.container_type),
        started_at=status.transitioned_at,
        expires_at=add_hours_to_iso(row.created_at, row.expiration_hours),
        cpu_usage=cpu_usage,
        memory_usage=memory_usage,
        gitee_url=row.gitee_url,
        gitee_user=row.gitee_user,
        gitee_repository=row.gitee_repository,
    )


def get_container_logs(container_id: str) -> str:
    """读取指定容器日志；管理员可读取仍保留的业务删除记录。"""
    with session_scope() as session:
        row = ContainerRepository(session).get(container_id)
        if row is None:
            raise ContainerNotFoundError("容器不存在")
        ensure_user_not_blacklisted(row.user_id)

    try:
        logs: str | bytes = get_opensandbox_client().get_logs(container_id)
        return normalize_log_text(logs)
    except SandboxNotFoundError as exc:
        raise ContainerNotFoundError("后端容器不存在") from exc
    except Exception as exc:
        _raise_backend_service_error("获取容器日志错误", exc)


# ---------------------------------------------------------------------------
# 容器操作（T6.3）
# ---------------------------------------------------------------------------
def start(container_id: str) -> None:
    """启动容器；已运行重复调用幂等成功（v4 §11.3）。"""
    _require_active_record(container_id)
    try:
        get_opensandbox_client().start(container_id)
    except SandboxFailedError as exc:
        raise BusinessConflictError("失败状态的容器不能直接启动，请先删除后重新创建") from exc
    except Exception as exc:
        _raise_backend_service_error("启动容器", exc)
    from scheduler.lifecycle import mark_container_start_requested

    mark_container_start_requested(container_id)


def stop(container_id: str) -> None:
    """正常停止；已停止重复调用幂等成功（v4 §11.3）。"""
    _require_active_record(container_id)
    try:
        get_opensandbox_client().stop(container_id)
    except Exception as exc:
        _raise_backend_service_error("停止容器", exc)
    # OpenSandbox Pause is accepted before the runtime necessarily reports
    # Paused.  Publish a transition state immediately instead of serving an
    # older running snapshot from the admin API.
    from scheduler.lifecycle import mark_container_stop_requested

    mark_container_stop_requested(container_id)


def restart(container_id: str) -> None:
    """停止并重新启动；Container ID 不变（v4 §11.3）。"""
    _require_active_record(container_id)
    try:
        get_opensandbox_client().restart(container_id)
    except SandboxFailedError as exc:
        raise BusinessConflictError("失败状态的容器不能直接重启，请先删除后重新创建") from exc
    except Exception as exc:
        _raise_backend_service_error("重启容器", exc)
    from scheduler.lifecycle import mark_container_start_requested

    mark_container_start_requested(container_id)


# ---------------------------------------------------------------------------
# 业务删除（T6.4）
# ---------------------------------------------------------------------------
def business_delete(container_id: str) -> None:
    """业务删除：OpenSandbox Stop + 写 `deleted_at`，底层容器保留。

    已业务删除的容器再次删除一律视为不存在（404 语义，v4 §14.9 由最新约定覆盖）。
    """
    with session_scope() as session:
        repo = ContainerRepository(session)
        row = repo.get(container_id)
        if row is None:
            raise ContainerNotFoundError("容器不存在")
        ensure_user_not_blacklisted(row.user_id)
        if row.deleted_at is not None:
            raise ContainerNotFoundError("容器不存在")
        try:
            get_opensandbox_client().stop(container_id)
        except Exception as exc:
            _raise_backend_service_error("停止容器", exc)
        repo.business_delete(container_id, _now_iso())


# ---------------------------------------------------------------------------
# 恢复（T6.5，仅管理 API）
# ---------------------------------------------------------------------------
def restore(container_id: str, expiration_hours: int) -> ContainerStatusView:
    """恢复：清除 `deleted_at`、重写 `created_at`（当前时间）与 `expiration_hours`，并启动容器。

    `authorize_general_account` 不重新指定，保持原值（变更 #2）。
    """
    if expiration_hours < 0:
        raise InvalidArgumentError("expiration_hours 不能为负数")
    with lifecycle_guard():
        with session_scope() as session:
            repo = ContainerRepository(session)
            row = repo.get(container_id)
            if row is None:
                raise ContainerNotFoundError("容器不存在")
            ensure_user_not_blacklisted(row.user_id)
            if row.deleted_at is None:
                raise BusinessConflictError("容器未处于业务删除状态，无法恢复")
            try:
                # OpenSandbox 的普通 start 对不存在容器按幂等成功处理；恢复必须先严格确认远端记录仍存在。
                get_opensandbox_client().get_status(container_id)
            except SandboxNotFoundError as exc:
                raise ContainerNotFoundError("后端容器不存在，无法恢复") from exc
            except Exception as exc:
                _raise_backend_service_error("检查容器状态", exc)
            try:
                get_opensandbox_client().start(container_id)
            except Exception as exc:
                _raise_backend_service_error("启动容器", exc)
            repo.business_restore(container_id, _now_iso(), expiration_hours)

    return get_status(container_id)


def _get_metrics(container_id: str) -> tuple[Optional[float], Optional[float]]:
    """读取容器资源使用率；指标不可用时返回空值且不影响状态查询。"""
    get_metrics = getattr(get_opensandbox_client(), "get_metrics", None)
    if not callable(get_metrics):
        return None, None
    # noinspection broad-exception
    try:
        # noinspection calling-non-callable
        metrics = get_metrics(container_id)
    except SandboxNotFoundError as exc:
        delete_missing_container_record(container_id)
        raise ContainerNotFoundError("后端容器不存在") from exc
    except Exception:
        return None, None
    if not isinstance(metrics, SandboxMetrics):
        return None, None
    return metrics.cpu_usage, metrics.memory_usage


# ---------------------------------------------------------------------------
# 立即删除（T6.6，仅管理 API）
# ---------------------------------------------------------------------------
def permanent_delete(container_id: str) -> None:
    """立即删除：OpenSandbox Delete 物理删除底层容器 + 删除 SQLite 记录（v4 §11.4）。"""
    with lifecycle_guard():
        with session_scope() as session:
            repo = ContainerRepository(session)
            row = repo.get(container_id)
            if row is None:
                raise ContainerNotFoundError("容器不存在")
            ensure_user_not_blacklisted(row.user_id)
            try:
                get_opensandbox_client().delete(container_id)
            except SandboxNotFoundError:
                # 远端已不存在，仍需清理本地卷和记录。
                pass
            except Exception as exc:
                _raise_backend_service_error("删除容器", exc)
            cleanup_volume_for_container(row.user_id, row.service_id)
            repo.delete(container_id)


# ---------------------------------------------------------------------------
# 设置业务有效时长（T6.8）
# ---------------------------------------------------------------------------
def set_expiration(container_id: str, expiration_hours: int) -> ExpirationView:
    """仅修改 `expiration_hours`，不重置 `created_at`；0 表示永不过期（v4 §14.8）。"""
    if expiration_hours < 0:
        raise InvalidArgumentError("expiration_hours 不能为负数")
    with lifecycle_guard():
        with session_scope() as session:
            repo = ContainerRepository(session)
            row = repo.get(container_id)
            if row is None:
                raise ContainerNotFoundError("容器不存在")
            ensure_user_not_blacklisted(row.user_id)
            if row.deleted_at is not None:
                raise ContainerNotFoundError("容器不存在")
            repo.update_expiration(container_id, expiration_hours)
            expiration = add_hours_to_iso(row.created_at, expiration_hours)
    return ExpirationView(container_id=container_id, expires_at=expiration)


# ---------------------------------------------------------------------------
# 业务条件查询（REST GET /containers）
# ---------------------------------------------------------------------------
def query_container_ids(
    user_id: str,
    *,
    gitee_user: Optional[str] = None,
    gitee_repository: Optional[str] = None,
    gitee_branch: Optional[str] = None,
    container_type: Optional[ContainerType] = None,
) -> list[str]:
    """按业务条件查询容器 ID（AND 组合，不含业务已删除，v4 §14.6）。

    `user_id` 必填：REST 端点无认证，禁止不带用户标识枚举全部容器。
    """
    if not user_id or not user_id.strip():
        raise InvalidArgumentError("user_id 不能为空")
    ensure_user_not_blacklisted(user_id)
    with session_scope() as session:
        rows = ContainerRepository(session).list_active(
            user_id=user_id,
            gitee_user=gitee_user,
            gitee_repository=gitee_repository,
            gitee_branch=gitee_branch,
            container_type=container_type.value if container_type is not None else None,
        )
        return [r.container_id for r in rows]


def query_container_statuses(
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
    with session_scope() as session:
        rows = ContainerRepository(session).list_active(
            user_id=user_id,
            gitee_user=gitee_user,
            gitee_repository=gitee_repository,
            gitee_branch=gitee_branch,
            container_type=container_type.value if container_type is not None else None,
        )
        container_ids = [r.container_id for r in rows]
    return [get_status(container_id) for container_id in container_ids]


def list_orphan_container_ids() -> list[str]:
    """查询带本服务来源标记、但数据库中没有记录的远端容器。"""
    with _create_lock:
        return _list_orphan_container_ids_locked()


def delete_orphan_containers(container_ids: list[str]) -> None:
    """删除指定孤儿容器；单个失败不影响其他合法 ID 的处理。"""
    requested_ids = _normalise_orphan_container_ids(container_ids)
    # 即使请求声称目标是孤儿容器，也先拒绝已登记黑名单用户的资源，避免
    # 以孤儿校验路径绕过 container_id 归属策略。
    for container_id in requested_ids:
        _ensure_container_owner_allowed_if_present(container_id)
    not_orphan_ids: list[str] = []
    failed_ids: list[str] = []

    # 将重新比对和删除放在同一创建锁中，避免新建容器在快照之后尚未
    # 落库时被误判为孤儿。
    with _create_lock:
        orphan_ids = set(_list_orphan_container_ids_locked())
        client = get_opensandbox_client()
        for container_id in requested_ids:
            if container_id not in orphan_ids:
                not_orphan_ids.append(container_id)
                continue
            try:
                client.delete(container_id)
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
        raise InvalidArgumentError(f"以下容器不是孤儿容器: {details}")
    if failed_ids:
        raise ExternalDependencyError(
            f"以下孤儿容器删除失败: {', '.join(failed_ids)}"
        )


def delete_sandboxes_by_pod_names(pod_names: list[str]) -> None:
    """按 K8s Pod 名称物理删除沙盒（仅管理 API）。

    直接 `kubectl delete pod` 会被 BatchSandbox 控制器重建（表现为「自动重启」）。
    本接口把 Pod 名称解析回 OpenSandbox 沙盒 ID 后调用管理面删除，连带删除
    BatchSandbox CR，Pod 不会被重建。已不存在的沙盒按幂等成功处理，并清理同 ID
    的本地数据库记录。
    """
    requested_names = _normalise_pod_names(pod_names)
    client = get_opensandbox_client()
    known_ids = _list_managed_container_ids(client)
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
        _ensure_container_owner_allowed_if_present(container_id)

    for pod_name, container_id in resolved_ids:
        with lifecycle_guard():
            try:
                client.delete(container_id)
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
            with session_scope() as session:
                repo = ContainerRepository(session)
                row = repo.get(container_id)
                if row is not None:
                    cleanup_volume_for_container(row.user_id, row.service_id)
                    repo.delete(container_id)

    if invalid_names:
        raise InvalidArgumentError(
            f"以下 Pod 名称无法解析为沙盒 ID: {', '.join(invalid_names)}"
        )
    if failed_names:
        raise ExternalDependencyError(
            f"以下 Pod 删除失败: {', '.join(failed_names)}"
        )


# ---------------------------------------------------------------------------
# 管理端容器完整查询与数量限制
# ---------------------------------------------------------------------------
def list_admin_containers() -> list[AdminContainerView]:
    """列出全部容器，包括业务已删除记录。"""
    with session_scope() as session:
        rows = list(ContainerRepository(session).list_all())
    views: list[AdminContainerView] = []
    for row in rows:
        try:
            views.append(_to_admin_view(row))
        except ContainerNotFoundError:
            # 状态查询已同步清理远端缺失的本地活跃记录，不再返回该条目。
            continue
    return views


def get_admin_container(container_id: str) -> AdminContainerView:
    """查询管理端容器完整信息；业务已删除记录仍可查询。"""
    with session_scope() as session:
        row = ContainerRepository(session).get(container_id)
    if row is None:
        raise ContainerNotFoundError("容器不存在")
    ensure_user_not_blacklisted(row.user_id)
    return _to_admin_view(row)


def get_container_limit() -> ContainerLimitView:
    """读取容器数量及资源限制配置。"""
    cpu, memory = _cfg_resource_limits()
    return ContainerLimitView(
        container_limit=_cfg_count_limit(),
        cpu=cpu,
        memory=memory,
    )


def set_container_limit(
    container_limit: int,
    *,
    cpu: float,
    memory: int,
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
    _cfg_set_count_limit(container_limit)
    _cfg_set_resource_limits(cpu, memory)
    return get_container_limit()


def get_admin_state() -> AdminStateView:
    """读取未业务删除容器及白名单/管理员清单的基础统计。"""
    with session_scope() as session:
        container_repo = ContainerRepository(session)
        whitelist_ids = {
            row.user_id for row in WhitelistUserRepository(session).list_all()
        }
        admin_ids = {row.user_id for row in AdminUserRepository(session).list_all()}
        containers = container_repo.list_active()

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
def _require_active_record(
    container_id: str,
    *,
    enforce_user_policy: bool = True,
) -> ContainerRow:
    """取业务有效（非业务已删除）容器记录；不存在或已业务删除抛 404 语义错误。"""
    with session_scope() as session:
        row = ContainerRepository(session).get(container_id)
    if row is None:
        raise ContainerNotFoundError("容器不存在")
    if enforce_user_policy:
        ensure_user_not_blacklisted(row.user_id)
    if row.deleted_at is not None:
        raise ContainerNotFoundError("容器不存在")
    return row


def _ensure_container_owner_allowed_if_present(container_id: str) -> None:
    """校验已登记容器的用户；孤儿容器没有可确认的业务归属。"""
    with session_scope() as session:
        row = ContainerRepository(session).get(container_id)
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


def _list_managed_container_ids(client: OpenSandboxClient) -> set[str]:
    """尽力获取带本服务来源标记的沙盒 ID；失败时返回空集合（仍可用 UUID 解析）。"""
    try:
        return set(
            client.list_container_ids(
                metadata={_SOURCE_METADATA_KEY: _SOURCE_METADATA_VALUE}
            )
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "按 Pod 名称删除时查询沙盒列表失败（改用名称解析）: %s: %s",
            type(exc).__name__,
            exc,
        )
        return set()


def _resolve_container_id_from_pod_name(
    pod_name: str,
    known_ids: set[str],
) -> Optional[str]:
    """将 K8s Pod 名称解析为 OpenSandbox 沙盒 ID。

    OpenSandbox 用随机 UUID4 作为沙盒 ID，其 BatchSandbox CR 与 Pod 名称前缀一致，
    Pod 名称形如 `<sandbox-id>-<随机后缀>`（多副本时可能还有索引段）。解析顺序：
    1. Pod 名称本身即沙盒 ID（已记录或本身为合法 UUID）；
    2. 与已知沙盒 ID 前缀匹配；
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
    """判断字符串是否为合法 UUID（OpenSandbox 沙盒 ID 形态）。"""
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


def _list_orphan_container_ids_locked() -> list[str]:
    """在 `_create_lock` 内查询并返回孤儿 ID。"""
    try:
        remote_ids = get_opensandbox_client().list_container_ids(
            metadata={
                _SOURCE_METADATA_KEY: _SOURCE_METADATA_VALUE,
            }
        )
    except Exception as exc:
        _raise_backend_service_error("查询孤儿容器", exc)

    with session_scope() as session:
        stored_ids = set(ContainerRepository(session).list_all_ids())

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
    raise ExternalDependencyError("后端服务错误") from exc


def _raise_volume_creation_error(operation: str, exc: Exception) -> NoReturn:
    """将卷准备/标记失败转换为安全的业务错误。"""
    if isinstance(exc, VolumePathConflictError):
        raise exc
    if isinstance(exc, FileBrowserConflictError):
        raise VolumePathConflictError("FileBrowser 卷路径已存在或发生冲突") from None
    logger.error("FileBrowser %s失败: %s", operation, type(exc).__name__)
    raise ExternalDependencyError("FileBrowser 服务错误") from None


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


def _to_admin_view(row: ContainerRow) -> AdminContainerView:
    cpu_usage: Optional[float] = None
    memory_usage: Optional[float] = None
    # 有效期来自持久化业务字段，不能依赖运行时状态缓存。
    expires_at = add_hours_to_iso(row.created_at, row.expiration_hours)
    if row.deleted_at is not None:
        status = ContainerStatus.BUSINESS_DELETED
        endpoint: Optional[str] = None
        started_at: Optional[str] = None
    else:
        runtime = _get_admin_runtime(row.container_id)
        status = resolve_container_status(row.git_fin_status, runtime.status)
        endpoint = runtime.endpoint
        started_at = runtime.started_at
        cpu_usage = runtime.cpu_usage
        memory_usage = runtime.memory_usage

    return AdminContainerView(
        container_id=row.container_id,
        image=row.image,
        container_type=row.container_type,
        user_id=row.user_id,
        gitee_url=row.gitee_url,
        gitee_user=row.gitee_user,
        gitee_repository=row.gitee_repository,
        gitee_branch=row.gitee_branch,
        created_at=row.created_at,
        expiration_hours=row.expiration_hours,
        authorize_general_account=bool(row.authorize_general_account),
        status=status,
        git_fin_status=get_public_git_fin_status(row.git_fin_status),
        endpoint=endpoint,
        novnc_url=_autotest_novnc_url(row.container_id, row.container_type),
        started_at=started_at,
        expires_at=expires_at,
        cpu_usage=cpu_usage,
        memory_usage=memory_usage,
        deleted_at=row.deleted_at,
        business_deleted=row.deleted_at is not None,
    )


def _get_admin_runtime(container_id: str) -> ContainerStatusView:
    """优先使用 Scheduler 快照，首次刷新前才回退到实时查询。"""
    # 局部导入避免 application.container 与 scheduler.lifecycle 的模块循环依赖。
    from scheduler.lifecycle import get_cached_runtime, get_cached_status

    cached = get_cached_runtime(container_id)
    if cached is not None:
        return ContainerStatusView(
            container_id=container_id,
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
        return ContainerStatusView(container_id=container_id, status=cached_status)

    return get_status(container_id, enforce_user_policy=False)
