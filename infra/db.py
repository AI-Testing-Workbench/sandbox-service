"""
SQLAlchemy async engine / session factory and migration entry point (v4 §6.3).

- SQLite file path = `Constants.DB_PATH` (v4 §5.2).
- `session_scope` commits on success, rolls back on error, and always closes.
- Alembic runs on a separate synchronous engine in a worker thread.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from config import Constants

__all__ = [
    "get_schema_version",
    "init_db",
    "dispose_db",
    "session_scope",
]

_db_path = Constants.DB_PATH.value

if _db_path != ":memory:":
    Path(_db_path).parent.mkdir(parents=True, exist_ok=True)
    _async_database_url = f"sqlite+aiosqlite:///{_db_path}"
    _migration_database_url = f"sqlite:///{_db_path}"
else:
    # Both drivers must address the same in-memory database during migrations.
    _memory_database = (
        f"file:testagent_cloud_{uuid4().hex}?mode=memory&cache=shared&uri=true"
    )
    _async_database_url = f"sqlite+aiosqlite:///{_memory_database}"
    _migration_database_url = f"sqlite:///{_memory_database}"

engine = create_async_engine(
    _async_database_url,
    connect_args={"check_same_thread": False},
)
migration_engine = create_engine(
    _migration_database_url,
    connect_args={"check_same_thread": False},
)

SessionFactory = async_sessionmaker(
    bind=engine,
    autoflush=False,
    expire_on_commit=False,
)


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Open a transactional AsyncSession; commit, rollback, and close it."""
    async with SessionFactory() as session:
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise


async def init_db() -> None:
    """Upgrade the schema and initialize Git credential crypto; safe to repeat."""
    from infra.git_crypto import initialize_git_credential_crypto
    from infra.migrations import upgrade_database

    if _db_path == ":memory:":
        # Keep the shared in-memory database alive while the sync migration engine opens it.
        async with engine.connect():
            pass
    await asyncio.to_thread(upgrade_database, migration_engine)
    async with session_scope() as session:
        await initialize_git_credential_crypto(session)


async def get_schema_version() -> int:
    """Read the integer schema version using the async runtime engine."""
    from infra.migrations import get_schema_version as _get_schema_version

    return await _get_schema_version(engine)


async def dispose_db() -> None:
    """Dispose the async connection pool during application shutdown."""
    await engine.dispose()
    await asyncio.to_thread(migration_engine.dispose)
