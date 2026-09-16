"""
Git API 应用服务（C53.9）。

路由层只负责 HTTP 合同；本模块负责资源绑定、内存状态、凭证持久化/领取和最终状态事务。
不记录密码、凭证对象或 Git 原始错误详情。
"""

from __future__ import annotations

import logging
from typing import Optional

from application.blacklist import ensure_user_not_blacklisted
from application.git_credentials import (
    GitCredential,
    get_persisted_credential,
    save_credential,
)
from application.git_sessions import (
    GitInitializationSession,
    GitResource,
    get_git_session_store,
)
from domain.errors import (
    BusinessConflictError,
    ExternalDependencyError,
    GitCredentialAlreadyClaimedError,
    GitCredentialConflictError,
    GitCredentialUnavailableError,
    GitResourceNotFoundError,
    GitSessionEndedError,
    InvalidArgumentError,
    UnauthorizedError,
)
from domain.models import (
    GIT_INTERMEDIATE_STATUSES,
    GitFinalStatus,
    GitStatus,
    coerce_git_final_status,
    coerce_git_status,
    is_git_final_status,
)
from infra.db import session_scope
from infra.repositories import ContainerRepository

logger = logging.getLogger(__name__)

__all__ = [
    "get_git_state",
    "report_git_status",
    "get_git_credential",
    "submit_git_credential",
]


def get_git_state(service_id: str, operator_user_id: Optional[str]) -> GitStatus:
    """读取资源的详细 Git 状态。"""
    operator_user_id = _require_operator_user_id(operator_user_id)
    ensure_user_not_blacklisted(operator_user_id)
    resource = get_git_session_store().resolve_service_resource(
        service_id,
        operator_user_id,
    )
    if resource.session is not None:
        return resource.session.git_status
    if resource.git_fin_status is None:
        raise GitSessionEndedError("Git 初始化会话已结束或无法恢复")
    try:
        return GitStatus(resource.git_fin_status)
    except ValueError as exc:
        raise ExternalDependencyError("Git 最终状态数据无效") from exc


def report_git_status(
    service_id: str,
    operator_user_id: Optional[str],
    git_status: str | GitStatus,
) -> GitStatus:
    """接收中间状态或最终状态；最终状态成功写库后才清理会话。"""
    operator_user_id = _require_operator_user_id(operator_user_id)
    ensure_user_not_blacklisted(operator_user_id)
    try:
        status = coerce_git_status(git_status)
    except ValueError as exc:
        raise InvalidArgumentError("Git 状态非法") from exc

    store = get_git_session_store()
    resource = store.resolve_service_resource(service_id, operator_user_id)
    session = resource.session
    if status in GIT_INTERMEDIATE_STATUSES:
        if session is None:
            raise GitSessionEndedError("Git 初始化会话已结束")
        _validate_transition(session, status)
        store.update_status(service_id, operator_user_id, status)
        return status

    try:
        final_status = coerce_git_final_status(status)
    except ValueError as exc:
        raise InvalidArgumentError("Git 最终状态非法") from exc
    if resource.git_fin_status is not None:
        if (
            final_status is GitFinalStatus.INITIALIZED
            and resource.git_fin_status == GitFinalStatus.INITIALIZED.value
        ):
            _finish_git_session(store, service_id, final_status)
            return GitStatus(final_status.value)
        raise BusinessConflictError("Git 初始化已经有最终状态")
    if resource.container_id is None:
        raise GitSessionEndedError("Git 初始化会话已结束")
    if session is not None and session.ended:
        raise GitSessionEndedError("Git 初始化会话已结束")

    try:
        with session_scope() as db_session:
            updated, existing_status = ContainerRepository(db_session).set_git_fin_status_if_unset(
                resource.container_id,
                final_status.value,
            )
            if not updated:
                if existing_status is None:
                    raise GitResourceNotFoundError("容器记录不存在")
                if (
                    final_status is GitFinalStatus.INITIALIZED
                    and existing_status == GitFinalStatus.INITIALIZED.value
                ):
                    idempotent = True
                else:
                    raise BusinessConflictError("Git 初始化已经有最终状态")
            else:
                idempotent = False
    except (BusinessConflictError, GitResourceNotFoundError):
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error("Git 最终状态写入失败: %s", type(exc).__name__)
        raise ExternalDependencyError("保存 Git 初始化结果失败") from exc

    if idempotent or updated:
        _finish_git_session(store, service_id, final_status)
    return GitStatus(final_status.value)


def get_git_credential(
    service_id: str,
    operator_user_id: Optional[str],
) -> GitCredential:
    """领取临时或用户级凭证；无可用凭证时抛出携带状态的 409。"""
    operator_user_id = _require_operator_user_id(operator_user_id)
    ensure_user_not_blacklisted(operator_user_id)
    store = get_git_session_store()
    try:
        resource = store.resolve_service_resource(service_id, operator_user_id)
    except GitSessionEndedError as exc:
        raise GitCredentialConflictError(GitStatus.FAILED_SERVICE.value) from exc
    if resource.git_fin_status is not None and resource.git_fin_status.startswith("failed_"):
        raise GitCredentialConflictError(resource.git_fin_status)
    if resource.session is not None:
        try:
            return store.claim_temporary_credential(service_id, operator_user_id)
        except GitCredentialAlreadyClaimedError:
            raise
        except GitSessionEndedError as exc:
            raise GitCredentialConflictError(
                _credential_conflict_status(resource).value
            ) from exc
        except GitCredentialUnavailableError:
            pass

    credential = get_persisted_credential(resource.user_id)
    if credential is not None:
        return credential

    status_value: str = str(
        resource.session.git_status.value
        if resource.session is not None
        else _final_status(resource.git_fin_status).value
    )
    if status_value in ("starting", "processing"):
        status_value = "credential_required"
    raise GitCredentialConflictError(status_value)


def submit_git_credential(
    service_id: str,
    operator_user_id: Optional[str],
    *,
    credential: GitCredential,
    persist: bool,
) -> None:
    """提交持久化或当前会话临时凭证，不返回密码。"""
    operator_user_id = _require_operator_user_id(operator_user_id)
    ensure_user_not_blacklisted(operator_user_id)
    store = get_git_session_store()
    resource = store.resolve_service_resource(service_id, operator_user_id)
    if resource.git_fin_status == GitFinalStatus.INITIALIZED.value:
        if not persist:
            raise BusinessConflictError("初始化完成后不允许提交非持久化凭证")
        if not credential.git_password.strip():
            return
        save_credential(resource.user_id, credential, persist=True)
        return
    if resource.git_fin_status is not None:
        raise BusinessConflictError("Git 初始化已经有最终状态")
    if not credential.git_password.strip():
        report_git_status(service_id, operator_user_id, GitStatus.FAILED_USER_CANCELLED)
        return

    stored = save_credential(resource.user_id, credential, persist=persist)
    if persist:
        store.mark_credential_available(service_id, operator_user_id)
    else:
        if stored is None:
            raise ExternalDependencyError("Git 临时凭证保存失败")
        store.set_temporary_credential(service_id, operator_user_id, stored)


def _validate_transition(session: GitInitializationSession, target: GitStatus) -> None:
    """拒绝从终态回退或重新进入 starting；中间状态允许插件重试。"""
    if is_git_final_status(session.git_status.value):
        raise GitSessionEndedError("Git 初始化会话已结束")
    if target is GitStatus.STARTING and session.git_status is not GitStatus.STARTING:
        raise BusinessConflictError("Git 状态不能回退到 starting")


def _final_status(value: Optional[str]) -> GitStatus:
    if value is None:
        return GitStatus.STARTING
    try:
        return GitStatus(value)
    except ValueError as exc:
        raise ExternalDependencyError("Git 最终状态数据无效") from exc


def _credential_conflict_status(resource: GitResource) -> GitStatus:
    if resource.session is not None:
        return resource.session.git_status
    return _final_status(resource.git_fin_status)


def _finish_git_session(
    store,
    service_id: str,
    final_status: GitFinalStatus,
) -> None:
    """最终结果已落库后清理会话；重复上报时清理保持幂等。"""
    try:
        store.end_session(service_id, final_status=final_status)
    except (GitSessionEndedError, GitResourceNotFoundError):
        pass


def _require_operator_user_id(value: Optional[str]) -> str:
    if not isinstance(value, str) or not value.strip():
        raise UnauthorizedError("身份错误")
    return value.strip()
