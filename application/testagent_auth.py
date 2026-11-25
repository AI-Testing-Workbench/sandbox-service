"""从 OpenCode Auth API 补齐当前后端缺失的 TestAgent API Token。"""

from __future__ import annotations

import logging

from config import Constants
from domain.models import ContainerStatus, map_runtime_state
from infra.db import session_scope
from infra.opensandbox import rewrite_loopback_endpoint
from infra.opencode import OpenCodeServerAuthClient
from infra.repositories import ContainerRepository

logger = logging.getLogger(__name__)

__all__ = ["fill_missing_api_tokens"]


async def fill_missing_api_tokens() -> None:
    """Fetch tokens only for active, unstopped services with a RUNNING backend."""
    from application.container import get_opensandbox_client

    async with session_scope() as session:
        candidates = [
            (row.service_id, row.container_id)
            for row in await ContainerRepository(session).list_active()
            if row.stop_at is None and not row.api_token
        ]

    updated = 0
    for service_id, container_id in candidates:
        stage = "status"
        try:
            client = get_opensandbox_client()
            status = await client.get_status(container_id)
            if map_runtime_state(getattr(status, "state", None)) is not ContainerStatus.RUNNING:
                continue

            stage = "endpoint"
            endpoint = await client.get_endpoint(
                container_id,
                Constants.CONTAINER_TSCODE_API_PORT.value,
            )
            api_url = getattr(endpoint, "endpoint", None)
            if not isinstance(api_url, str) or not api_url.strip():
                raise ValueError("OpenCode API endpoint is unavailable")
            api_url = rewrite_loopback_endpoint(api_url)

            stage = "auth"
            token = await OpenCodeServerAuthClient(api_url).get_token()

            stage = "persistence"
            async with session_scope() as session:
                stored = await ContainerRepository(session).set_api_token_if_missing(
                    service_id=service_id,
                    expected_container_id=container_id,
                    api_token=token,
                )
            updated += int(stored)
        except Exception as exc:  # noqa: BLE001 one container must not stop the scan
            logger.warning(
                "自动获取 API Token 失败: service_id=%s container_id=%s stage=%s error=%s",
                service_id,
                container_id,
                stage,
                type(exc).__name__,
            )

    if updated:
        logger.info("自动补齐 OpenCode API Token: 更新 %d 个容器", updated)
