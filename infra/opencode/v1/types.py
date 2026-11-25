"""OpenCode V1 会话 API 适配器返回的数据类型。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

__all__ = ["OpenCodeSessionSnapshot"]


@dataclass(frozen=True)
class OpenCodeSessionSnapshot:
    """上游 API 返回的会话 ID 与 V1 状态值。"""

    version: str
    session_ids: tuple[str, ...]
    statuses: Mapping[str, str]
