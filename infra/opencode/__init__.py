"""OpenCode HTTP 通用客户端与错误类型。"""

from infra.opencode.client import (
    OpenCodeAuthError,
    OpenCodeInvalidResponseError,
    OpenCodeUnavailableError,
)
__all__ = [
    "OpenCodeAuthError",
    "OpenCodeInvalidResponseError",
    "OpenCodeUnavailableError",
]
