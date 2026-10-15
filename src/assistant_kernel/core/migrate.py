"""Ordered, run-once SQL migrations shipped inside the package.

Files in ``core/sql`` are applied in name order, each in its own transaction,
and recorded in ``assistant.schema_migrations``. An applied file must never be
edited; add a new file instead.
"""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
from typing import Any

import psycopg

from assistant_kernel.config import KernelSettings

SQL_DIR = Path(__file__).with_name("sql")
LEDGER_SQL = """
CREATE SCHEMA IF NOT EXISTS assistant;
CREATE TABLE IF NOT EXISTS assistant.schema_migrations (
    name text PRIMARY KEY,
    sha256 text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now()
);
"""


def connection_args(settings: KernelSettings) -> dict[str, Any]:
    return {
        "host": settings.postgres_host,
        "port": settings.postgres_port,
        "dbname": settings.postgres_database,
        "user": settings.postgres_user,
        "password": settings.postgres_password.get_secret_value(),
        "connect_timeout": 10,
    }


def apply(args: dict[str, Any], directory: Path = SQL_DIR, *, dry_run: bool = False) -> list[str]:
    """Apply pending migrations; return the names applied (or pending, for a dry run)."""
    with psycopg.connect(**args) as connection:
        connection.execute(LEDGER_SQL)
        connection.commit()
        applied = dict(connection.execute(
            "SELECT name, sha256 FROM assistant.schema_migrations").fetchall())
        names = []
        for path in sorted(directory.glob("*.sql")):
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if path.name in applied:
                if applied[path.name] != digest:
                    raise RuntimeError(f"applied migration {path.name} was edited")
                continue
            names.append(path.name)
            if dry_run:
                continue
            with connection.transaction():
                connection.execute(path.read_text(encoding="utf-8"))
                connection.execute(
                    "INSERT INTO assistant.schema_migrations(name, sha256) VALUES (%s, %s)",
                    (path.name, digest))
        return names


def run() -> None:
    parser = argparse.ArgumentParser(description="Apply Kernel core SQL migrations")
    parser.add_argument("--dry-run", action="store_true")
    options = parser.parse_args()
    names = apply(connection_args(KernelSettings.from_environment()), dry_run=options.dry_run)
    verb = "pending" if options.dry_run else "applied"
    print(f"MIGRATIONS_{verb.upper()}={','.join(names) or '-'}")


if __name__ == "__main__":
    run()
