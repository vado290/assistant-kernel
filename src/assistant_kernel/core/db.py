"""Connections, row locks and the append-only event log for the core schema."""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from assistant_kernel.config import KernelSettings


class Database:
    def __init__(self, settings: KernelSettings):
        self.settings = settings

    @asynccontextmanager
    async def tx(self) -> AsyncIterator[psycopg.AsyncConnection]:
        """One transaction: committed on normal exit, rolled back on any exception."""
        s = self.settings
        connection = await psycopg.AsyncConnection.connect(
            host=s.postgres_host, port=s.postgres_port, dbname=s.postgres_database,
            user=s.postgres_user, password=s.postgres_password.get_secret_value(),
            row_factory=dict_row, autocommit=True, connect_timeout=10)
        try:
            async with connection.transaction():
                yield connection
        finally:
            await connection.close()


async def one(c, query: str, params: tuple = ()) -> dict[str, Any] | None:
    return await (await c.execute(query, params)).fetchone()


async def all_rows(c, query: str, params: tuple = ()) -> list[dict[str, Any]]:
    return list(await (await c.execute(query, params)).fetchall())


async def lock_project(c, project_id: str) -> None:
    """Serialise every task/card change of one project. Lock order: project → run → task → card."""
    if await one(c, "SELECT 1 FROM core.projects WHERE project_id=%s FOR UPDATE", (project_id,)) is None:
        raise KeyError(f"unknown project {project_id}")


async def event(c, project_id: str, event_kind: str, *, task_id=None, run_id=None,
                interaction_id=None, **payload: Any) -> None:
    await c.execute(
        """INSERT INTO core.task_events(project_id, task_id, run_id, interaction_id, kind, payload)
           VALUES (%s, %s, %s, %s, %s, %s)""",
        (project_id, task_id, run_id, interaction_id, event_kind, Jsonb(payload, dumps=_dumps)))


def _dumps(value: Any) -> str:
    import json
    return json.dumps(value, default=str, ensure_ascii=False)
