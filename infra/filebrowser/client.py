"""
FileBrowser Quantum API 客户端。

本模块只封装卷生命周期所需的资源查询、目录/空文件创建、目录子项查询和递归删除，
不提供通用反向代理能力。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Mapping, Optional
from urllib.parse import quote, urlsplit

import requests

from infra.filebrowser.types import FileBrowserItem, FileBrowserItems, FileBrowserResource

logger = logging.getLogger(__name__)

__all__ = [
    "FILEBROWSER_SOURCE",
    "FileBrowserError",
    "FileBrowserConfigurationError",
    "FileBrowserNotFoundError",
    "FileBrowserConflictError",
    "FileBrowserAuthError",
    "FileBrowserServerError",
    "FileBrowserTransportError",
    "FileBrowserResponseError",
    "FileBrowserClient",
]

# FileBrowser 配置的 source 名称由卷管理需求固定，不能从请求或环境变量覆盖。
FILEBROWSER_SOURCE = "根目录"
_RESOURCES_ENDPOINT = "resources"
_ITEMS_ENDPOINT = "resources/items"


class FileBrowserError(Exception):
    """FileBrowser 调用失败的对外摘要错误。"""


class FileBrowserConfigurationError(FileBrowserError):
    """FileBrowser 客户端配置不完整。"""


class FileBrowserNotFoundError(FileBrowserError):
    """FileBrowser 资源不存在。"""


class FileBrowserConflictError(FileBrowserError):
    """FileBrowser 资源冲突，创建方不得将其视为成功。"""


class FileBrowserAuthError(FileBrowserError):
    """FileBrowser 认证凭证无效或权限不足。"""


class FileBrowserServerError(FileBrowserError):
    """FileBrowser 返回服务端错误。"""


class FileBrowserTransportError(FileBrowserError):
    """FileBrowser 网络连接或请求超时。"""


class FileBrowserResponseError(FileBrowserError):
    """FileBrowser 返回未单独分类的 HTTP 或协议错误。"""


class FileBrowserClient:
    """FileBrowser Quantum 资源 API 客户端。"""

    SOURCE = FILEBROWSER_SOURCE

    def __init__(
        self,
        api_url: Optional[str] = None,
        api_key: Optional[str] = None,
        *,
        username: Optional[str] = None,
        password: Optional[str] = None,
        timeout: float = 30.0,
        max_retries: int = 2,
        retry_backoff: float = 0.2,
    ) -> None:
        if (
            api_url is None
            and api_key is None
            and username is None
            and password is None
        ):
            from config import settings

            if not settings.filebrowser_enabled:
                raise FileBrowserConfigurationError("FileBrowser 卷功能未启用")
            api_url = settings.filebrowser_api_url
            api_key = settings.filebrowser_api_key
            username = settings.filebrowser_username
            password = settings.filebrowser_password

        if not isinstance(api_url, str) or not api_url.strip():
            raise FileBrowserConfigurationError("FileBrowser API 地址未配置")
        if timeout <= 0:
            raise ValueError("FileBrowser 请求超时必须为正数")
        if max_retries < 0:
            raise ValueError("FileBrowser 重试次数不能为负数")
        if retry_backoff < 0:
            raise ValueError("FileBrowser 重试退避时间不能为负数")

        self._api_url = api_url.strip().rstrip("/")
        _validate_api_url(self._api_url)
        self._api_key = api_key if isinstance(api_key, str) and api_key.strip() else None
        self._username = username if isinstance(username, str) and username.strip() else None
        self._password = password if isinstance(password, str) and password.strip() else None
        if (self._username is None) != (self._password is None):
            raise FileBrowserConfigurationError("FileBrowser 用户名和密码必须同时提供")
        if self._username is not None and self._password is not None:
            self._auth_mode = "password"
        elif self._api_key is not None:
            self._auth_mode = "api_key"
        else:
            raise FileBrowserConfigurationError(
                "FileBrowser 必须配置 API Token 或完整的用户名密码认证"
            )
        self._timeout = timeout
        self._max_retries = max_retries
        self._retry_backoff = retry_backoff
        self._jwt: Optional[str] = None
        self._jwt_generation = 0
        self._auth_lock = threading.Lock()

    def get_resource(self, path: str) -> FileBrowserResource:
        """查询单个资源及其类型。"""
        response = self._request(
            "GET",
            _RESOURCES_ENDPOINT,
            path=path,
            retryable=True,
        )
        payload = self._json_object(response, "查询资源")
        return _parse_resource(payload, path)

    def resource_exists(self, path: str) -> bool:
        """检查资源是否存在；不存在时返回 False。"""
        try:
            self.get_resource(path)
        except FileBrowserNotFoundError:
            return False
        return True

    def list_directory(self, path: str) -> FileBrowserItems:
        """查询目录的直接子项。"""
        response = self._request(
            "GET",
            _ITEMS_ENDPOINT,
            path=path,
            retryable=True,
        )
        payload = self._json_object(response, "查询目录子项")
        return _parse_items(payload)

    def list_items(self, path: str) -> FileBrowserItems:
        """`list_directory` 的语义别名。"""
        return self.list_directory(path)

    def create_directory(self, path: str) -> None:
        """创建目录；冲突会抛出 `FileBrowserConflictError`。"""
        self._request(
            "POST",
            _RESOURCES_ENDPOINT,
            path=path,
            is_directory=True,
            data=b"",
            retryable=False,
        )

    def create_empty_file(self, path: str) -> None:
        """创建零字节文件；冲突会抛出 `FileBrowserConflictError`。"""
        self._request(
            "POST",
            _RESOURCES_ENDPOINT,
            path=path,
            is_directory=False,
            data=b"",
            retryable=False,
        )

    def delete_resource(self, path: str) -> bool:
        """递归删除资源；资源不存在时按幂等成功返回 True。"""
        response = self._request(
            "DELETE",
            _RESOURCES_ENDPOINT,
            path=path,
            retryable=True,
            allow_not_found=True,
        )
        return response is None or 200 <= response.status_code < 300

    def delete_directory(self, path: str) -> bool:
        """删除目录的语义别名。"""
        return self.delete_resource(path)

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        path: str,
        is_directory: Optional[bool] = None,
        data: Optional[bytes] = None,
        retryable: bool,
        allow_not_found: bool = False,
    ) -> Optional[requests.Response]:
        path = _validate_path(path)
        url = f"{self._api_url}/{endpoint.lstrip('/')}"
        params: dict[str, str] = {
            "source": self.SOURCE,
            "path": path,
        }
        if is_directory is not None:
            params["isDir"] = "true" if is_directory else "false"
        attempts = self._max_retries + 1 if retryable else 1
        transient_attempt = 0
        auth_refresh_used = False
        while True:
            request_headers, presented_token = self._authorization_headers()
            try:
                response = requests.request(
                    method,
                    url,
                    headers=request_headers,
                    params=params,
                    data=data,
                    timeout=self._timeout,
                )
            except requests.RequestException as exc:
                if transient_attempt + 1 < attempts:
                    self._log_retry(
                        method,
                        url,
                        transient_attempt + 1,
                        attempts,
                        str(exc),
                    )
                    self._sleep_before_retry(transient_attempt)
                    transient_attempt += 1
                    continue
                # 不使用 logger.exception：异常 traceback 可能原样包含第三方
                # 请求库附带的敏感信息，显式记录已脱敏的类型和摘要即可。
                logger.error(
                    "FileBrowser %s 请求失败: %s: %s",
                    method,
                    type(exc).__name__,
                    self._redact(str(exc)),
                )
                # 底层异常可能携带请求上下文，不把它作为异常链暴露给上层日志。
                raise FileBrowserTransportError("FileBrowser 不可达或请求失败") from None

            status = response.status_code
            if 200 <= status < 300:
                return response
            if status == 401:
                if self._auth_mode == "password" and not auth_refresh_used:
                    self._refresh_jwt(presented_token)
                    auth_refresh_used = True
                    continue
                logger.error("FileBrowser %s 鉴权失败: HTTP 401", method)
                raise FileBrowserAuthError("FileBrowser 鉴权失败或权限不足")
            if status == 404:
                if allow_not_found:
                    return None
                raise FileBrowserNotFoundError("FileBrowser 资源不存在")
            if status == 403:
                logger.error("FileBrowser %s 鉴权失败: HTTP %s", method, status)
                raise FileBrowserAuthError("FileBrowser 鉴权失败或权限不足")
            if status == 409:
                raise FileBrowserConflictError("FileBrowser 资源已存在或发生冲突")
            if 500 <= status < 600:
                if transient_attempt + 1 < attempts:
                    self._log_retry(
                        method,
                        url,
                        transient_attempt + 1,
                        attempts,
                        str(status),
                    )
                    self._sleep_before_retry(transient_attempt)
                    transient_attempt += 1
                    continue
                logger.error("FileBrowser %s 返回服务端错误: HTTP %s", method, status)
                raise FileBrowserServerError(f"FileBrowser 服务端错误: HTTP {status}")

            logger.error("FileBrowser %s 返回未预期状态: HTTP %s", method, status)
            raise FileBrowserResponseError(f"FileBrowser 返回未预期状态: HTTP {status}")

        # attempts 至少为一次；此处仅用于满足静态类型检查。
        raise FileBrowserTransportError("FileBrowser 请求失败")

    def _authorization_headers(
        self,
    ) -> tuple[dict[str, str], Optional[tuple[str, int]]]:
        """返回当前资源请求凭证及本次请求使用的 JWT 版本。"""
        if self._auth_mode == "api_key":
            if self._api_key is None:
                raise FileBrowserConfigurationError("FileBrowser API Token 未配置")
            headers: dict[str, str] = {"Authorization": f"Bearer {self._api_key}"}
            return headers, None

        with self._auth_lock:
            token = self._jwt
            if token is None:
                token = self._login_locked()
                self._jwt = token
            headers = {"Authorization": f"Bearer {token}"}
            presented: tuple[str, int] = (
                token,
                self._jwt_generation,
            )
            return headers, presented

    def _refresh_jwt(self, presented: Optional[tuple[str, int]]) -> None:
        """在资源 401 后使旧 JWT 失效并最多触发一次重新登录。"""
        with self._auth_lock:
            # Another request may already have refreshed the token while this
            # request was in flight. Reuse it instead of logging in again.
            if presented is not None and self._jwt_generation != presented[1]:
                return
            self._jwt = None
            self._jwt = self._login_locked()

    def _login_locked(self) -> str:
        """使用用户名密码登录；调用方必须持有 `_auth_lock`。"""
        if self._username is None or self._password is None:
            raise FileBrowserConfigurationError("FileBrowser 用户名密码未配置")
        url = f"{self._api_url}/auth/login"
        headers = {"X-Password": quote(self._password, safe="")}
        try:
            response = requests.request(
                "POST",
                url,
                headers=headers,
                params={"username": self._username},
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            logger.error(
                "FileBrowser 登录请求失败: %s: %s",
                type(exc).__name__,
                self._redact(str(exc)),
            )
            raise FileBrowserTransportError("FileBrowser 登录服务不可达或请求失败") from None

        status = response.status_code
        if status in (401, 403):
            logger.error("FileBrowser 登录鉴权失败: HTTP %s", status)
            raise FileBrowserAuthError("FileBrowser 登录失败")
        if 500 <= status < 600:
            logger.error("FileBrowser 登录服务端错误: HTTP %s", status)
            raise FileBrowserServerError("FileBrowser 登录服务端错误")
        if not 200 <= status < 300:
            logger.error("FileBrowser 登录返回未预期状态: HTTP %s", status)
            raise FileBrowserResponseError("FileBrowser 登录返回格式错误")

        token = _parse_login_token(response)
        self._jwt = token
        self._jwt_generation += 1
        return token

    @staticmethod
    def _json_object(response: Optional[requests.Response], operation: str) -> dict[str, Any]:
        if response is None:
            raise FileBrowserResponseError(f"FileBrowser {operation} 未返回响应")
        try:
            payload = response.json()
        except ValueError:
            logger.error("FileBrowser %s 返回了无效 JSON", operation)
            raise FileBrowserResponseError(f"FileBrowser {operation} 返回格式错误") from None
        if not isinstance(payload, dict):
            logger.error("FileBrowser %s 返回了非对象 JSON", operation)
            raise FileBrowserResponseError(f"FileBrowser {operation} 返回格式错误")
        return payload

    def _log_retry(
        self,
        method: str,
        url: str,
        attempt: int,
        attempts: int,
        detail: str,
    ) -> None:
        logger.warning(
            "FileBrowser %s 请求将重试 (%s/%s): %s: %s",
            method,
            attempt,
            attempts - 1,
            self._redact(url),
            self._redact(detail),
        )

    def _sleep_before_retry(self, attempt: int) -> None:
        delay = self._retry_backoff * (2**attempt)
        if delay > 0:
            time.sleep(delay)

    def _redact(self, value: str) -> str:
        """防止异常对象意外携带认证信息时进入日志。"""
        secrets = [self._api_key, self._username, self._password, self._jwt]
        if self._password:
            secrets.append(quote(self._password, safe=""))
        for secret in sorted(
            (item for item in secrets if item),
            key=len,
            reverse=True,
        ):
            value = value.replace(secret, "[REDACTED]")
        return value


def _parse_login_token(response: requests.Response) -> str:
    """解析 FileBrowser 登录接口返回的 JWT，不把响应内容写入错误。"""
    try:
        payload: Any = response.json()
    except ValueError:
        payload = response.text.strip()

    token: Any = payload
    if isinstance(payload, Mapping):
        for key in ("token", "jwt", "access_token"):
            candidate = payload.get(key)
            if isinstance(candidate, str):
                token = candidate
                break
        else:
            token = None
    if not isinstance(token, str) or not token.strip():
        logger.error("FileBrowser 登录返回了无效 JWT")
        raise FileBrowserResponseError("FileBrowser 登录返回格式错误")
    return token.strip()


def _validate_api_url(api_url: str) -> None:
    """拒绝把认证信息或查询参数放入客户端 API 地址。"""
    try:
        parsed = urlsplit(api_url)
        _ = parsed.port
    except ValueError:
        raise FileBrowserConfigurationError("FileBrowser API 地址格式错误") from None
    if (
        parsed.scheme.lower() not in ("http", "https")
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise FileBrowserConfigurationError("FileBrowser API 地址不得包含认证信息或查询参数")


def _validate_path(path: str) -> str:
    """校验客户端请求路径，保留 URL 编码交给 requests 参数编码器处理。"""
    if not isinstance(path, str) or not path or not path.startswith("/"):
        raise ValueError("FileBrowser 路径必须为以 `/` 开头的非空字符串")
    if "\\" in path or "\x00" in path:
        raise ValueError("FileBrowser 路径包含非法字符")
    if any(segment in (".", "..") for segment in path.split("/")):
        raise ValueError("FileBrowser 路径不得包含路径穿越段")
    return path


def _parse_resource(payload: Mapping[str, Any], requested_path: str) -> FileBrowserResource:
    resource_type = payload.get("type")
    if not isinstance(resource_type, str) or not resource_type:
        raise FileBrowserResponseError("FileBrowser 资源响应缺少类型")
    response_path = payload.get("path", requested_path)
    if not isinstance(response_path, str):
        response_path = requested_path
    name = payload.get("name")
    if not isinstance(name, str):
        name = None
    return FileBrowserResource(
        path=response_path,
        resource_type=resource_type,
        name=name,
        raw=payload,
    )


def _parse_items(payload: Mapping[str, Any]) -> FileBrowserItems:
    files = _parse_item_list(payload.get("files", []), "files")
    folders = _parse_item_list(payload.get("folders", []), "folders")
    return FileBrowserItems(files=tuple(files), folders=tuple(folders), raw=payload)


def _parse_item_list(value: Any, field_name: str) -> list[FileBrowserItem]:
    if not isinstance(value, list):
        raise FileBrowserResponseError(f"FileBrowser 目录响应字段 {field_name} 格式错误")
    result: list[FileBrowserItem] = []
    for raw_item in value:
        if isinstance(raw_item, str) and raw_item:
            result.append(
                FileBrowserItem(
                    name=raw_item,
                    resource_type="directory" if field_name == "folders" else "file",
                )
            )
            continue
        if not isinstance(raw_item, dict):
            raise FileBrowserResponseError(f"FileBrowser 目录响应字段 {field_name} 格式错误")
        name = raw_item.get("name")
        resource_type = raw_item.get(
            "type",
            "directory" if field_name == "folders" else "file",
        )
        if not isinstance(name, str) or not isinstance(resource_type, str):
            raise FileBrowserResponseError(f"FileBrowser 目录响应字段 {field_name} 格式错误")
        item_path = raw_item.get("path")
        if not isinstance(item_path, str):
            item_path = None
        result.append(
            FileBrowserItem(
                name=name,
                resource_type=resource_type,
                path=item_path,
                raw=raw_item,
            )
        )
    return result
