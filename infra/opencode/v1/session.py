"""OpenCode V1 会话列表与状态接口。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Optional

import httpx

from infra.opencode.client import (
    OpenCodeClient,
    OpenCodeInvalidResponseError,
    OpenCodeRouteNotFoundError,
)
from infra.opencode.v1.types import OpenCodeSessionSnapshot


__all__ = ["OpenCodeSessionClient"]

_SESSION_LIMIT = 10000
_VERSION_PATTERN = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-+][0-9A-Za-z.-]+)?$")


class OpenCodeSessionClient:
    """只读访问受支持的 OpenCode V1 会话和状态接口。"""

    def __init__(
            self,
            api_url: str,
            api_token: str,
            *,
            transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self._api_url = api_url
        self._api_token = api_token
        self._transport = transport

    async def get_snapshot(
            self,
            *,
            directory: Optional[str] = None,
            workspace: Optional[str] = None,
            scope: Optional[str] = None,
            path: Optional[str] = None,
            roots: Optional[bool] = None,
            start: Optional[str] = None,
            search: Optional[str] = None,
            limit: Optional[int] = None,
    ) -> OpenCodeSessionSnapshot:
        filters: dict[str, str | int | bool] = {}
        for key, value in {
            "directory": directory,
            "workspace": workspace,
            "scope": scope,
            "path": path,
            "roots": roots,
            "start": start,
            "search": search,
            "limit": limit,
        }.items():
            if value is not None:
                filters[key] = value
        async with OpenCodeClient(
                self._api_url,
                self._api_token,
                transport=self._transport,
        ) as client:
            version = await self._get_version(client)
            try:
                session_ids = await self._list_sessions(client, filters)
                statuses = await self._get_statuses(client, filters)
            except OpenCodeRouteNotFoundError as exc:
                raise OpenCodeInvalidResponseError(
                    "OpenCode V1 session API route is unavailable"
                ) from exc
        return OpenCodeSessionSnapshot(
            version=version,
            session_ids=tuple(session_ids),
            statuses=statuses,
        )

    @staticmethod
    async def _get_version(client: OpenCodeClient) -> str:
        try:
            health = await client.get_json("/global/health")
        except OpenCodeRouteNotFoundError as exc:
            raise OpenCodeInvalidResponseError(
                "OpenCode V1 health route is unavailable"
            ) from exc
        if not isinstance(health, Mapping):
            raise OpenCodeInvalidResponseError("OpenCode V1 health 响应格式非法")
        if not isinstance(health.get("healthy"), bool):
            raise OpenCodeInvalidResponseError("OpenCode V1 health 状态缺失")
        if health["healthy"] is not True:
            raise OpenCodeInvalidResponseError("OpenCode V1 上游尚未就绪")
        version = _version_text(health.get("version"))
        if _version_major(version) != 1:
            raise OpenCodeInvalidResponseError("OpenCode V1 server version 不受支持")
        return version

    @staticmethod
    async def _list_sessions(
            client: OpenCodeClient,
            filters: dict[str, str | int | bool],
    ) -> list[str]:
        # V1 的 `start` 是 time_updated 的下界，不是偏移量或分页游标。
        params = {**filters}
        params.setdefault("limit", _SESSION_LIMIT)
        limit = params["limit"]
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise OpenCodeInvalidResponseError("OpenCode V1 session limit 非法")
        ids = _session_ids(await client.get_json("/session", params=params))
        if len(ids) >= limit:
            raise OpenCodeInvalidResponseError(
                "OpenCode V1 会话数达到安全上限，无法证明清单完整"
            )
        return list(dict.fromkeys(ids))

    @staticmethod
    async def _get_statuses(
            client: OpenCodeClient,
            filters: dict[str, str | int | bool],
    ) -> dict[str, str]:
        params = {
            key: filters[key]
            for key in ("directory", "workspace")
            if key in filters
        }
        return _status_map(
            await client.get_json("/session/status", params=params or None)
        )


def _version_text(value: object) -> str:
    if not isinstance(value, str) or _VERSION_PATTERN.fullmatch(value) is None:
        raise OpenCodeInvalidResponseError("OpenCode V1 server version 缺失或格式非法")
    return value


def _version_major(version: str) -> int:
    match = _VERSION_PATTERN.fullmatch(version)
    if match is None:
        raise OpenCodeInvalidResponseError("OpenCode V1 server version 格式非法")
    return int(match.group(1))


def _session_ids(payload: object) -> list[str]:
    if not isinstance(payload, list):
        raise OpenCodeInvalidResponseError("OpenCode V1 会话清单必须为数组")
    ids: list[str] = []
    for session in payload:
        if not isinstance(session, Mapping):
            raise OpenCodeInvalidResponseError("OpenCode V1 会话条目格式非法")
        session_id = session.get("id")
        if not isinstance(session_id, str) or not session_id:
            raise OpenCodeInvalidResponseError("OpenCode V1 会话 ID 缺失")
        ids.append(session_id)
    return ids


def _status_map(payload: object) -> dict[str, str]:
    if not isinstance(payload, Mapping):
        raise OpenCodeInvalidResponseError("OpenCode V1 会话状态必须为对象")
    result: dict[str, str] = {}
    for session_id, status in payload.items():
        if not isinstance(session_id, str) or not session_id or not isinstance(status, Mapping):
            raise OpenCodeInvalidResponseError("OpenCode V1 会话状态条目格式非法")
        status_type: Any = status.get("type")
        if not isinstance(status_type, str) or not status_type:
            raise OpenCodeInvalidResponseError("OpenCode V1 会话状态类型缺失")
        result[session_id] = status_type
    return result
