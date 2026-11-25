"""访问可信 OpenCode API 端点的轻量异步 HTTP 客户端。"""

from __future__ import annotations

import base64
import binascii
import logging
from collections.abc import Mapping
from typing import Any, Optional
from urllib.parse import parse_qsl, urlsplit, urlunsplit

import httpx

logger = logging.getLogger(__name__)

__all__ = [
    "OpenCodeAuthError",
    "OpenCodeClient",
    "OpenCodeInvalidResponseError",
    "OpenCodeRouteNotFoundError",
    "OpenCodeServerAuthClient",
    "OpenCodeUnavailableError",
]

_REQUEST_TIMEOUT_SECONDS = 5.0


class OpenCodeError(Exception):
    """OpenCode 错误基类，不包含凭据或上游响应正文。"""


class OpenCodeAuthError(OpenCodeError):
    """OpenCode 拒绝了服务凭据。"""


class OpenCodeUnavailableError(OpenCodeError):
    """无法连接 OpenCode，或上游返回了服务器错误。"""


class OpenCodeInvalidResponseError(OpenCodeError):
    """OpenCode 返回的数据不符合受支持的接口约定。"""


class OpenCodeRouteNotFoundError(OpenCodeError):
    """OpenCode 服务端不存在请求的 API 路径。"""


class OpenCodeClient:
    """使用 OpenCode Basic Auth 凭据的只读 HTTP 客户端。"""

    def __init__(
        self,
        api_url: str,
        api_token: Optional[str] = None,
        *,
        timeout: float = _REQUEST_TIMEOUT_SECONDS,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        normalized_url = api_url.strip()
        if "://" not in normalized_url:
            # noinspection HttpUrlsUsage
            normalized_url = f"http://{normalized_url}"
        try:
            parsed = urlsplit(normalized_url)
            port = parsed.port
        except ValueError as exc:
            raise OpenCodeInvalidResponseError("OpenCode API 地址非法") from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.hostname is None
            or (port is not None and not 1 <= port <= 65535)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise OpenCodeInvalidResponseError("OpenCode API 地址非法")
        self._base_url = urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "")
        )
        self._base_query = parse_qsl(parsed.query, keep_blank_values=True)
        headers = (
            {"Authorization": f"Basic {api_token}"}
            if api_token
            else None
        )
        self._client = httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(timeout),
            transport=transport,
            follow_redirects=False,
        )

    async def __aenter__(self) -> OpenCodeClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self._client.aclose()

    async def get_json(
        self,
        path: str,
        *,
        params: Optional[Mapping[Any, Any]] = None,
    ) -> Any:
        """请求 JSON 响应并分类错误；日志中不记录凭据或敏感内容。"""
        url = f"{self._base_url}/{path.lstrip('/')}"
        query_params: list[tuple[str, str | int | float | bool | None]] = []
        query_params.extend(self._base_query)
        if params is not None:
            query_params.extend(
                (
                    key,
                    str(value).lower() if isinstance(value, bool) else str(value),
                )
                for key, value in params.items()
            )
        try:
            response = await self._client.get(url, params=query_params or None)
        except httpx.InvalidURL as exc:
            raise OpenCodeInvalidResponseError("OpenCode API 请求地址非法") from exc
        except httpx.TimeoutException as exc:
            logger.warning("OpenCode 请求超时: %s", path)
            raise OpenCodeUnavailableError("OpenCode 上游请求超时") from exc
        except httpx.RequestError as exc:
            logger.warning("OpenCode 请求失败: %s (%s)", path, type(exc).__name__)
            raise OpenCodeUnavailableError("OpenCode 上游不可用") from exc

        if response.status_code in {401, 403}:
            raise OpenCodeAuthError("OpenCode 上游鉴权失败")
        if response.status_code == 404:
            raise OpenCodeRouteNotFoundError("OpenCode API 路径不存在")
        if response.status_code >= 500:
            logger.warning("OpenCode 上游返回 HTTP %d: %s", response.status_code, path)
            raise OpenCodeUnavailableError("OpenCode 上游不可用")
        if not response.is_success:
            logger.warning("OpenCode 上游返回 HTTP %d: %s", response.status_code, path)
            raise OpenCodeInvalidResponseError("OpenCode 上游请求响应非法")
        try:
            return response.json()
        except (ValueError, UnicodeDecodeError) as exc:
            raise OpenCodeInvalidResponseError("OpenCode 上游返回了非法 JSON") from exc


class OpenCodeServerAuthClient:
    """Fetch and validate the server-issued Base64 Basic credential."""

    def __init__(
        self,
        api_url: str,
        *,
        timeout: float = _REQUEST_TIMEOUT_SECONDS,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self._client = OpenCodeClient(
            api_url,
            api_token=None,
            timeout=timeout,
            transport=transport,
        )

    async def get_token(self) -> str:
        """Request Auth without credentials and return its original token string."""
        async with self._client as client:
            payload = await client.get_json("/testagent/server/auth")
        if not isinstance(payload, Mapping):
            raise OpenCodeInvalidResponseError("OpenCode Auth 响应格式非法")
        username = payload.get("username")
        token = payload.get("token")
        if username != "opencode" or not isinstance(token, str) or not token:
            raise OpenCodeInvalidResponseError("OpenCode Auth 凭据格式非法")
        try:
            encoded = token.encode("ascii")
            decoded = base64.b64decode(encoded, validate=True)
            credential = decoded.decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError, binascii.Error, ValueError) as exc:
            raise OpenCodeInvalidResponseError(
                "OpenCode Auth Token 不是有效的 Base64 凭据"
            ) from exc
        if base64.b64encode(decoded) != encoded:
            raise OpenCodeInvalidResponseError("OpenCode Auth Token 不是规范的 Base64 凭据")
        token_username, separator, _ = credential.partition(":")
        if not separator or token_username != username:
            raise OpenCodeInvalidResponseError("OpenCode Auth Token 用户名不匹配")
        return token
