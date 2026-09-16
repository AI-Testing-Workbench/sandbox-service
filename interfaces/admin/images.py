"""
管理端镜像 API。
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import BinaryIO, Optional, cast

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile

from application import image as image_service
from config import Constants, settings
from domain.errors import ExternalDependencyError
from domain.models import ContainerType
from interfaces.admin.auth import require_admin_access
from interfaces.admin.schemas import (
    DefaultImageResponse,
    ImageDeleteRequest,
    ImageListItem,
    ImageListResponse,
    ImageReferenceRequest,
    SetDefaultImageRequest,
)
from interfaces.common import ErrorResponse, api_responses

__all__ = [
    "router",
]


router = APIRouter(
    prefix="/admin/images",
    tags=["管理员 API (镜像操作)"],
    dependencies=[Depends(require_admin_access)],
    responses={
        401: {"model": ErrorResponse, "description": "未认证"},
        403: {"model": ErrorResponse, "description": "用户被禁止"},
    },
)


# 镜像接口顺序：上传、推送、获取清单、删除、设置默认、删除默认、获取默认
@router.post(
    "/upload",
    status_code=204,
    responses=api_responses("成功 (无内容)", 204, 400, 502),
)
def upload_image(
    file: UploadFile = File(..., description="镜像归档文件，仅支持 .tar 或 .tar.gz"),
    registry: Optional[str] = Form(
        default=settings.image_default_registry,
        description="注册表地址 (可选)",
    ),
    namespace: Optional[str] = Form(
        default=settings.image_default_namespace,
        description="命名空间 (可选)",
    ),
    auto_push: bool = Form(
        default=True,
        description="上传后是否自动推送到注册表 (可选)",
    ),
) -> Response:
    """上传镜像文件。"""
    temp_path = _save_upload(file)
    try:
        image_service.upload_image(
            temp_path,
            registry=registry,
            namespace=namespace,
            auto_push=auto_push,
        )
        return Response(status_code=204)
    finally:
        # 应用层负责正常流程清理，这里覆盖保存后调用失败等边界。
        try:
            Path(temp_path).unlink(missing_ok=True)
        except OSError:
            pass


@router.post(
    "/push",
    status_code=204,
    responses=api_responses("成功 (无内容)", 204, 400, 502),
)
def push_image(request: ImageReferenceRequest) -> Response:
    """推送指定镜像至注册表。"""
    image_service.push_image(request.full_name)
    return Response(status_code=204)


@router.get(
    "",
    response_model=ImageListResponse,
    responses=api_responses("成功", 200, 502),
)
def list_images() -> ImageListResponse:
    """获取本地镜像清单（不自动探测推送状态，避免慢 Registry 拖慢列表）。"""
    rows = image_service.list_images()
    return ImageListResponse(images=[_image_item(row) for row in rows])


@router.post(
    "/check",
    response_model=ImageListResponse,
    responses=api_responses("成功", 200, 502),
)
def check_image_push_states() -> ImageListResponse:
    """手动全量刷新镜像推送状态（对 Registry 逐个探测，管理员显式触发）。"""
    rows = image_service.check_image_push_states()
    return ImageListResponse(images=[_image_item(row) for row in rows])


@router.post(
    "/delete",
    status_code=204,
    responses=api_responses("成功 (无内容)", 204, 400, 409, 502),
)
def delete_image(request: ImageDeleteRequest) -> Response:
    """删除指定镜像。"""
    result = image_service.delete_image(request.full_name, request.also_registry)
    headers = {"X-Registry-Delete-Failed": "true"} if result.registry_failed else None
    return Response(status_code=204, headers=headers)


@router.post(
    "/default",
    status_code=204,
    responses=api_responses("成功 (无内容)", 204, 400, 409, 502),
)
def set_default_image(request: SetDefaultImageRequest) -> Response:
    """设置指定容器类型的默认镜像。"""
    image_service.set_default_image(request.full_name, request.type)
    return Response(status_code=204)


@router.post(
    "/default/unset",
    status_code=204,
    responses=api_responses("成功 (无内容)", 204),
)
def unset_default_image(
    container_type: ContainerType = Query(
        default=ContainerType.TESTAGENT_CLOUD,
        description="容器类型：testagent_cloud / autotest_cloud",
    ),
) -> Response:
    """取消设置指定容器类型的默认镜像。"""
    image_service.unset_default_image(container_type)
    return Response(status_code=204)


@router.get(
    "/default",
    response_model=DefaultImageResponse,
    responses=api_responses("成功", 200),
)
def get_default_image(
    container_type: ContainerType = Query(
        default=ContainerType.TESTAGENT_CLOUD,
        description="容器类型：testagent_cloud / autotest_cloud",
    ),
) -> DefaultImageResponse:
    """获取指定容器类型的默认镜像。"""
    return DefaultImageResponse(
        type=container_type.value,
        full_name=image_service.get_default_image(container_type),
    )


def _save_upload(file: UploadFile) -> str:
    filename = (file.filename or "").lower()
    if filename.endswith(".tar.gz"):
        suffix = ".tar.gz"
    elif filename.endswith(".tar"):
        suffix = ".tar"
    else:
        suffix = Path(filename).suffix or ".upload"

    directory = Path(Constants.UPLOAD_TEMP_PATH.value)
    directory.mkdir(parents=True, exist_ok=True)
    descriptor, path = tempfile.mkstemp(prefix="image-", suffix=suffix, dir=directory)
    try:
        source: BinaryIO = file.file
        with cast(BinaryIO, os.fdopen(descriptor, "wb")) as output:
            shutil.copyfileobj(source, output)
    except Exception as exc:
        try:
            os.close(descriptor)
        except OSError:
            pass
        Path(path).unlink(missing_ok=True)
        raise ExternalDependencyError("保存上传镜像失败") from exc
    return path


def _image_item(row: image_service.ImageRow) -> ImageListItem:
    return ImageListItem(
        id=row.id,
        full_name=row.full_name,
        registry=row.registry,
        namespace=row.namespace,
        name=row.name,
        version=row.version,
        created_at=row.created_at.isoformat() if row.created_at is not None else None,
        size=row.size,
        status=row.status.value,
    )
