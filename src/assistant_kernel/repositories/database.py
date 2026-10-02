from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

import psycopg
from psycopg.rows import dict_row

from assistant_kernel.config.settings import KernelSettings


class AssistantDatabase:
    """Connection factory only; business persistence belongs to CoreRepository."""

    def __init__(self, settings: KernelSettings):
        self.settings = settings

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[psycopg.AsyncConnection]:
        connection = await psycopg.AsyncConnection.connect(
            host=self.settings.postgres_host,
            port=self.settings.postgres_port,
            dbname=self.settings.postgres_database,
            user=self.settings.postgres_user,
            password=self.settings.postgres_password.get_secret_value(),
            row_factory=dict_row,
            autocommit=False,
        )
        try:
            yield connection
        finally:
            await connection.close()

    async def ping(self) -> bool:
        try:
            async with self.connection() as connection:
                await connection.execute("SELECT 1")
            return True
        except Exception:
            return False
