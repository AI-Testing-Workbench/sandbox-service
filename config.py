"""
应用配置层：环境变量加载与校验、全局常量、settings 读写接口。

规范依据：
- v4 §5.1 环境变量（`TA_SS_*`）：必填缺失或非法值启动失败并明确报错，整数无法解析启动失败，
  `TA_SS_CONTAINER_CREATE_LIMIT_MODE` 仅允许 `user` / `repository`。
- v4 §5.2 全局常量（`Constants` 枚举）。
- v4 §4.3 / §6.2.2 settings（`default_image` / `container_count_limit`）读写归属本模块；
  应用层与接口层不直接操作 settings 表。

配置在模块加载时初始化（Python 模块缓存保证整个进程仅执行一次）。
"""

from __future__ import annotations

import logging
import math
import os
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterator, Literal, Optional, cast
from urllib.parse import urlsplit, urlunsplit

from domain.models import ContainerType

logger = logging.getLogger(__name__)

__all__ = [
    "ConfigError",
    "Constants",
    "settings",
    "get_default_image",
    "set_default_image",
    "get_container_count_limit",
    "set_container_count_limit",
    "get_container_resource_limits",
    "set_container_resource_limits",
]

_ENV_PREFIX = "TA_SS_"

_LOG_LEVELS: tuple[str, ...] = ("DEBUG", "INFO", "WARNING", "ERROR")

# 容器创建限制
CONTAINER_CREATE_LIMIT_MODES: tuple[str, ...] = ("user", "repository")
#: 容器创建限制模式可取值
ContainerCreateLimitMode = Literal["user", "repository"]


class ConfigError(Exception):
    """配置缺失或非法，导致服务启动失败。"""
    ...


class Constants(Enum):
    """全局常量（v4 §5.2），不通过环境变量或数据库配置。"""

    #: 项目根目录
    APP_ROOT_PATH = str(Path(__file__).resolve().parent)
    #: SQLite 文件路径（v4 §6.3）
    DB_PATH = "./data/sandbox.db"
    #: 镜像上传临时文件目录
    UPLOAD_TEMP_PATH = "./temp"
    #: 允许上传的镜像文件扩展名
    UPLOAD_ALLOWED_EXTENSIONS = (".tar", ".tar.gz")
    #: 容器内 sshd 监听端口
    CONTAINER_SSH_PORT = 22
    #: 管理端分页默认每页条数（SHOULD）
    DEFAULT_PAGE_SIZE = 20
    #: 系统统一时区（UTC+8）
    TIMEZONE = "Asia/Shanghai"


@dataclass(frozen=True)
class Settings:
    """加载并校验后的运行配置（v4 §5.1）。"""

    #: OpenSandbox 服务地址（必填）
    opensandbox_url: str
    #: OpenSandbox 认证 Key；未设置则不发送
    opensandbox_api_key: Optional[str]
    #: 容器创建限制模式：user / repository
    container_create_limit_mode: ContainerCreateLimitMode
    #: 容器创建后的默认有效时长（小时）；到期后由 Scheduler 自动业务删除
    container_default_expiration_hours: int
    #: 业务删除后的保留时长（小时）；到期后由 Scheduler 自动物理删除
    container_retention_hours: int
    #: 容器数量限制默认值（settings 表未设置时使用）；0 表示取消数量限制
    container_default_count_limit: int
    #: Registry 地址默认值
    image_default_registry: str
    #: 镜像命名空间默认值
    image_default_namespace: str
    #: Scheduler 轮询周期（秒）
    scheduler_poll_interval_seconds: int
    #: REST API 监听端口（监听地址固定 0.0.0.0）
    rest_api_port: int
    #: REST 文档 Basic 鉴权用户名
    rest_api_username: str
    #: REST 文档 Basic 鉴权密码
    rest_api_password: str
    #: 日志级别：DEBUG / INFO / WARNING / ERROR
    log_level: str
    #: 容器内 PIP 包索引地址；为空时不设置，非空必须为 HTTP/HTTPS URL
    container_pip_index_url: str = ""
    #: 容器内 NPM Registry 地址；为空时不设置，非空必须为 HTTP/HTTPS URL
    container_npm_registry: str = ""
    #: 容器默认 CPU 核数；可由管理端 limit 配置覆盖
    container_default_cpu: float = 1.0
    #: 容器默认内存大小，单位 Gi；可由管理端 limit 配置覆盖
    container_default_memory: int = 1
    #: FileBrowser 配置的基础地址；未配置时为 None
    filebrowser_url: Optional[str] = None
    #: FileBrowser 资源 API 地址；由基础地址规范化追加 /api
    filebrowser_api_url: Optional[str] = None
    #: FileBrowser 用户生成的 API Token；未配置时为 None
    filebrowser_api_key: Optional[str] = None
    #: FileBrowser 登录用户名；未配置时为 None
    filebrowser_username: Optional[str] = None
    #: FileBrowser 登录密码；未配置时为 None
    filebrowser_password: Optional[str] = None
    #: FileBrowser 认证模式：api_key / password；未启用时为 None
    filebrowser_auth_mode: Optional[Literal["api_key", "password"]] = None
    #: OpenSandbox 使用的预先存在的 PVC 名称
    pvc_name: Optional[str] = None
    #: FileBrowser 卷功能是否启用
    filebrowser_enabled: bool = False


def _string(name: str, default: Optional[str] = None) -> str:
    value = os.environ.get(_ENV_PREFIX + name)
    if not value:
        if default is None:
            raise ConfigError(f"缺少必填环境变量 {_ENV_PREFIX + name}")
        return default
    return value


def _string_or_none(name: str) -> Optional[str]:
    value = os.environ.get(_ENV_PREFIX + name)
    return value if value else None


def _optional_url(name: str) -> str:
    """读取可选 HTTP(S) URL；空值表示不设置对应代理。"""
    value = os.environ.get(_ENV_PREFIX + name, "")
    if value == "":
        return value
    if any(char.isspace() for char in value):
        raise ConfigError(
            f"环境变量 {_ENV_PREFIX + name} 必须为合法的 HTTP/HTTPS URL: {value!r}"
        )
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        _ = parsed.port  # 访问 port 以校验非法端口格式和范围
    except ValueError as exc:
        raise ConfigError(
            f"环境变量 {_ENV_PREFIX + name} 必须为合法的 HTTP/HTTPS URL: {value!r}"
        ) from exc
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc or not hostname:
        raise ConfigError(
            f"环境变量 {_ENV_PREFIX + name} 必须为合法的 HTTP/HTTPS URL: {value!r}"
        )
    return value


def _optional_env_value(name: str, *, strip: bool = False) -> Optional[str]:
    """读取可选配置；空值及全空白值均视为未配置。"""
    value = os.environ.get(_ENV_PREFIX + name)
    if value is None or not value.strip():
        return None
    return value.strip() if strip else value


# noinspection unnecessary-cast
def _filebrowser_urls(raw_value: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """校验 FileBrowser 基础地址并生成内部资源 API 地址。"""
    if raw_value is None:
        return None, None

    value = raw_value.strip()
    if not value:
        return None, None

    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        _ = parsed.port  # 访问 port 以校验非法端口格式和范围
        has_credentials = parsed.username is not None or parsed.password is not None
    except ValueError as exc:
        raise ConfigError(
            f"环境变量 {_ENV_PREFIX}FILEBROWSER_URL 必须为不含认证信息的合法 HTTP/HTTPS URL"
        ) from exc

    if (
        any(char.isspace() for char in value)
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or parsed.scheme.lower() not in ("http", "https")
        or not parsed.netloc
        or not hostname
        or has_credentials
        or parsed.query
        or parsed.fragment
        or "?" in value
        or "#" in value
    ):
        raise ConfigError(
            f"环境变量 {_ENV_PREFIX}FILEBROWSER_URL 必须为不含认证信息的合法 HTTP/HTTPS URL"
        )

    # Keep the configured base address for the status API, while the client
    # receives a canonical URL ending in exactly one /api segment. A loopback
    # address points at the sandbox container itself, so use Docker Desktop's
    # host gateway for requests made from the container.
    scheme = cast(str, parsed.scheme).lower()
    netloc = cast(str, parsed.netloc)
    path = cast(str, parsed.path)
    base_path = path.rstrip("/")
    api_path = base_path if base_path == "/api" or base_path.endswith("/api") else (
        f"{base_path}/api" if base_path else "/api"
    )
    client_netloc = netloc
    if hostname in ("127.0.0.1", "localhost", "::1"):
        client_netloc = "host.docker.internal"
        if parsed.port is not None:
            client_netloc += f":{parsed.port}"
    api_url = urlunsplit((scheme, client_netloc, api_path, "", ""))
    return value, api_url


def _image_registry() -> str:
    value = _string("IMAGE_DEFAULT_REGISTRY")
    # noinspection HttpUrlsUsage
    for prefix in ("http://", "https://"):
        if value.startswith(prefix):
            return value[len(prefix):]
    return value


def _int(
    name: str,
    default: Optional[int] = None,
    *,
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
) -> int:
    raw = os.environ.get(_ENV_PREFIX + name)
    if raw is None or raw == "":
        if default is None:
            raise ConfigError(f"缺少必填环境变量 {_ENV_PREFIX + name}")
        value = default
    else:
        try:
            value = int(raw)
        except ValueError:
            raise ConfigError(f"环境变量 {_ENV_PREFIX + name} 无法解析为整数: {raw!r}")

    if minimum is not None and value < minimum:
        raise ConfigError(
            f"环境变量 {_ENV_PREFIX + name} 必须大于等于 {minimum}: {value}"
        )
    if maximum is not None and value > maximum:
        raise ConfigError(
            f"环境变量 {_ENV_PREFIX + name} 必须小于等于 {maximum}: {value}"
        )
    return value


def _float(
    name: str,
    default: Optional[float] = None,
    *,
    minimum: Optional[float] = None,
    maximum: Optional[float] = None,
) -> float:
    raw = os.environ.get(_ENV_PREFIX + name)
    if raw is None or raw == "":
        if default is None:
            raise ConfigError(f"缺少必填环境变量 {_ENV_PREFIX + name}")
        value = default
    else:
        try:
            value = float(raw)
        except ValueError:
            raise ConfigError(f"环境变量 {_ENV_PREFIX + name} 无法解析为数字: {raw!r}")

    if not math.isfinite(value):
        raise ConfigError(f"环境变量 {_ENV_PREFIX + name} 必须为有限数字: {value!r}")
    if minimum is not None and value < minimum:
        raise ConfigError(
            f"环境变量 {_ENV_PREFIX + name} 必须大于等于 {minimum}: {value}"
        )
    if maximum is not None and value > maximum:
        raise ConfigError(
            f"环境变量 {_ENV_PREFIX + name} 必须小于等于 {maximum}: {value}"
        )
    return value


_create_limit_mode = cast(ContainerCreateLimitMode, _string("CONTAINER_CREATE_LIMIT_MODE"))
if _create_limit_mode not in CONTAINER_CREATE_LIMIT_MODES:
    raise ConfigError(
        f"环境变量 {_ENV_PREFIX}CONTAINER_CREATE_LIMIT_MODE 仅允许 "
        f"{' / '.join(CONTAINER_CREATE_LIMIT_MODES)}，当前值: {_create_limit_mode!r}"
    )

_log_level = _string("LOG_LEVEL", "INFO")
if _log_level not in _LOG_LEVELS:
    raise ConfigError(
        f"环境变量 {_ENV_PREFIX}LOG_LEVEL 仅允许 {' / '.join(_LOG_LEVELS)}，当前值: {_log_level!r}"
    )

_filebrowser_url, _filebrowser_api_url = _filebrowser_urls(
    _optional_env_value("FILEBROWSER_URL", strip=True)
)
_filebrowser_api_key = _optional_env_value("FILEBROWSER_API_KEY")
_filebrowser_username = _optional_env_value("FILEBROWSER_USERNAME")
_filebrowser_password = _optional_env_value("FILEBROWSER_PASSWORD")
_pvc_name = _optional_env_value("PVC_NAME", strip=True)
_filebrowser_auth_mode: Optional[Literal["api_key", "password"]] = None
_filebrowser_enabled = False
if (_filebrowser_username is None) != (_filebrowser_password is None):
    raise ConfigError(
        "FileBrowser 用户名和密码必须同时配置"
    )
if _filebrowser_url is None:
    # URL 为空时，其他 FileBrowser 配置不单独启用卷功能；保留配置值供
    # 诊断/状态模型使用，但客户端不会被创建。
    if (
        _filebrowser_api_key is not None
        or _filebrowser_username is not None
        or _filebrowser_password is not None
    ):
        logger.warning("FileBrowser 卷功能未启用：缺少 TA_SS_FILEBROWSER_URL")
else:
    if _pvc_name is None:
        raise ConfigError(
            "FileBrowser 卷功能已配置，但缺少必填环境变量 TA_SS_PVC_NAME"
        )
    if _filebrowser_username is not None and _filebrowser_password is not None:
        _filebrowser_auth_mode = "password"
    elif _filebrowser_api_key is not None:
        _filebrowser_auth_mode = "api_key"
    else:
        raise ConfigError(
            "FileBrowser 必须配置 API KEY 或完整的用户名密码认证"
        )
    _filebrowser_enabled = True

settings: Settings = Settings(
    opensandbox_url=_string("OPENSANDBOX_URL"),
    opensandbox_api_key=_string_or_none("OPENSANDBOX_API_KEY"),
    container_create_limit_mode=_create_limit_mode,
    container_retention_hours=_int("CONTAINER_RETENTION_HOURS", default=24 * 7, minimum=0),
    container_default_expiration_hours=_int(
        "CONTAINER_DEFAULT_EXPIRATION_HOURS", default=24, minimum=0
    ),
    container_default_count_limit=_int("CONTAINER_DEFAULT_COUNT_LIMIT", default=0, minimum=0),
    image_default_registry=_image_registry(),
    image_default_namespace=_string("IMAGE_DEFAULT_NAMESPACE", "testagent"),
    rest_api_username=_string("REST_API_USERNAME"),
    rest_api_password=_string("REST_API_PASSWORD"),
    scheduler_poll_interval_seconds=_int("SCHEDULER_POLL_INTERVAL_SECONDS", 5, minimum=1),
    rest_api_port=_int("REST_API_PORT", 8080, minimum=1, maximum=65535),
    log_level=_log_level,
    container_pip_index_url=_optional_url("PROXY_PIP_INDEX_URL"),
    container_npm_registry=_optional_url("PROXY_NPM_REGISTRY"),
    container_default_cpu=_float("CONTAINER_DEFAULT_CPU", 1.0, minimum=0.01),
    container_default_memory=_int("CONTAINER_DEFAULT_MEMORY", 1, minimum=1),
    filebrowser_url=_filebrowser_url,
    filebrowser_api_url=_filebrowser_api_url if _filebrowser_enabled else None,
    filebrowser_api_key=_filebrowser_api_key,
    filebrowser_username=_filebrowser_username,
    filebrowser_password=_filebrowser_password,
    filebrowser_auth_mode=_filebrowser_auth_mode,
    pvc_name=_pvc_name,
    filebrowser_enabled=_filebrowser_enabled,
)


# ---------------------------------------------------------------------------
# settings 读写（v4 §4.3 / §6.2.2）：默认镜像、容器数量及资源限制
# 应用层与接口层通过本模块访问，不直接操作 settings 表。
# ---------------------------------------------------------------------------

SETTINGS_KEY_DEFAULT_IMAGE = "default_image"
SETTINGS_KEY_CONTAINER_COUNT_LIMIT = "container_count_limit"
SETTINGS_KEY_CONTAINER_CPU_LIMIT = "container_cpu_limit"
SETTINGS_KEY_CONTAINER_MEMORY_LIMIT = "container_memory_limit"

#: autotest_cloud 类型的默认镜像 key（testagent_cloud 沿用历史 key `default_image`）
SETTINGS_KEY_DEFAULT_IMAGE_AUTOTEST_CLOUD = "default_image_autotest_cloud"

_DEFAULT_IMAGE_SETTINGS_KEYS = {
    ContainerType.TESTAGENT_CLOUD: SETTINGS_KEY_DEFAULT_IMAGE,
    ContainerType.AUTOTEST_CLOUD: SETTINGS_KEY_DEFAULT_IMAGE_AUTOTEST_CLOUD,
}


def _default_image_settings_key(container_type: ContainerType) -> str:
    """按容器类型返回默认镜像在 settings 表中的 key。"""
    return _DEFAULT_IMAGE_SETTINGS_KEYS[container_type]


@contextmanager
def _settings_scope() -> Iterator:
    from infra.db import session_scope
    from infra.repositories import SettingsRepository

    with session_scope() as session:
        yield SettingsRepository(session)


def get_default_image(
    container_type: ContainerType = ContainerType.TESTAGENT_CLOUD,
) -> Optional[str]:
    """读取指定容器类型的默认镜像完整引用；未设置返回 None。

    `container_type` 缺省为 `testagent_cloud`，历史 key 行为保持不变。
    """
    with _settings_scope() as repo:
        row = repo.get(_default_image_settings_key(container_type))
    return row.value if row is not None else None


def set_default_image(
    value: Optional[str],
    container_type: ContainerType = ContainerType.TESTAGENT_CLOUD,
) -> None:
    """设置指定容器类型的默认镜像完整引用；传 None 表示取消默认。"""
    key = _default_image_settings_key(container_type)
    with _settings_scope() as repo:
        if value is None:
            repo.delete(key)
        else:
            repo.set(key, value)


def get_container_count_limit() -> int:
    """读取容器数量限制；数据库未设置时返回 `TA_SS_CONTAINER_DEFAULT_COUNT_LIMIT`。"""
    with _settings_scope() as repo:
        row = repo.get(SETTINGS_KEY_CONTAINER_COUNT_LIMIT)
    if row is None:
        return settings.container_default_count_limit
    try:
        return int(row.value)
    except ValueError:
        raise ConfigError(f"数据库中的容器数量限制非法: {row.value!r}")


def set_container_count_limit(value: int) -> None:
    """设置容器数量限制。"""
    with _settings_scope() as repo:
        repo.set(SETTINGS_KEY_CONTAINER_COUNT_LIMIT, str(int(value)))


def get_container_resource_limits() -> tuple[float, int]:
    """读取容器 CPU/内存限制；数据库未设置时返回环境变量默认值。"""
    with _settings_scope() as repo:
        cpu_row = repo.get(SETTINGS_KEY_CONTAINER_CPU_LIMIT)
        memory_row = repo.get(SETTINGS_KEY_CONTAINER_MEMORY_LIMIT)

    if cpu_row is None:
        cpu = settings.container_default_cpu
    else:
        try:
            cpu = float(cpu_row.value)
        except ValueError:
            raise ConfigError(f"数据库中的容器 CPU 限制非法: {cpu_row.value!r}")
        if not math.isfinite(cpu) or cpu <= 0:
            raise ConfigError(f"数据库中的容器 CPU 限制非法: {cpu_row.value!r}")

    if memory_row is None:
        memory = settings.container_default_memory
    else:
        try:
            memory = int(memory_row.value)
        except ValueError:
            raise ConfigError(f"数据库中的容器内存限制非法: {memory_row.value!r}")
        if memory <= 0:
            raise ConfigError(f"数据库中的容器内存限制非法: {memory_row.value!r}")

    return cpu, memory


def set_container_resource_limits(cpu: float, memory: int) -> None:
    """设置容器 CPU/内存限制。"""
    if (
        isinstance(cpu, bool)
        or not isinstance(cpu, (int, float))
        or not math.isfinite(cpu)
        or cpu <= 0
    ):
        raise ConfigError("容器 CPU 限制必须为正数")
    if isinstance(memory, bool) or not isinstance(memory, int) or memory <= 0:
        raise ConfigError("容器内存限制必须为正整数")
    with _settings_scope() as repo:
        repo.set(SETTINGS_KEY_CONTAINER_CPU_LIMIT, format(cpu, "g"))
        repo.set(SETTINGS_KEY_CONTAINER_MEMORY_LIMIT, str(memory))
