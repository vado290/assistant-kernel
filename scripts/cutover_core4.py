"""Replace the legacy Assistant schema with the four-table first-release core.

The cutover is intentionally destructive and therefore requires both ``--apply``
and the path of a completed external backup containing ``postgres.dump``.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import psycopg

from assistant_kernel.config import KernelSettings


EXPECTED_SCHEMA_VERSION = "core4.v5"
NONTERMINAL_RUN_STATUSES = (
    "queued", "running", "waiting_user", "blocked", "reconciling",
)


def relation_exists(connection: psycopg.Connection, name: str) -> bool:
    return connection.execute("SELECT to_regclass(%s) IS NOT NULL", (name,)).fetchone()[0]


def active_count(connection: psycopg.Connection, table: str, statuses: tuple[str, ...]) -> int:
    if not relation_exists(connection, f"assistant.{table}"):
        return 0
    placeholders = ",".join("%s" for _ in statuses)
    return int(
        connection.execute(
            f"SELECT count(*) FROM assistant.{table} WHERE status IN ({placeholders})",
            statuses,
        ).fetchone()[0]
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup", type=Path, required=True)
    args = parser.parse_args()
    backup = args.backup.resolve()
    if not (backup / "postgres.dump").is_file():
        raise RuntimeError("backup must contain postgres.dump")
    if not (backup / "sha256-manifest.json").is_file():
        raise RuntimeError("backup must contain sha256-manifest.json")
    if not args.apply:
        print("DRY_RUN_OK; add --apply to replace the assistant schema")
        return

    settings = KernelSettings.from_environment()
    connection_args = {
        "host": settings.postgres_host,
        "port": settings.postgres_port,
        "dbname": settings.postgres_database,
        "user": settings.postgres_user,
        "password": settings.postgres_password.get_secret_value(),
    }
    schema_path = Path(__file__).parents[1] / "migrations" / "schema_core4.sql"
    schema = schema_path.read_text(encoding="utf-8").strip()
    if not schema.startswith("BEGIN;") or not schema.endswith("COMMIT;"):
        raise RuntimeError("schema_core4.sql must be one explicit transaction")
    schema_body = schema.removeprefix("BEGIN;").removesuffix("COMMIT;").strip()

    with psycopg.connect(**connection_args, autocommit=True) as connection:
        active = {
            "control_jobs": active_count(connection, "control_jobs", ("queued", "running")),
            "tasks": active_count(connection, "tasks", ("queued", "running", "waiting_user")),
            "approval_requests": active_count(connection, "approval_requests", ("pending",)),
            "runs": active_count(connection, "runs", NONTERMINAL_RUN_STATUSES),
        }
        if any(active.values()):
            raise RuntimeError(f"refusing cutover while durable work is active: {active}")
        connection.execute(
            "BEGIN; DROP SCHEMA IF EXISTS assistant CASCADE; " + schema_body + "; COMMIT;"
        )
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname='assistant' ORDER BY tablename"
            ).fetchall()
        ]
        version = connection.execute(
            "SELECT value #>> '{}' FROM assistant.kernel_meta WHERE key='schema_version'"
        ).fetchone()[0]
    expected = ["kernel_meta", "messages", "runs", "sessions"]
    if tables != expected or version != EXPECTED_SCHEMA_VERSION:
        raise RuntimeError(f"post-cutover verification failed: tables={tables}, version={version}")
    print(f"CORE4_CUTOVER_OK; backup={backup}; tables={','.join(tables)}; version={version}")


if __name__ == "__main__":
    main()
