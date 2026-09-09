"""
领域层业务模型（v4 §8.3、§11.1）。

- `ContainerStatus`：业务状态枚举 `pending` / `running` / `stopped` / `failed` /
  `business_deleted` / `unknown`。
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
    "Container",
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
