"""FileBrowser Quantum API 的基础响应类型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

__all__ = [
    "FileBrowserResource",
    "FileBrowserItem",
    "FileBrowserItems",
]


@dataclass(frozen=True)
class FileBrowserResource:
    """`GET /api/resources` 返回的资源摘要。"""

    path: str
    resource_type: str
    name: Optional[str] = None
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def is_directory(self) -> bool:
        """判断资源是否为目录。"""
        return self.resource_type.lower() == "directory"


@dataclass(frozen=True)
class FileBrowserItem:
    """目录子项的最小信息。"""

    name: str
    resource_type: str
    path: Optional[str] = None
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def is_directory(self) -> bool:
        """判断子项是否为目录。"""
        return self.resource_type.lower() == "directory"


@dataclass(frozen=True)
class FileBrowserItems:
    """`GET /api/resources/items` 返回的目录子项。"""

    files: tuple[FileBrowserItem, ...] = ()
    folders: tuple[FileBrowserItem, ...] = ()
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        """判断目录是否没有文件和子目录。"""
        return not self.files and not self.folders
