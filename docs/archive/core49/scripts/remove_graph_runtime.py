"""Quiesced cutover to Python stages. Requires a recoverable pre-cutover DB dump."""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from assistant_kernel.config import KernelSettings


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply",action="store_true")
    parser.add_argument("--backup-file",type=Path)
    args=parser.parse_args()
    settings=KernelSettings.from_environment()
    with psycopg.connect(host=settings.postgres_host,port=settings.postgres_port,dbname=settings.postgres_database,
        user=settings.postgres_user,password=settings.postgres_password.get_secret_value(),autocommit=True,row_factory=dict_row) as c:
        if c.execute("SELECT 1 FROM assistant.schema_migrations WHERE version='0006'").fetchone():
            print("STAGE_MIGRATION_ALREADY_APPLIED")
            return
        active=c.execute("SELECT count(*) n FROM assistant.control_jobs WHERE status IN ('queued','running')").fetchone()["n"]
        python_active=c.execute("SELECT count(*) n FROM assistant.python_executions WHERE status='running'").fetchone()["n"]
        print(f"active_control_jobs={active}; running_python_executions={python_active}")
        # Removing the core dependency must not strand an active registered script.
        versions=c.execute("SELECT project_id,definition_path FROM assistant.workflow_versions WHERE status='active' AND spec_json->'executor'->>'type'='python_script'").fetchall()
        for version in versions:
            prefix=f"vault://{version['project_id']}/"
            if not version["definition_path"].startswith(prefix):
                raise SystemExit("Unexpected active script source URI; manual dependency audit required")
            root=(settings.data_root/"vaults"/version["project_id"]).resolve()
            source=(root/version["definition_path"][len(prefix):]).resolve()
            if root not in source.parents or not source.is_file():
                raise SystemExit("Active script is missing or outside Vault; investigate before cutover")
            if "langgraph" in source.read_text(encoding="utf-8").lower():
                raise SystemExit("Active registered script references retired dependency; migrate it first")
        if not args.apply:
            print("DRY_RUN_OK; no changes made")
            return
        if active or python_active:
            raise SystemExit("Refusing cutover while work is queued/running")
        if not args.backup_file:
            raise SystemExit("--backup-file is required")
        backup=args.backup_file.resolve()
        if (settings.data_root/"backups").resolve() not in backup.parents:
            raise SystemExit("Backup must be in the configured backups directory")
        content=backup.read_bytes()
        if not content.startswith(b"PGDMP"):
            raise SystemExit("Expected PostgreSQL custom-format backup")
        print("BACKUP_SHA256="+hashlib.sha256(content).hexdigest())
        before=c.execute("SELECT checkpoint_id,state_json,content_sha256 FROM assistant.structured_checkpoints ORDER BY checkpoint_id").fetchall()
        migration=Path(__file__).parents[1]/"migrations"/"0006_python_stages.sql"
        c.execute(migration.read_text())
        after=c.execute("SELECT checkpoint_id,state_json,content_sha256 FROM assistant.structured_checkpoints ORDER BY checkpoint_id").fetchall()
        assert before==after,"Business/session checkpoints changed unexpectedly"
        print(f"PYTHON_STAGE_CUTOVER_OK; preserved_business_checkpoints={len(after)}")


if __name__=="__main__":
    main()
