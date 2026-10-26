"""
Best-effort service-volume copy for source-based container creation.
"""

from __future__ import annotations

import logging

from application.volume_paths import VolumePathPlan
from application.volume_storage import prepare_volume_directories
from infra.filebrowser import FileBrowserClient, FileBrowserNotFoundError

logger = logging.getLogger(__name__)

__all__ = [
    "copy_service_volumes_best_effort",
    "restore_volume_directories_best_effort",
]


def copy_service_volumes_best_effort(
    client: FileBrowserClient,
    source: VolumePathPlan,
    target: VolumePathPlan,
) -> bool:
    """Copy each configured mount tree, recovering empty targets after failure."""
    if source.mount_manifest != target.mount_manifest:
        raise ValueError("来源和目标的挂载清单不一致")

    resources = [
        (source_mount.filebrowser_path, target_mount.filebrowser_path)
        for source_mount, target_mount in zip(source.mount_paths, target.mount_paths)
    ]
    try:
        client.copy_resources(resources)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "FileBrowser 来源卷复制失败，继续创建并恢复目标挂载目录: source=%s, target=%s, error=%s",
            source.service_path,
            target.service_path,
            type(exc).__name__,
        )
        _reset_target_directories(client, target)
        return False


def _reset_target_directories(client: FileBrowserClient, target: VolumePathPlan) -> None:
    """Remove partial target data and recreate mount directories if possible."""
    removed = False
    try:
        removed = client.delete_resource(target.service_path)
        if not removed:
            logger.error("FileBrowser 复制目标清理未成功: %s", target.service_path)
    except FileBrowserNotFoundError:
        removed = True
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "FileBrowser 复制目标清理失败: path=%s, error=%s",
            target.service_path,
            type(exc).__name__,
        )

    if removed:
        try:
            prepare_volume_directories(client, target)
            return
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "FileBrowser 复制失败后恢复空目标目录失败: path=%s, error=%s",
                target.service_path,
                type(exc).__name__,
            )

    # Cleanup itself is best-effort too. Ensure every required directory still
    # exists where possible, without ever touching the source service path.
    for path in target.directory_paths:
        try:
            resource = client.get_resource(path)
        except FileBrowserNotFoundError:
            try:
                client.create_directory(path)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "FileBrowser 复制失败后创建目标目录失败: path=%s, error=%s",
                    path,
                    type(exc).__name__,
                )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "FileBrowser 复制失败后查询目标目录失败: path=%s, error=%s",
                path,
                type(exc).__name__,
            )
        else:
            if not resource.is_directory:
                logger.error("FileBrowser 复制目标路径不是目录: %s", path)


def restore_volume_directories_best_effort(
    client: FileBrowserClient,
    target: VolumePathPlan,
) -> None:
    """Reset a target after source-path planning failed, without blocking create."""
    _reset_target_directories(client, target)
