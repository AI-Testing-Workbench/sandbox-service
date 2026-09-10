"""
Git 凭证应用能力（Git 凭证设计报告 §2.8、§2.9、§5.5、§5.6）。

- 应用层负责凭证字段校验和持久化策略选择。
- 持久化凭证由 AES-GCM 加密后交给 Repository 写入用户级数据库。
- 非持久化凭证不打开数据库，只返回给后续内存会话层保存。
- 本模块不记录或构造对外状态响应；含密码的对象仅供已完成授权校验的凭证领取流程使用。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from domain.errors import InvalidArgumentError
from infra.db import session_scope
from infra.git_crypto import GitCryptoError, initialize_git_credential_crypto
from infra.orm import GitCredentialRow
from infra.repositories import GitCredentialRepository

__all__ = [
    "GitCredential",
    "validate_credential",
    "save_credential",
    "get_persisted_credential",
]


@dataclass(frozen=True)
class GitCredential:
    """当前 Git 凭证；密码禁止出现在对象默认 repr 中。"""

    type: str
    git_username: str
    git_email: str
    git_password: str = field(repr=False)


def validate_credential(user_id: str, credential: GitCredential) -> None:
    """校验用户 ID 和当前 password 类型凭证的必填字段。"""
    if not isinstance(user_id, str) or not user_id.strip():
        raise InvalidArgumentError("用户 ID 不能为空")
    if not isinstance(credential, GitCredential):
        raise InvalidArgumentError("Git 凭证参数非法")
    if credential.type != "password":
        raise InvalidArgumentError("Git 凭证 type 目前只支持 password")
    _require_text(credential.git_username, "git_username")
    _require_text(credential.git_email, "git_email")
    _require_text(credential.git_password, "git_password")


def save_credential(
    user_id: str,
    credential: GitCredential,
    *,
    persist: bool,
) -> GitCredential | None:
    """保存当前凭证。

    `persist=True` 时加密并覆盖用户级数据库凭证，成功后不返回密码；`persist=False`
    时不访问数据库，返回凭证对象供 C53.5 内存会话接管。
    """
    validate_credential(user_id, credential)
    if not isinstance(persist, bool):
        raise InvalidArgumentError("persist 必须为布尔值")
    if not persist:
        return credential

    with session_scope() as session:
        cipher = initialize_git_credential_crypto(session)
        GitCredentialRepository(session).upsert(
            GitCredentialRow(
                user_id=user_id,
                type=credential.type,
                git_username=credential.git_username,
                git_email=credential.git_email,
                git_password=cipher.encrypt(credential.git_password),
            )
        )
    return None


def get_persisted_credential(user_id: str) -> GitCredential | None:
    """读取并解密用户级凭证。

    调用方必须先完成 Git 资源与请求用户的绑定校验；本函数不承担 HTTP 授权。
    """
    if not isinstance(user_id, str) or not user_id.strip():
        raise InvalidArgumentError("用户 ID 不能为空")

    with session_scope() as session:
        repository = GitCredentialRepository(session)
        row = repository.get(user_id)
        if row is None:
            return None
        cipher = initialize_git_credential_crypto(session)
        try:
            password = cipher.decrypt(row.git_password)
        except GitCryptoError as exc:
            # 启动自检已覆盖历史密文；再次读取时仍禁止向上层暴露底层密文细节。
            raise InvalidArgumentError("Git 凭证无法读取") from exc
        return GitCredential(
            type=row.type,
            git_username=row.git_username,
            git_email=row.git_email,
            git_password=password,
        )


def _require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise InvalidArgumentError(f"{field_name} 不能为空")
