"""
Git 凭证加密基础设施（Git 凭证设计报告 §2.4）。

- 使用 AES-256-GCM 加密用户 Git 密码；每次加密生成独立随机 nonce。
- 密钥默认位于数据库文件同目录的 `git_credentials.key`。
- 启动时校验 settings 中的测试原文/密文及所有历史 Git 凭证，校验失败即阻止启动。
- 本模块不记录密钥、测试数据或 Git 密码。
"""

from __future__ import annotations

import base64
import binascii
import os
import secrets
from pathlib import Path
from typing import Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import select
from sqlalchemy.orm import Session

from config import Constants
from infra.orm import GitCredentialRow
from infra.repositories import SettingsRepository

__all__ = [
    "GitCryptoError",
    "GitCredentialCipher",
    "GIT_CRYPTO_TEST_PLAINTEXT_KEY",
    "GIT_CRYPTO_TEST_CIPHERTEXT_KEY",
    "get_git_credentials_key_path",
    "load_git_credentials_cipher",
    "initialize_git_credential_crypto",
]

GIT_CRYPTO_TEST_PLAINTEXT_KEY = "git_crypto_test_plaintext"
GIT_CRYPTO_TEST_CIPHERTEXT_KEY = "git_crypto_test_ciphertext"

_KEY_SIZE_BYTES = 32
_NONCE_SIZE_BYTES = 12
_TAG_SIZE_BYTES = 16
_TEST_PLAINTEXT_SIZE_BYTES = 32


class GitCryptoError(RuntimeError):
    """Git 凭证密钥、密文或启动自检失败。"""


class GitCredentialCipher:
    """使用单个 AES-256-GCM 密钥执行 Git 凭证加解密。"""

    def __init__(self, key: bytes) -> None:
        if len(key) != _KEY_SIZE_BYTES:
            raise GitCryptoError("Git 凭证密钥长度非法")
        self._key = bytes(key)
        self._aes = AESGCM(self._key)

    @classmethod
    def from_key_file(cls, key_path: str | Path) -> GitCredentialCipher:
        """读取密钥文件；文件缺失时安全生成一个新的 256 位密钥。"""
        path = Path(key_path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with path.open("xb") as key_file:
                    key_file.write(AESGCM.generate_key(bit_length=256))
            except FileExistsError:
                pass
            key = path.read_bytes()
            # Unix 下限制密钥文件权限；Windows 下 chmod 可能不完全等价，不能因此阻止启动。
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        except OSError as exc:
            raise GitCryptoError("Git 凭证密钥文件不可用") from exc
        return cls(key)

    def encrypt_bytes(self, plaintext: bytes) -> str:
        """加密字节并返回 URL-safe Base64 编码的 nonce + 密文 + 认证标签。"""
        nonce = secrets.token_bytes(_NONCE_SIZE_BYTES)
        sealed = self._aes.encrypt(nonce, plaintext, None)
        return base64.urlsafe_b64encode(nonce + sealed).decode("ascii")

    def decrypt_bytes(self, encoded: str) -> bytes:
        """解码并解密凭证密文；任何格式或认证错误都转换为摘要异常。"""
        if not isinstance(encoded, str) or not encoded:
            raise GitCryptoError("Git 凭证密文无效")
        try:
            payload = base64.b64decode(
                encoded.encode("ascii"),
                altchars=b"-_",
                validate=True,
            )
        except (binascii.Error, UnicodeEncodeError, ValueError) as exc:
            raise GitCryptoError("Git 凭证密文无效") from exc
        if len(payload) < _NONCE_SIZE_BYTES + _TAG_SIZE_BYTES:
            raise GitCryptoError("Git 凭证密文无效")
        nonce = payload[:_NONCE_SIZE_BYTES]
        sealed = payload[_NONCE_SIZE_BYTES:]
        try:
            return self._aes.decrypt(nonce, sealed, None)
        except (InvalidTag, ValueError, TypeError) as exc:
            raise GitCryptoError("Git 凭证密文无法解密") from exc

    def encrypt(self, plaintext: str) -> str:
        """加密 UTF-8 字符串。"""
        if not isinstance(plaintext, str):
            raise GitCryptoError("Git 凭证原文类型无效")
        return self.encrypt_bytes(plaintext.encode("utf-8"))

    def decrypt(self, encoded: str) -> str:
        """解密 UTF-8 字符串。"""
        try:
            return self.decrypt_bytes(encoded).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise GitCryptoError("Git 凭证明文编码无效") from exc


def get_git_credentials_key_path() -> Path:
    """返回密钥路径，默认与 SQLite 数据库位于同一 `data` 目录。"""
    db_path_value = Constants.DB_PATH.value
    if db_path_value == ":memory:":
        return Path(Constants.APP_ROOT_PATH.value) / "data" / "git_credentials.key"
    db_path = Path(db_path_value)
    if not db_path.is_absolute():
        db_path = Path(Constants.APP_ROOT_PATH.value) / db_path
    return db_path.parent / "git_credentials.key"


def load_git_credentials_cipher(
    key_path: Optional[str | Path] = None,
) -> GitCredentialCipher:
    """加载默认或指定密钥文件并返回加解密器。"""
    return GitCredentialCipher.from_key_file(
        get_git_credentials_key_path() if key_path is None else key_path
    )


def initialize_git_credential_crypto(
    session: Session,
    *,
    key_path: Optional[str | Path] = None,
) -> GitCredentialCipher:
    """执行 Git 凭证启动自检并返回已加载的加解密器。

    首次初始化会在同一事务中写入随机测试原文和密文；任一校验数据缺失、密文无法
    解密、校验不一致或历史 Git 凭证无法解密时抛出 `GitCryptoError`。
    """
    cipher = load_git_credentials_cipher(key_path)
    settings_repo = SettingsRepository(session)
    plaintext_row = settings_repo.get(GIT_CRYPTO_TEST_PLAINTEXT_KEY)
    ciphertext_row = settings_repo.get(GIT_CRYPTO_TEST_CIPHERTEXT_KEY)

    if plaintext_row is None and ciphertext_row is None:
        plaintext = secrets.token_urlsafe(_TEST_PLAINTEXT_SIZE_BYTES)
        settings_repo.set(GIT_CRYPTO_TEST_PLAINTEXT_KEY, plaintext)
        settings_repo.set(GIT_CRYPTO_TEST_CIPHERTEXT_KEY, cipher.encrypt(plaintext))
    elif plaintext_row is None or ciphertext_row is None:
        raise GitCryptoError("Git 凭证密钥校验数据不完整")
    else:
        if not plaintext_row.value or not ciphertext_row.value:
            raise GitCryptoError("Git 凭证密钥校验数据无效")
        try:
            decrypted = cipher.decrypt(ciphertext_row.value)
        except GitCryptoError as exc:
            raise GitCryptoError("Git 凭证密钥自检失败") from exc
        if decrypted.encode("utf-8") != plaintext_row.value.encode("utf-8"):
            raise GitCryptoError("Git 凭证密钥自检失败")

    for encrypted_password in session.scalars(
        select(GitCredentialRow.git_password)
    ):
        try:
            cipher.decrypt(encrypted_password)
        except GitCryptoError as exc:
            raise GitCryptoError("已有 Git 凭证无法使用当前密钥解密") from exc

    return cipher
