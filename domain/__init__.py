"""
领域层（v4 §4.3）：业务模型与业务异常。

公共模块为 `models`（业务模型与状态）与 `errors`（业务异常）。
"""

from __future__ import annotations

from domain.errors import (
    AppError,
    BusinessConflictError,
    VolumePathConflictError,
    ContainerNotFoundError,
    DefaultImageNotConfiguredError,
    ExternalDependencyError,
    GitCredentialAlreadyClaimedError,
    GitCredentialConflictError,
    GitCredentialUnavailableError,
    GitResourceNotFoundError,
    GitResourceUserMismatchError,
    GitSessionEndedError,
    InvalidArgumentError,
    LimitReachedError,
    UnauthorizedError,
    UserBlacklistedError,
    UserNotFoundError,
)
from domain.models import (
    GIT_FAILURE_STATUSES,
    GIT_FINAL_STATUSES,
    GIT_INTERMEDIATE_STATUSES,
    Container,
    ContainerStatus,
    GitFinalStatus,
    GitStatus,
    coerce_git_final_status,
    coerce_git_status,
    get_public_git_fin_status,
    is_git_final_status,
    map_runtime_state,
    resolve_container_status,
)

__all__ = [
    "AppError",
    "InvalidArgumentError",
    "UnauthorizedError",
    "UserBlacklistedError",
    "DefaultImageNotConfiguredError",
    "ContainerNotFoundError",
    "GitResourceNotFoundError",
    "GitResourceUserMismatchError",
    "UserNotFoundError",
    "BusinessConflictError",
    "VolumePathConflictError",
    "GitSessionEndedError",
    "GitCredentialUnavailableError",
    "GitCredentialAlreadyClaimedError",
    "GitCredentialConflictError",
    "LimitReachedError",
    "ExternalDependencyError",
    "Container",
    "ContainerStatus",
    "GitStatus",
    "GitFinalStatus",
    "GIT_INTERMEDIATE_STATUSES",
    "GIT_FAILURE_STATUSES",
    "GIT_FINAL_STATUSES",
    "coerce_git_status",
    "coerce_git_final_status",
    "is_git_final_status",
    "get_public_git_fin_status",
    "resolve_container_status",
    "map_runtime_state",
]
