"""Robot-facing API operations."""

from __future__ import annotations

from dataclasses import dataclass

from domain.errors import AppError, ContainerNotFoundError, InvalidArgumentError
from infra.db import session_scope
from infra.repositories import ContainerRepository, GiteeUrlHistoryRepository

__all__ = [
    "GiteeUrlHistoryView",
    "set_api_token",
    "list_gitee_urls",
]


@dataclass(frozen=True)
class GiteeUrlHistoryView:
    id: int
    gitee_url: str
    gitee_branch: str


async def set_api_token(service_id: str, api_token: str) -> None:
    """Update a container token without including the token in errors or logs."""
    if not api_token.strip():
        raise InvalidArgumentError("api_token 不能为空")
    try:
        async with session_scope() as session:
            updated = await ContainerRepository(session).update_api_token(
                service_id,
                api_token,
            )
            if not updated:
                raise ContainerNotFoundError("云端沙箱不存在")
    except (ContainerNotFoundError, InvalidArgumentError):
        raise
    except Exception:
        # Database errors can include bound parameters, so suppress their details.
        raise AppError("设置 API Token 失败") from None


async def list_gitee_urls(user_id: str) -> list[GiteeUrlHistoryView]:
    """Return the user's URL history in insertion order, newest first."""
    if not user_id or not user_id.strip():
        raise InvalidArgumentError("user_id 不能为空")
    async with session_scope() as session:
        rows = await GiteeUrlHistoryRepository(session).list_for_user(user_id)
        return [
            GiteeUrlHistoryView(
                id=row.id,
                gitee_url=row.gitee_url,
                gitee_branch=row.gitee_branch,
            )
            for row in rows
        ]
