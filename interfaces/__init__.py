"""
对外接口层：REST API。

公共模块：`app`（应用装配）、`common`（公共响应模型）、`user`（用户 API）、`admin`（管理 API）、
`git`（Git 凭证 API）。
`volume`（FileBrowser 卷状态 API）。
"""

from __future__ import annotations

__all__ = [
    "app",
    "auth",
    "common",
    "common_container_routes",
    "user",
    "admin",
    "git",
    "volume",
]
