"""
本地紧急管理员添加工具。

默认请求 `http://127.0.0.1:8080/admin/admin-users`，不发送
`X-Operator-User-ID`；服务端对回环客户端提供专用运维旁路。
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence

import requests

__all__ = [
    "add_admin_user",
    "main",
]


DEFAULT_PORT = 8080
REQUEST_TIMEOUT_SECONDS = 10.0


def add_admin_user(
    user_id: str,
    *,
    timeout: float = REQUEST_TIMEOUT_SECONDS,
) -> requests.Response:
    """向本地服务发送添加管理员请求，不附带操作用户请求头。"""
    if not user_id or not user_id.strip():
        raise ValueError("用户 ID 不能为空")
    url = f"{_configured_base_url()}/admin/admin-users"
    return requests.post(
        url,
        json={"user_id": user_id},
        timeout=timeout,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="本地运维工具 - 本地无验证管理管理员用户")
    subparsers = parser.add_subparsers(dest="command", required=True)
    add_parser = subparsers.add_parser("add", help="添加管理员")
    add_parser.add_argument("user_id", help="管理员用户 ID")
    args = parser.parse_args(argv)

    try:
        response = add_admin_user(args.user_id)
    except (requests.RequestException, ValueError) as exc:
        print(f"添加管理员请求失败: {exc}", file=sys.stderr)
        return 1

    if response.status_code == 200:
        print(f"管理员添加成功: {args.user_id}")
        return 0

    try:
        detail = response.json()
    except ValueError:
        detail = response.text
    print(
        f"管理员添加失败 (HTTP {response.status_code}): {detail}",
        file=sys.stderr,
    )
    return 1


def _configured_base_url() -> str:
    raw_port = os.environ.get("TA_SS_REST_API_PORT") or str(DEFAULT_PORT)
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise ValueError("TA_SS_REST_API_PORT 必须是 1-65535 的整数") from exc
    if not 1 <= port <= 65535:
        raise ValueError("TA_SS_REST_API_PORT 必须是 1-65535 的整数")
    return f"http://127.0.0.1:{port}"


if __name__ == "__main__":
    raise SystemExit(main())
