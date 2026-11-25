"""通过容器的 OpenCode API 查询 TestAgent 会话计数。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from application import container as container_service
from application.blacklist import ensure_user_not_blacklisted
from config import Constants
from domain.errors import (
    ContainerNotFoundError,
    TestAgentAuthFailedError,
    TestAgentInvalidResponseError,
    TestAgentUnavailableError,
)
from infra.db import session_scope
from infra.opensandbox import OpenSandboxError, rewrite_loopback_endpoint
from infra.opencode import (
    OpenCodeAuthError,
    OpenCodeInvalidResponseError,
    OpenCodeUnavailableError,
)
from infra.opencode.v1 import OpenCodeSessionClient, OpenCodeSessionSnapshot
from infra.repositories import ContainerRepository

logger = logging.getLogger(__name__)

__all__ = ["SessionCounts", "get_session_counts"]


@dataclass(frozen=True)
class SessionCounts:
    total: int
    idle: int
    busy: int
    retry: int
    unknown: int


async def get_session_counts(
    service_id: str,
    *,
    expected_container_id: Optional[str] = None,
) -> SessionCounts:
    """从服务记录解析可信端点和凭据，并统计 OpenCode 会话。"""
    if not service_id:
        raise ContainerNotFoundError("云端沙箱不存在")
    async with session_scope() as session:
        row = await ContainerRepository(session).get_by_service_id(service_id)
        if row is None or row.deleted_at is not None:
            raise ContainerNotFoundError("云端沙箱不存在")
        if expected_container_id is not None and row.container_id != expected_container_id:
            raise ContainerNotFoundError("云端沙箱后端已更换")
        ensure_user_not_blacklisted(row.user_id)
        container_id = row.container_id
        api_token = row.api_token

    if not api_token:
        raise TestAgentAuthFailedError("OpenCode 上游鉴权失败")

    try:
        endpoint = await container_service.get_opensandbox_client().get_endpoint(
            container_id,
            Constants.CONTAINER_TSCODE_API_PORT.value,
        )
    except OpenSandboxError as exc:
        logger.warning("解析 OpenCode API 端点失败: %s (%s)", service_id, type(exc).__name__)
        raise TestAgentUnavailableError("OpenCode 上游不可用") from exc
    except Exception as exc:  # noqa: BLE001
        logger.warning("解析 OpenCode API 端点失败: %s (%s)", service_id, type(exc).__name__)
        raise TestAgentUnavailableError("OpenCode 上游不可用") from exc

    api_url = getattr(endpoint, "endpoint", None)
    if not isinstance(api_url, str) or not api_url.strip():
        raise TestAgentUnavailableError("OpenCode API 端点不可用")
    api_url = rewrite_loopback_endpoint(api_url)

    try:
        snapshot = await OpenCodeSessionClient(api_url, api_token).get_snapshot()
    except OpenCodeAuthError as exc:
        raise TestAgentAuthFailedError("OpenCode 上游鉴权失败") from exc
    except OpenCodeUnavailableError as exc:
        raise TestAgentUnavailableError("OpenCode 上游不可用") from exc
    except OpenCodeInvalidResponseError as exc:
        raise TestAgentInvalidResponseError("OpenCode 上游响应非法或版本不受支持") from exc
    return _count_snapshot(snapshot)


def _count_snapshot(snapshot: OpenCodeSessionSnapshot) -> SessionCounts:
    counts = {"idle": 0, "busy": 0, "retry": 0, "unknown": 0}
    for session_id in snapshot.session_ids:
        status = snapshot.statuses.get(session_id)
        if status is None:
            # OpenCode V1 的状态映射会省略空闲会话。
            counts["idle"] += 1
        elif status in {"idle", "busy", "retry"}:
            counts[status] += 1
        else:
            counts["unknown"] += 1

    return SessionCounts(
        total=len(snapshot.session_ids),
        idle=counts["idle"],
        busy=counts["busy"],
        retry=counts["retry"],
        unknown=counts["unknown"],
    )
