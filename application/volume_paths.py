"""FileBrowser 卷目录与 OpenSandbox 挂载路径规划。"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass
from typing import Optional

from infra.opensandbox.types import SandboxVolume

__all__ = [
    "FILEBROWSER_MOUNT_PATHS",
    "VolumePathError",
    "MountPathPlan",
    "VolumePathPlan",
    "build_volume_path_plan",
    "build_sandbox_volumes",
]


# 每个清单项同时表示容器目标路径和 service 目录下的相对路径后缀。
FILEBROWSER_MOUNT_PATHS: list[str] = [
    "/app",
    "/root/.git-helper",
]


class VolumePathError(ValueError):
    """用户、服务或容器标识不能安全地作为 POSIX 路径段。"""


@dataclass(frozen=True)
class MountPathPlan:
    """一个挂载清单项对应的 FileBrowser 路径和 PVC 子路径。"""

    mount_path: str
    filebrowser_path: str
    pvc_sub_path: str


@dataclass(frozen=True)
class VolumePathPlan:
    """一次沙盒卷生命周期所需的完整路径规划。"""

    user_id: str
    service_id: str
    user_path: str
    service_path: str
    directory_paths: tuple[str, ...]
    mount_paths: tuple[MountPathPlan, ...]
    reusable_paths: tuple[str, ...]
    exclusive_paths: tuple[str, ...]
    cleanup_paths: tuple[str, ...]
    container_id: Optional[str] = None
    container_marker_path: Optional[str] = None

    @property
    def mount_manifest(self) -> tuple[str, ...]:
        """返回固定挂载目标路径清单。"""
        return tuple(item.mount_path for item in self.mount_paths)


def build_volume_path_plan(
    user_id: str,
    service_id: str,
    container_id: Optional[str] = None,
) -> VolumePathPlan:
    """生成用户/服务目录、挂载目录、PVC 子路径和清理路径。

    `container_id` 在 OpenSandbox 创建成功后才可用；未提供时不规划容器 ID
    零字节标记文件。所有路径使用 POSIX 语义，不依赖宿主机操作系统。
    """
    _validate_path_segment(user_id, "user_id")
    _validate_path_segment(service_id, "service_id")
    if container_id is not None:
        _validate_path_segment(container_id, "container_id")

    user_path = f"/{user_id}"
    service_path = posixpath.join(user_path, service_id)
    mount_paths: list[MountPathPlan] = []
    directory_paths: list[str] = [user_path, service_path]

    for mount_path in FILEBROWSER_MOUNT_PATHS:
        _validate_mount_path(mount_path)
        relative_path = mount_path.lstrip("/")
        filebrowser_path = posixpath.join(service_path, relative_path)
        mount_plan = MountPathPlan(
            mount_path=mount_path,
            filebrowser_path=filebrowser_path,
            pvc_sub_path=filebrowser_path.lstrip("/"),
        )
        mount_paths.append(mount_plan)
        for parent_path in _path_prefixes(filebrowser_path):
            if parent_path not in directory_paths:
                directory_paths.append(parent_path)

    marker_path = (
        posixpath.join(service_path, container_id)
        if container_id is not None
        else None
    )
    return VolumePathPlan(
        user_id=user_id,
        service_id=service_id,
        user_path=user_path,
        service_path=service_path,
        directory_paths=tuple(directory_paths),
        mount_paths=tuple(mount_paths),
        # Only the user directory may be reused.  The service directory and
        # final mount directories must be treated as conflicts if present.
        reusable_paths=(user_path,),
        exclusive_paths=(
            service_path,
            *(item.filebrowser_path for item in mount_paths),
        ),
        cleanup_paths=(service_path, user_path),
        container_id=container_id,
        container_marker_path=marker_path,
    )


def build_sandbox_volumes(plan: VolumePathPlan, pvc_name: str) -> list[SandboxVolume]:
    """根据路径规划生成挂载描述；所有挂载引用同一个预存在 PVC。"""
    if not isinstance(pvc_name, str) or not pvc_name.strip():
        raise VolumePathError("PVC 名称不能为空")
    claim_name = pvc_name.strip()
    return [
        SandboxVolume(
            name=f"testagent-volume-{index}",
            mount_path=mount.mount_path,
            claim_name=claim_name,
            sub_path=mount.pvc_sub_path,
            read_only=False,
        )
        for index, mount in enumerate(plan.mount_paths, start=1)
    ]


def _validate_path_segment(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value or not value.strip():
        raise VolumePathError(f"{field_name} 不能为空")
    if value in (".", ".."):
        raise VolumePathError(f"{field_name} 不能为路径特殊段")
    if "/" in value or "\\" in value:
        raise VolumePathError(f"{field_name} 不能包含路径分隔符")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise VolumePathError(f"{field_name} 不能包含控制字符")


def _validate_mount_path(mount_path: str) -> None:
    if not isinstance(mount_path, str) or not mount_path.startswith("/"):
        raise VolumePathError("挂载路径必须以 `/` 开头")
    segments = mount_path.split("/")
    if any(
        not segment
        or segment in (".", "..")
        or "\\" in segment
        or any(ord(char) < 32 or ord(char) == 127 for char in segment)
        for segment in segments[1:]
    ):
        raise VolumePathError("挂载路径包含非法路径段")


def _path_prefixes(path: str) -> list[str]:
    """返回从根到目标路径的所有前缀，供递归创建按层级执行。"""
    segments = path.strip("/").split("/")
    prefixes: list[str] = []
    current = ""
    for segment in segments:
        current = posixpath.join(current, segment)
        prefixes.append("/" + current.lstrip("/"))
    return prefixes
