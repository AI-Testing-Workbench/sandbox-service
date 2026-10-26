"""
Resolve inherited creation fields from an existing service record.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, cast

from domain.errors import BusinessConflictError, ContainerNotFoundError
from domain.models import ContainerType
from infra.db import session_scope
from infra.orm import Container as ContainerRow
from infra.repositories import ContainerRepository


__all__ = ["ResolvedContainerSource", "resolve_container_source"]

# A source value flows only when both its request field and mapped DB column are listed.
_SOURCE_MATCH_FIELDS = (
    "gitee_user",
    "gitee_repository",
    "gitee_url",
    "gitee_branch",
    "authorize_general_account",
    "type",
)
_SOURCE_OVERRIDE_FIELDS = (
    "expiration_hours",
)

_SOURCE_MATCH_DATABASE_FIELDS = (
    "gitee_user",
    "gitee_repository",
    "gitee_url",
    "gitee_branch",
    "authorize_general_account",
    "container_type",
)
_SOURCE_OVERRIDE_DATABASE_FIELDS = (
    "expiration_hours",
)

_REQUEST_TO_DATABASE_FIELD = {
    "gitee_user": "gitee_user",
    "gitee_repository": "gitee_repository",
    "gitee_url": "gitee_url",
    "gitee_branch": "gitee_branch",
    "authorize_general_account": "authorize_general_account",
    "type": "container_type",
    "expiration_hours": "expiration_hours",
}


def _validate_source_field_configuration() -> None:
    policies = (
        (
            "匹配继承",
            _SOURCE_MATCH_FIELDS,
            _SOURCE_MATCH_DATABASE_FIELDS,
        ),
        (
            "显式覆盖",
            _SOURCE_OVERRIDE_FIELDS,
            _SOURCE_OVERRIDE_DATABASE_FIELDS,
        ),
    )
    mapped_request_fields: set[str] = set()
    orm_columns = set(ContainerRow.__table__.columns.keys())

    for policy_name, request_fields, database_fields in policies:
        if len(request_fields) != len(set(request_fields)):
            raise RuntimeError(f"来源字段配置错误： {policy_name} 请求元组存在重复字段")
        if len(database_fields) != len(set(database_fields)):
            raise RuntimeError(f"来源字段配置错误： {policy_name} 数据库元组存在重复字段")

        missing_request_mappings = set(request_fields) - set(_REQUEST_TO_DATABASE_FIELD)
        if missing_request_mappings:
            raise RuntimeError(
                f"来源字段配置错误： {policy_name} 请求字段未配置数据库映射："
                f"{sorted(missing_request_mappings)}"
            )

        mapped_fields = tuple(
            _REQUEST_TO_DATABASE_FIELD[request_field]
            for request_field in request_fields
        )
        if len(mapped_fields) != len(set(mapped_fields)):
            raise RuntimeError(f"来源字段配置错误： {policy_name} 请求字段映射重复数据库列")
        if set(mapped_fields) != set(database_fields):
            missing_database_fields = set(mapped_fields) - set(database_fields)
            missing_request_fields = set(database_fields) - set(mapped_fields)
            raise RuntimeError(
                f"来源字段配置错误： {policy_name} 请求/数据库元组不匹配；"
                f"缺少数据库列={sorted(missing_database_fields)}，"
                f"缺少请求字段={sorted(missing_request_fields)}"
            )

        missing_orm_columns = set(database_fields) - orm_columns
        if missing_orm_columns:
            raise RuntimeError(
                f"来源字段配置错误： {policy_name} 数据库列不存在于 ORM："
                f"{sorted(missing_orm_columns)}"
            )

        mapped_request_fields.update(request_fields)

    if mapped_request_fields != set(_REQUEST_TO_DATABASE_FIELD):
        raise RuntimeError(
            "来源字段配置错误：请求到数据库映射包含未列入策略元组的字段："
            f"{sorted(set(_REQUEST_TO_DATABASE_FIELD) - mapped_request_fields)}"
        )

    if set(_SOURCE_MATCH_FIELDS).intersection(_SOURCE_OVERRIDE_FIELDS) or set(
            _SOURCE_MATCH_DATABASE_FIELDS
    ).intersection(_SOURCE_OVERRIDE_DATABASE_FIELDS):
        raise RuntimeError("来源字段配置错误：同一字段不能同时匹配继承和显式覆盖")


# Fail fast during service imports instead of silently skipping mismatched fields.
_validate_source_field_configuration()


@dataclass(frozen=True)
class ResolvedContainerSource:
    container_type: ContainerType
    gitee_user: Optional[str]
    gitee_repository: Optional[str]
    gitee_branch: Optional[str]
    gitee_url: Optional[str]
    authorize_general_account: Optional[bool]
    expiration_hours: Optional[int]


async def resolve_container_source(
        service_id: str,
        *,
        user_id: str,
        gitee_user: Optional[str],
        gitee_repository: Optional[str],
        gitee_branch: Optional[str],
        gitee_url: Optional[str],
        authorize_general_account: Optional[bool],
        expiration_hours: Optional[int],
        container_type: ContainerType,
        explicit_fields: frozenset[str],
) -> ResolvedContainerSource:
    """Load a source row and apply clone-specific inheritance and equality rules."""
    async with session_scope() as session:
        source = await ContainerRepository(session).get_by_service_id(service_id)
        if source is None or source.user_id != user_id:
            # Treat ownership mismatch like an unknown source to avoid disclosing
            # whether another user's service ID exists.
            raise ContainerNotFoundError("此用户未拥有这个服务 ID 对应的沙箱")

        requested_values: dict[str, object] = {
            "gitee_user": gitee_user,
            "gitee_repository": gitee_repository,
            "gitee_url": gitee_url,
            "gitee_branch": gitee_branch,
            "authorize_general_account": authorize_general_account,
            "type": container_type,
            "expiration_hours": expiration_hours,
        }
        resolved_database_values: dict[str, object] = {
            database_field: requested_values[request_field]
            for request_field, database_field in _REQUEST_TO_DATABASE_FIELD.items()
        }
        match_fields = tuple(
            (request_field, _REQUEST_TO_DATABASE_FIELD[request_field])
            for request_field in _SOURCE_MATCH_FIELDS
            if _REQUEST_TO_DATABASE_FIELD[request_field] in _SOURCE_MATCH_DATABASE_FIELDS
        )
        override_fields = tuple(
            (request_field, _REQUEST_TO_DATABASE_FIELD[request_field])
            for request_field in _SOURCE_OVERRIDE_FIELDS
            if _REQUEST_TO_DATABASE_FIELD[request_field] in _SOURCE_OVERRIDE_DATABASE_FIELDS
        )

        source_database_values: dict[str, object] = {}
        for _, database_field in (*match_fields, *override_fields):
            if database_field in source_database_values:
                continue
            source_value = getattr(source, database_field)
            if database_field in ("gitee_user", "gitee_repository", "gitee_url"):
                source_value = source_value or ""
            elif database_field == "container_type":
                try:
                    source_value = ContainerType(source_value)
                except ValueError:
                    raise BusinessConflictError("来源云端沙箱配置无效") from None
            source_database_values[database_field] = source_value

        for request_field, database_field in match_fields:
            requested = requested_values[request_field]
            inherited = source_database_values[database_field]
            if request_field not in explicit_fields or _is_empty(requested):
                resolved_database_values[database_field] = inherited
            elif requested != inherited:
                _raise_source_field_conflict(request_field)
            else:
                resolved_database_values[database_field] = inherited

        for request_field, database_field in override_fields:
            requested = requested_values[request_field]
            resolved_database_values[database_field] = (
                requested
                if request_field in explicit_fields and requested is not None
                else source_database_values[database_field]
            )

        return ResolvedContainerSource(
            container_type=cast(ContainerType, resolved_database_values["container_type"]),
            gitee_user=cast(Optional[str], resolved_database_values["gitee_user"]),
            gitee_repository=cast(Optional[str], resolved_database_values["gitee_repository"]),
            gitee_branch=cast(Optional[str], resolved_database_values["gitee_branch"]),
            gitee_url=cast(Optional[str], resolved_database_values["gitee_url"]),
            authorize_general_account=cast(
                Optional[bool],
                resolved_database_values["authorize_general_account"],
            ),
            expiration_hours=cast(Optional[int], resolved_database_values["expiration_hours"]),
        )


def _is_empty(value: object) -> bool:
    return value is None or isinstance(value, str) and not value.strip()


def _raise_source_field_conflict(field: str) -> None:
    if field == "type":
        raise BusinessConflictError("请求的云端沙箱类型与来源不一致")
    if field == "authorize_general_account":
        raise BusinessConflictError("请求的授权配置与来源不一致")
    raise BusinessConflictError("请求的码云配置与来源不一致")
