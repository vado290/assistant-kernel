"""Self-checks for the phase-0 test harness and migration runner."""
from __future__ import annotations

import psycopg
import pytest

from assistant_kernel.core import migrate
from assistant_kernel.harness.base import AgentTurnRequest


def test_temporary_database_has_migration_ledger(kernel_settings):
    with psycopg.connect(**migrate.connection_args(kernel_settings)) as connection:
        row = connection.execute("SELECT to_regclass('assistant.schema_migrations')").fetchone()
    assert row[0] == "assistant.schema_migrations"


def test_migrations_apply_once_in_order(kernel_settings, tmp_path):
    directory = tmp_path / "sql"
    directory.mkdir()
    (directory / "0002_b.sql").write_text("INSERT INTO assistant.probe VALUES ('b');", encoding="utf-8")
    (directory / "0001_a.sql").write_text(
        "CREATE TABLE assistant.probe(value text); INSERT INTO assistant.probe VALUES ('a');", encoding="utf-8")
    args = migrate.connection_args(kernel_settings)

    assert migrate.apply(args, directory, dry_run=True) == ["0001_a.sql", "0002_b.sql"]
    assert migrate.apply(args, directory) == ["0001_a.sql", "0002_b.sql"]
    assert migrate.apply(args, directory) == []
    with psycopg.connect(**args) as connection:
        assert [r[0] for r in connection.execute("SELECT value FROM assistant.probe ORDER BY value")] == ["a", "b"]

    (directory / "0001_a.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(RuntimeError, match="was edited"):
        migrate.apply(args, directory)


def test_failed_migration_leaves_no_partial_record(kernel_settings, tmp_path):
    directory = tmp_path / "sql"
    directory.mkdir()
    (directory / "0001_bad.sql").write_text(
        "CREATE TABLE assistant.half(value text); SELECT no_such_function();", encoding="utf-8")
    args = migrate.connection_args(kernel_settings)
    with pytest.raises(psycopg.Error):
        migrate.apply(args, directory)
    with psycopg.connect(**args) as connection:
        assert connection.execute("SELECT to_regclass('assistant.half')").fetchone()[0] is None
        assert connection.execute("SELECT count(*) FROM assistant.schema_migrations").fetchone()[0] == 0


async def test_fake_harness_replays_scripts_in_order(fake_harness, tmp_path):
    fake_harness.script("pm:requirements", {"outcome": "needs_input"}, "plain text")

    def request():
        return AgentTurnRequest(project_id="demo", prompt="p", cwd=tmp_path,
                                scope_context={"script_key": "pm:requirements"})

    first = await fake_harness.run_turn(request())
    second = await fake_harness.run_turn(request())
    assert first.final_text == '{"outcome": "needs_input"}'
    assert second.final_text == "plain text"
    with pytest.raises(AssertionError, match="no scripted model output"):
        await fake_harness.run_turn(request())
    assert len(fake_harness.calls) == 3
