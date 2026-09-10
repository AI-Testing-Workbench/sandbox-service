"""
领域层业务模型（v4 §8.3、§11.1）。

- `ContainerStatus`：业务状态枚举 `pending` / `running` / `stopped` / `failed` /
  `business_deleted` / `unknown`。
- `GitStatus`：Git 初始化详细状态，仅供 Git API 的领域逻辑使用。
- `GitFinalStatus`：数据库允许保存的 Git 初始化最终状态。
- `Container`：容器业务模型（含 `authorize_general_account`，变更 #2）。
- 时间均为带时区偏移的 ISO 8601 字符串（v4 §5.3，UTC+8）。
- OpenSandbox 原始运行状态到业务状态的映射规则见 `map_runtime_state`。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional

__all__ = [
    "ContainerStatus",
    "ContainerType",
    "GitStatus",
    "GitFinalStatus",
    "GIT_INTERMEDIATE_STATUSES",
    "GIT_FAILURE_STATUSES",
    "GIT_FINAL_STATUSES",
    "Container",
    "coerce_git_status",
    "coerce_git_final_status",
    "is_git_final_status",
    "get_public_git_fin_status",
    "resolve_container_status",
    "map_runtime_state",
    "add_hours_to_iso"
]


class ContainerStatus(str, Enum):
    """容器业务状态（v4 §8.3、§11.1）。"""

    PENDING = "pending"
    RUNNING = "running"
    STOPPED = "stopped"
    FAILED = "failed"
    BUSINESS_DELETED = "business_deleted"
    UNKNOWN = "unknown"


class ContainerType(str, Enum):
    """容器类型：同一底层镜像的不同启动方式（v4 §11.1）。

    - `testagent_cloud`：开发/联调容器（仅 SSH）。
    - `autotest_cloud`：自动化跑批容器（额外启用 Chrome/VNC，仍可通过 SSH 连入）。
    """

    TESTAGENT_CLOUD = "testagent_cloud"
    AUTOTEST_CLOUD = "autotest_cloud"


class GitStatus(str, Enum):
    """Git 初始化详细状态（Git 凭证设计报告 §2.2）。"""

    STARTING = "starting"
    CREDENTIAL_REQUIRED = "credential_required"
    CREDENTIAL_REJECTED = "credential_rejected"
    PROCESSING = "processing"
    INITIALIZED = "initialized"
    FAILED_TIMEOUT = "failed_timeout"
    FAILED_MAX_ATTEMPTS = "failed_max_attempts"
    FAILED_UNEXPECTED_STATE = "failed_unexpected_state"
    FAILED_GIT = "failed_git"
    FAILED_SERVICE = "failed_service"
    FAILED_CONTAINER = "failed_container"
    FAILED_INITIALIZE = "failed_initialize"
    FAILED_USER_CANCELLED = "failed_user_cancelled"


class GitFinalStatus(str, Enum):
    """数据库允许保存的 Git 初始化最终状态。"""

    INITIALIZED = "initialized"
    FAILED_TIMEOUT = "failed_timeout"
    FAILED_MAX_ATTEMPTS = "failed_max_attempts"
    FAILED_UNEXPECTED_STATE = "failed_unexpected_state"
    FAILED_GIT = "failed_git"
    FAILED_SERVICE = "failed_service"
    FAILED_CONTAINER = "failed_container"
    FAILED_INITIALIZE = "failed_initialize"
    FAILED_USER_CANCELLED = "failed_user_cancelled"


# 中间状态只保存在服务进程内存，不得写入数据库或现有容器状态 API。
GIT_INTERMEDIATE_STATUSES: tuple[GitStatus, ...] = (
    GitStatus.STARTING,
    GitStatus.CREDENTIAL_REQUIRED,
    GitStatus.CREDENTIAL_REJECTED,
    GitStatus.PROCESSING,
)

# 失败状态使用固定枚举白名单，禁止上报任意 `failed_...` 文本。
GIT_FAILURE_STATUSES: tuple[GitFinalStatus, ...] = (
    GitFinalStatus.FAILED_TIMEOUT,
    GitFinalStatus.FAILED_MAX_ATTEMPTS,
    GitFinalStatus.FAILED_UNEXPECTED_STATE,
    GitFinalStatus.FAILED_GIT,
    GitFinalStatus.FAILED_SERVICE,
    GitFinalStatus.FAILED_CONTAINER,
    GitFinalStatus.FAILED_INITIALIZE,
    GitFinalStatus.FAILED_USER_CANCELLED,
)

GIT_FINAL_STATUSES: tuple[GitFinalStatus, ...] = (
    GitFinalStatus.INITIALIZED,
    *GIT_FAILURE_STATUSES,
)

_GitStatusInput = str | GitStatus | GitFinalStatus


def coerce_git_status(value: str | GitStatus) -> GitStatus:
    """将 Git 详细状态转换为枚举，非法值立即拒绝。"""
    if isinstance(value, GitStatus):
        return value
    try:
        return GitStatus(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"无效 Git 状态: {value!r}") from exc


def coerce_git_final_status(value: _GitStatusInput) -> GitFinalStatus:
    """将数据库 Git 最终状态转换为枚举，只接受最终状态白名单。"""
    if isinstance(value, GitFinalStatus):
        return value
    try:
        return GitFinalStatus(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"无效 Git 最终状态: {value!r}") from exc


def is_git_final_status(value: object) -> bool:
    """判断值是否为允许持久化的 Git 最终状态。"""
    if not isinstance(value, (str, GitStatus, GitFinalStatus)):
        return False
    try:
        coerce_git_final_status(value)
    except (TypeError, ValueError):
        return False
    return True


def get_public_git_fin_status(value: Optional[_GitStatusInput]) -> str:
    """将内部 Git 状态转换为现有容器 API 的粗粒度 `git_fin_status`。

    初始化未完成（无状态或任意中间状态）统一返回 `pending`；最终状态原样返回。
    `pending` 是现有 API 的输出值，不属于详细 `GitStatus`，但作为输入接受以便处理
    已经被粗粒度化的状态。
    """
    if value is None or value == "pending":
        return "pending"

    status = coerce_git_status(value)
    if status in GIT_INTERMEDIATE_STATUSES:
        return "pending"
    return status.value


def resolve_container_status(
    git_fin_status: Optional[_GitStatusInput],
    runtime_status: Optional[str | ContainerStatus],
) -> ContainerStatus:
    """根据 Git 初始化结果和 OpenSandbox 状态计算总体容器状态。

    Git 初始化完成前无论沙盒原始状态如何都返回 `pending`；Git 初始化最终失败
    返回 `failed`；只有初始化成功后才封装 OpenSandbox 运行状态。
    """
    public_git_status = get_public_git_fin_status(git_fin_status)
    if public_git_status == "pending":
        return ContainerStatus.PENDING
    if public_git_status.startswith("failed_"):
        return ContainerStatus.FAILED
    if public_git_status == GitFinalStatus.INITIALIZED.value:
        if isinstance(runtime_status, ContainerStatus):
            return runtime_status
        return map_runtime_state(runtime_status)
    raise ValueError(f"无效 Git 最终状态: {public_git_status!r}")


@dataclass(frozen=True)
class Container:
    """容器业务模型（v4 §11.1；变更 #2 增加 `authorize_general_account`）。"""

    container_id: str
    image: str

    status: ContainerStatus

    user_id: str
    gitee_user: str
    gitee_repository: str
    gitee_branch: Optional[str]
    gitee_url: str
    authorize_general_account: bool

    created_at: str
    expiration_hours: int
    deleted_at: Optional[str] = None

    @property
    def business_deleted(self) -> bool:
        """是否已业务删除。"""
        return self.deleted_at is not None

    @property
    def expires_at(self) -> Optional[str]:
        """业务删除时刻（`created_at + expiration_hours`，带时区偏移 ISO 8601）；
        `expiration_hours <= 0`（永不过期）返回 None。"""
        return add_hours_to_iso(self.created_at, self.expiration_hours)


def map_runtime_state(raw: Optional[str]) -> ContainerStatus:
    """将 OpenSandbox 原始运行状态映射为业务状态（v4 §8.3）：

    - 运行中 → `running`
    - 已停止（Paused / Terminated，以及旧后端的退出态）→ `stopped`
    - 运行失败（Failed）→ `failed`
    - 创建 / 暂停中 / 恢复中 / 终止中等过渡状态 → `pending`
    - 获取失败或不可达时应由调用方另行给出 `unknown`（不通过本函数）。
    """
    state = (raw or "").strip().upper()
    if state == "RUNNING":
        return ContainerStatus.RUNNING
    if state == "FAILED":
        return ContainerStatus.FAILED
    if state in ("PAUSED", "EXITED", "STOPPED", "TERMINATED", "DEAD"):
        return ContainerStatus.STOPPED
    return ContainerStatus.PENDING


def add_hours_to_iso(value: str, hours: int) -> Optional[str]:
    """在带时区偏移的 ISO 时间字符串上累加小时；`hours <= 0` 返回 None（永不过期）。"""
    if hours <= 0:
        return None
    dt = datetime.fromisoformat(value)
    return (dt + timedelta(hours=hours)).isoformat()
