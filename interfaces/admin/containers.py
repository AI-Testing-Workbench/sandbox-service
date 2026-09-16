"""
管理端容器、孤儿容器、容器日志与数量限制 API。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from fastapi.responses import PlainTextResponse

from application import container as container_service
from application import image as image_service
from interfaces.admin.auth import require_admin_access
from interfaces.admin.schemas import (
    AdminContainerListResponse,
    AdminContainerResponse,
    AdminCreateContainerRequest,
    AdminCreateContainerResponse,
    ContainerLimitRequest,
    ContainerLimitResponse,
    ExpirationRequest,
    ExpirationResponse,
    OrphanContainerDeleteRequest,
    OrphanContainerListResponse,
    PodDeleteRequest,
)
from interfaces.common_container_routes import register_container_action_routes
from interfaces.common import ErrorResponse, api_responses

__all__ = [
    "router",
]


router = APIRouter(
    prefix="/admin/containers",
    tags=["管理员 API (容器操作)"],
    dependencies=[Depends(require_admin_access)],
    responses={
        401: {"model": ErrorResponse, "description": "未认证"},
        403: {"model": ErrorResponse, "description": "用户被禁止"},
    },
)


# 管理端容器接口顺序：增加、批量获取、单项获取、日志、Start、Stop、Restart、删除、永久删除、Expiration、恢复
@router.post(
    "",
    response_model=AdminCreateContainerResponse,
    status_code=200,
    responses=api_responses("成功", 200, 400, 409, 502),
)
def create_container(request: AdminCreateContainerRequest) -> AdminCreateContainerResponse:
    """创建并启动容器，包含供管理员使用的完整字段。"""
    full_name = (
        image_service.normalize_full_name(request.image)
        if request.image is not None
        else None
    )
    created = container_service.create_container(
        container_service.CreateContainerParams(
            user_id=request.user_id,
            image=full_name,
            container_type=request.type,
            gitee_url=request.gitee_url,
            gitee_user=request.gitee_user,
            gitee_repository=request.gitee_repository,
            gitee_branch=request.gitee_branch,
            expiration_hours=request.expiration_hours,
            authorize_general_account=request.authorize_general_account,
            cpu=request.cpu,
            memory=request.memory,
        )
    )
    view = _container_response(container_service.get_admin_container(created.container_id))
    return AdminCreateContainerResponse(
        **view.model_dump(exclude={"git_fin_status"}),
        service_id=created.service_id,
    )


@router.get(
    "",
    response_model=AdminContainerListResponse,
    responses=api_responses("成功", 200, 502),
)
def list_containers() -> AdminContainerListResponse:
    """获取全部容器，包括业务删除容器。"""
    return AdminContainerListResponse(
        containers=[_container_response(row) for row in container_service.list_admin_containers()]
    )


# 静态 limit 路径必须声明在动态 container_id 路径之前。
@router.get(
    "/limit",
    response_model=ContainerLimitResponse,
    responses=api_responses("成功", 200),
)
def get_container_limit() -> ContainerLimitResponse:
    """获取容器数量及资源限制配置。"""
    return _limit_response(container_service.get_container_limit())


@router.post(
    "/limit",
    response_model=ContainerLimitResponse,
    status_code=200,
    responses=api_responses("成功", 200, 400),
)
def set_container_limit(request: ContainerLimitRequest) -> ContainerLimitResponse:
    """设置容器数量及资源限制配置。"""
    return _limit_response(
        container_service.set_container_limit(
            request.container_limit,
            cpu=request.cpu,
            memory=request.memory,
        )
    )


# 静态孤儿容器路径必须声明在动态 container_id 路径之前。
@router.get(
    "/orphans",
    response_model=OrphanContainerListResponse,
    responses=api_responses("成功", 200, 502),
)
def list_orphan_containers() -> OrphanContainerListResponse:
    """查询未被记录在数据库中的孤儿容器。"""
    return OrphanContainerListResponse(
        container_ids=container_service.list_orphan_container_ids()
    )


@router.post(
    "/orphans/delete",
    status_code=204,
    responses=api_responses("成功 (无内容)", 204, 400, 502),
)
def delete_orphan_containers(request: OrphanContainerDeleteRequest) -> Response:
    """批量删除指定的孤儿容器。"""
    container_service.delete_orphan_containers(request.container_ids)
    return Response(status_code=204)


# 按 K8s Pod 名称删除：Pod 名称会被解析回 OpenSandbox 沙盒 ID，再删除 BatchSandbox，
# 因而不会像直接 `kubectl delete pod` 那样被控制器重建。
@router.post(
    "/k8s/pods/delete",
    status_code=204,
    responses=api_responses("成功 (无内容)", 204, 400, 502),
)
def delete_k8s_pods(request: PodDeleteRequest) -> Response:
    """按 kubectl 查询到的 Pod 名称物理删除指定沙盒容器。"""
    container_service.delete_sandboxes_by_pod_names(request.pod_names)
    return Response(status_code=204)


@router.get(
    "/{container_id}",
    response_model=AdminContainerResponse,
    responses=api_responses("成功", 200, 404, 502),
)
def get_container(container_id: str) -> AdminContainerResponse:
    """查询指定容器运行状态，包括业务删除容器。"""
    return _container_response(container_service.get_admin_container(container_id))


@router.get(
    "/{container_id}/log",
    response_model=None,
    response_class=PlainTextResponse,
    responses=api_responses("成功", 200, 404, 502),
)
def get_container_log(container_id: str) -> PlainTextResponse:
    """获取指定容器最新日志。"""
    return PlainTextResponse(content=container_service.get_container_logs(container_id))


register_container_action_routes(router, operation_id_prefix="admin")


@router.post(
    "/{container_id}/permanent-delete",
    status_code=204,
    responses=api_responses("成功 (无内容)", 204, 404, 502),
)
def permanent_delete(container_id: str) -> Response:
    """物理删除指定容器。"""
    container_service.permanent_delete(container_id)
    return Response(status_code=204)


@router.post(
    "/{container_id}/expiration",
    response_model=ExpirationResponse,
    status_code=200,
    openapi_extra={"requestBody": {"description": "容器过期时间 (小时)。"}},
    responses=api_responses("成功", 200, 400, 404, 502),
)
def set_expiration(container_id: str, request: ExpirationRequest) -> ExpirationResponse:
    """设置指定容器过期时间，0 表示永不过期。"""
    view = container_service.set_expiration(container_id, request.expiration_hours)
    return ExpirationResponse(container_id=view.container_id, expires_at=view.expires_at)


@router.post(
    "/{container_id}/restore",
    status_code=204,
    responses=api_responses("成功 (无内容)", 204, 400, 404, 409, 502),
)
def restore(container_id: str, request: ExpirationRequest) -> Response:
    """恢复指定业务删除容器。"""
    container_service.restore(container_id, request.expiration_hours)
    return Response(status_code=204)


def _container_response(view: container_service.AdminContainerView) -> AdminContainerResponse:
    return AdminContainerResponse(
        container_id=view.container_id,
        type=view.container_type,
        image=view.image,
        user_id=view.user_id,
        gitee_url=view.gitee_url,
        gitee_user=view.gitee_user,
        gitee_repository=view.gitee_repository,
        gitee_branch=view.gitee_branch,
        created_at=view.created_at,
        expiration_hours=view.expiration_hours,
        authorize_general_account=view.authorize_general_account,
        status=view.status.value,
        git_fin_status=view.git_fin_status,
        endpoint=view.endpoint,
        novnc_url=view.novnc_url,
        started_at=view.started_at,
        expires_at=view.expires_at,
        cpu_usage=view.cpu_usage,
        memory_usage=view.memory_usage,
        deleted_at=view.deleted_at,
        business_deleted=view.business_deleted,
    )


def _limit_response(view: container_service.ContainerLimitView) -> ContainerLimitResponse:
    return ContainerLimitResponse(
        container_limit=view.container_limit,
        cpu=view.cpu,
        memory=view.memory,
    )
