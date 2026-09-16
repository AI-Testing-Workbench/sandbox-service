"""FileBrowser 卷目录的创建、冲突检查和创建失败回滚。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from domain.errors import VolumePathConflictError
from infra.filebrowser import FileBrowserClient, FileBrowserNotFoundError

from application.volume_paths import VolumePathPlan

logger = logging.getLogger(__name__)

__all__ = [
    "PreparedVolumeDirectories",
    "prepare_volume_directories",
    "rollback_volume_directories",
]


@dataclass(frozen=True)
class PreparedVolumeDirectories:
    """记录本次创建实际新建的顶层目录，供后续失败回滚。"""

    plan: VolumePathPlan
    user_path_created: bool
    service_path_created: bool


def prepare_volume_directories(
    client: FileBrowserClient,
    plan: VolumePathPlan,
) -> PreparedVolumeDirectories:
    """按规划递归创建目录，并只允许复用已存在的用户目录。"""
    user_path_created = False
    service_path_created = False
    try:
        user_resource = _get_resource(client, plan.user_path)
        if user_resource is None:
            client.create_directory(plan.user_path)
            user_path_created = True
        elif not user_resource.is_directory:
            raise VolumePathConflictError("FileBrowser 用户路径不是目录")

        service_resource = _get_resource(client, plan.service_path)
        if service_resource is not None:
            raise VolumePathConflictError("FileBrowser service 目录已存在")
        client.create_directory(plan.service_path)
        service_path_created = True

        exclusive_paths = set(plan.exclusive_paths)
        for directory_path in plan.directory_paths:
            if directory_path in (plan.user_path, plan.service_path):
                continue
            resource = _get_resource(client, directory_path)
            if resource is not None:
                if directory_path in exclusive_paths or not resource.is_directory:
                    raise VolumePathConflictError("FileBrowser 挂载目录已存在或类型错误")
                continue
            client.create_directory(directory_path)

        return PreparedVolumeDirectories(
            plan=plan,
            user_path_created=user_path_created,
            service_path_created=service_path_created,
        )
    except Exception:
        rollback_volume_directories(
            client,
            PreparedVolumeDirectories(
                plan=plan,
                user_path_created=user_path_created,
                service_path_created=service_path_created,
            ),
        )
        raise


def rollback_volume_directories(
    client: Optional[FileBrowserClient],
    prepared: Optional[PreparedVolumeDirectories],
) -> None:
    """尽力删除本次创建的 service/用户目录，绝不删除复用目录。"""
    if prepared is None or client is None:
        return
    paths_to_delete: list[str] = []
    if prepared.service_path_created:
        paths_to_delete.append(prepared.plan.service_path)
    if prepared.user_path_created:
        paths_to_delete.append(prepared.plan.user_path)
    for path in paths_to_delete:
        try:
            client.delete_resource(path)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "FileBrowser 回滚目录失败: path=%s, %s",
                path,
                type(exc).__name__,
            )


def _get_resource(client: FileBrowserClient, path: str):
    try:
        return client.get_resource(path)
    except FileBrowserNotFoundError:
        return None
