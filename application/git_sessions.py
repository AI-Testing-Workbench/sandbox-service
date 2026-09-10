"""
Git 初始化内存会话（Git 凭证设计报告 §1.2、§2.3、§2.5）。

- 创建阶段使用预生成的 `service_id`，OpenSandbox 成功后绑定真实 `container_id`。
- 中间 Git 状态和非持久化凭证只保存在当前进程内存。
- 资源解析先查活跃会话，再按 `container_id` 查询数据库；所有路径都校验用户绑定。
- 结束会话时清除明文凭证并移除活跃映射；进程重启不会恢复内存会话。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from threading import RLock
from typing import NoReturn, Optional
from uuid import uuid4

from domain.errors import (
    BusinessConflictError,
    GitCredentialAlreadyClaimedError,
    GitCredentialUnavailableError,
    GitResourceNotFoundError,
    GitResourceUserMismatchError,
    GitSessionEndedError,
    InvalidArgumentError,
    UnauthorizedError,
)
from domain.models import (
    GitFinalStatus,
    GitStatus,
    coerce_git_final_status,
    coerce_git_status,
)
from infra.db import session_scope
from infra.repositories import ContainerRepository

from application.git_credentials import GitCredential, validate_credential

__all__ = [
    "GitInitializationSession",
    "GitResource",
    "GitSessionStore",
    "get_git_session_store",
]

_ENDED_SERVICE_ID_LIMIT = 4096


@dataclass
class GitInitializationSession:
    """当前进程内的 Git 初始化或运行期临时凭证会话。"""

    service_id: Optional[str]
    service_user: str
    container_id: Optional[str] = None
    git_status: GitStatus = GitStatus.STARTING
    credential_available: bool = False
    temporary_git_username: Optional[str] = None
    temporary_git_email: Optional[str] = None
    temporary_git_password: Optional[str] = field(default=None, repr=False)
    temporary_type: Optional[str] = None
    credential_claimed: bool = False
    ended: bool = False

    def clear_temporary_credential(self, *, claimed: bool = False) -> None:
        """清除会话中的全部临时凭证字段。"""
        self.temporary_git_username = None
        self.temporary_git_email = None
        self.temporary_git_password = None
        self.temporary_type = None
        self.credential_available = False
        self.credential_claimed = claimed


@dataclass(frozen=True)
class GitResource:
    """资源解析结果；`session` 仅供服务内部使用，不参与响应。"""

    user_id: str
    service_id: Optional[str]
    container_id: Optional[str]
    git_fin_status: Optional[str] = None
    session: Optional[GitInitializationSession] = field(default=None, repr=False)


class GitSessionStore:
    """线程安全的 Git 内存会话存储。"""

    def __init__(self) -> None:
        self._lock = RLock()
        self._by_service_id: dict[str, GitInitializationSession] = {}
        self._by_container_id: dict[str, GitInitializationSession] = {}
        self._ended_service_ids: set[str] = set()
        self._ended_service_order: deque[str] = deque()

    def create_session(
        self,
        service_user: str,
        *,
        service_id: Optional[str] = None,
    ) -> GitInitializationSession:
        """创建一个使用 UUID `service_id` 的初始化会话。"""
        service_user = _require_id(service_user, "service_user")
        resolved_service_id = _require_id(service_id, "service_id") if service_id else str(uuid4())
        with self._lock:
            if (
                resolved_service_id in self._by_service_id
                or resolved_service_id in self._by_container_id
                or resolved_service_id in self._ended_service_ids
            ):
                raise BusinessConflictError("Git service_id 已被占用")
            session = GitInitializationSession(
                service_id=resolved_service_id,
                service_user=service_user,
            )
            self._by_service_id[resolved_service_id] = session
            return session

    def bind_container_id(
        self,
        service_id: str,
        container_id: str,
    ) -> GitInitializationSession:
        """将 OpenSandbox 真实容器 ID 绑定到初始化会话。"""
        service_id = _require_id(service_id, "service_id")
        container_id = _require_id(container_id, "container_id")
        with self._lock:
            session = self._by_service_id.get(service_id)
            if session is None:
                self._raise_missing_active_session(service_id)
            existing = self._by_container_id.get(container_id)
            if existing is not None and existing is not session:
                raise BusinessConflictError("Git container_id 已被其他会话占用")
            if session.container_id is not None and session.container_id != container_id:
                raise BusinessConflictError("Git 会话已绑定其他 container_id")
            session.container_id = container_id
            self._by_container_id[container_id] = session
            return session

    def resolve_resource(self, resource_id: str, operator_user_id: str) -> GitResource:
        """解析 `service_id` 或 `container_id` 并校验请求用户绑定。"""
        resource_id = _require_id(resource_id, "resource_id")
        operator_user_id = _require_operator_user_id(operator_user_id)

        with self._lock:
            session = self._by_service_id.get(resource_id)
            if session is None:
                session = self._by_container_id.get(resource_id)
            if session is not None:
                _ensure_user_match(session.service_user, operator_user_id)
                return GitResource(
                    user_id=session.service_user,
                    service_id=session.service_id,
                    container_id=session.container_id,
                    git_fin_status=(
                        GitFinalStatus.INITIALIZED.value
                        if session.service_id is None
                        else None
                    ),
                    session=session,
                )
            if resource_id in self._ended_service_ids:
                raise GitSessionEndedError("Git 初始化会话已结束")

        # service_id 不在内存时不能从数据库恢复；仅真实 container_id 可以走持久化记录。
        with session_scope() as db_session:
            row = ContainerRepository(db_session).get(resource_id)
        if row is None or row.deleted_at is not None:
            raise GitResourceNotFoundError("Git 资源不存在")
        _ensure_user_match(row.user_id, operator_user_id)
        return GitResource(
            user_id=row.user_id,
            service_id=None,
            container_id=row.container_id,
            git_fin_status=row.git_fin_status,
        )

    def get_or_create_container_session(
        self,
        resource_id: str,
        operator_user_id: str,
    ) -> GitInitializationSession:
        """为运行中的真实容器创建按 `container_id` 定位的临时会话。"""
        resource = self.resolve_resource(resource_id, operator_user_id)
        if resource.session is not None:
            return resource.session
        if resource.container_id is None:
            raise GitResourceNotFoundError("Git 资源未绑定 container_id")
        if resource.git_fin_status != GitFinalStatus.INITIALIZED.value:
            raise GitSessionEndedError("Git 初始化会话未完成，不能建立运行期会话")

        with self._lock:
            existing = self._by_container_id.get(resource.container_id)
            if existing is not None:
                _ensure_user_match(existing.service_user, operator_user_id)
                return existing
            session = GitInitializationSession(
                service_id=None,
                service_user=resource.user_id,
                container_id=resource.container_id,
                git_status=GitStatus.INITIALIZED,
            )
            self._by_container_id[resource.container_id] = session
            return session

    def update_status(
        self,
        resource_id: str,
        operator_user_id: str,
        git_status: str | GitStatus,
    ) -> GitInitializationSession:
        """更新活跃会话的详细 Git 状态；不写数据库。"""
        try:
            status = coerce_git_status(git_status)
        except ValueError as exc:
            raise InvalidArgumentError("Git 状态非法") from exc
        resource = self.resolve_resource(resource_id, operator_user_id)
        if resource.session is None:
            raise GitSessionEndedError("Git 初始化会话已结束")
        with self._lock:
            if resource.session.ended:
                raise GitSessionEndedError("Git 初始化会话已结束")
            resource.session.git_status = status
            return resource.session

    def set_temporary_credential(
        self,
        resource_id: str,
        operator_user_id: str,
        credential: GitCredential,
    ) -> GitInitializationSession:
        """将非持久化凭证保存到当前资源的进程内存会话。"""
        resource = self.resolve_resource(resource_id, operator_user_id)
        session = (
            resource.session
            if resource.session is not None
            else self.get_or_create_container_session(resource_id, operator_user_id)
        )
        validate_credential(session.service_user, credential)
        with self._lock:
            if session.ended:
                raise GitSessionEndedError("Git 初始化会话已结束")
            session.temporary_git_username = credential.git_username
            session.temporary_git_email = credential.git_email
            session.temporary_git_password = credential.git_password
            session.temporary_type = credential.type
            session.credential_available = True
            session.credential_claimed = False
            return session

    def mark_credential_available(
        self,
        resource_id: str,
        operator_user_id: str,
    ) -> GitInitializationSession:
        """标记持久化凭证已可领取；明文凭证仍不进入会话。"""
        resource = self.resolve_resource(resource_id, operator_user_id)
        session = (
            resource.session
            if resource.session is not None
            else self.get_or_create_container_session(resource_id, operator_user_id)
        )
        with self._lock:
            if session.ended:
                raise GitSessionEndedError("Git 初始化会话已结束")
            session.credential_available = True
            session.credential_claimed = False
            return session

    def claim_temporary_credential(
        self,
        resource_id: str,
        operator_user_id: str,
    ) -> GitCredential:
        """原子领取并清除当前会话的临时明文凭证。"""
        resource = self.resolve_resource(resource_id, operator_user_id)
        session = resource.session
        if session is None:
            raise GitCredentialUnavailableError("当前资源没有临时凭证")
        with self._lock:
            if session.ended:
                raise GitSessionEndedError("Git 初始化会话已结束")
            if not session.credential_available:
                if session.credential_claimed:
                    raise GitCredentialAlreadyClaimedError("临时 Git 凭证已经领取")
                raise GitCredentialUnavailableError("当前没有可领取的 Git 凭证")
            temporary_type = session.temporary_type
            temporary_git_username = session.temporary_git_username
            temporary_git_email = session.temporary_git_email
            temporary_git_password = session.temporary_git_password
            if (
                temporary_type is None
                or temporary_git_username is None
                or temporary_git_email is None
                or temporary_git_password is None
            ):
                session.clear_temporary_credential()
                raise GitCredentialUnavailableError("当前临时 Git 凭证数据无效")
            credential = GitCredential(
                type=temporary_type,
                git_username=temporary_git_username,
                git_email=temporary_git_email,
                git_password=temporary_git_password,
            )
            session.clear_temporary_credential(claimed=True)
            return credential

    def end_session(
        self,
        resource_id: str,
        *,
        final_status: Optional[str | GitFinalStatus | GitStatus] = None,
    ) -> None:
        """清除会话敏感数据并移除活跃映射；数据库最终写入由 C53.9 负责。"""
        resource_id = _require_id(resource_id, "resource_id")
        final_git_status: Optional[GitStatus] = None
        if final_status is not None:
            try:
                final_git_status = GitStatus(coerce_git_final_status(final_status).value)
            except (TypeError, ValueError) as exc:
                raise InvalidArgumentError("Git 最终状态非法") from exc

        with self._lock:
            session = self._by_service_id.get(resource_id)
            if session is None:
                session = self._by_container_id.get(resource_id)
            if session is None:
                self._raise_missing_active_session(resource_id)
            if final_git_status is not None:
                session.git_status = final_git_status
            session.clear_temporary_credential()
            session.ended = True
            if session.service_id is not None:
                self._by_service_id.pop(session.service_id, None)
                self._remember_ended_service_id(session.service_id)
            if session.container_id is not None:
                self._by_container_id.pop(session.container_id, None)

    def discard_session(self, service_id: str) -> None:
        """创建流程异常清理；不存在或已清理时保持幂等。"""
        service_id = _require_id(service_id, "service_id")
        with self._lock:
            session = self._by_service_id.get(service_id)
            if session is None:
                return
            session.clear_temporary_credential()
            session.ended = True
            self._by_service_id.pop(service_id, None)
            if session.container_id is not None:
                self._by_container_id.pop(session.container_id, None)
            self._remember_ended_service_id(service_id)

    def _remember_ended_service_id(self, service_id: str) -> None:
        if service_id in self._ended_service_ids:
            return
        self._ended_service_ids.add(service_id)
        self._ended_service_order.append(service_id)
        while len(self._ended_service_order) > _ENDED_SERVICE_ID_LIMIT:
            expired = self._ended_service_order.popleft()
            self._ended_service_ids.discard(expired)

    def _raise_missing_active_session(self, resource_id: str) -> NoReturn:
        if resource_id in self._ended_service_ids:
            raise GitSessionEndedError("Git 初始化会话已结束")
        raise GitResourceNotFoundError("Git 初始化会话不存在")


_git_session_store = GitSessionStore()


def get_git_session_store() -> GitSessionStore:
    """返回进程级 Git 会话存储单例。"""
    return _git_session_store


def _require_id(value: Optional[str], field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidArgumentError(f"{field_name} 不能为空")
    return value.strip()


def _require_operator_user_id(value: Optional[str]) -> str:
    if not isinstance(value, str) or not value.strip():
        raise UnauthorizedError("缺少 X-Operator-User-ID")
    return value.strip()


def _ensure_user_match(expected_user_id: str, operator_user_id: str) -> None:
    if expected_user_id != operator_user_id:
        raise GitResourceUserMismatchError("Git 资源不属于当前用户")
