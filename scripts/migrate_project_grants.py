"""Import previously approved YAML source bindings into the grant ledger.

Run after upgrade_core5_v1.sql.  The command is dry-run unless --apply is set.
It does not remove the historical YAML or receipt evidence.
"""
from __future__ import annotations

import argparse
import uuid
from pathlib import Path

import psycopg
import yaml
from psycopg.types.json import Jsonb

from assistant_kernel.config import KernelSettings
from assistant_kernel.services.project_grants import policy_sha256


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    settings = KernelSettings.from_environment()
    source = settings.data_root / "config" / "project-scopes.yaml"
    data = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    pending = []
    for project_id, project in ((data.get("projects") or {}).items()):
        for item in project.get("roots") or []:
            if project_id == "system" or item.get("key") == "project":
                continue
            if str(item.get("access") or "read") != "write":
                continue
            policy = dict(item)
            if project_id == "trading-risk-bot" and policy.get("key") == "history_fetch_script":
                # This legacy Binance-only Skill was granted while trying to
                # obtain Alpaca data. The owner chose to narrow crypto grants;
                # data access now uses a separate registered source.
                continue
            if project_id == "trading-risk-bot" and policy.get("key") == "crypto_strategy_reference":
                policy["path"] = "/home/v/AI/crypto/trade8/strategies"
                policy["description"] = "Owner-narrowed crypto strategy code working directory"
            if (
                (project_id == "kernel" and policy.get("key") == "kernel-source")
                or (project_id == "butler-maintenance" and policy.get("key") == "butler-target")
            ):
                policy["write_mode"] = "gated"
            else:
                policy["write_mode"] = "direct"
            policy["publish"] = policy["write_mode"] == "gated"
            if policy["write_mode"] == "gated":
                policy["native_mode"] = "mcp_only"
            else:
                policy["native_mode"] = "enforced"
            pending.append((str(project_id), str(policy["key"]), policy))
    print(f"PROJECT_GRANTS_IMPORT_SCAN count={len(pending)} apply={args.apply}")
    for project_id, key, policy in pending:
        print(
            f"  {project_id}/{key}: write_mode={policy['write_mode']} "
            f"native_mode={policy['native_mode']}"
        )
    if not args.apply:
        return
    with psycopg.connect(
        host=settings.postgres_host, port=settings.postgres_port,
        dbname=settings.postgres_database, user=settings.postgres_user,
        password=settings.postgres_password.get_secret_value(),
    ) as connection:
        version = connection.execute(
            "SELECT value #>> '{}' FROM assistant.kernel_meta WHERE key='schema_version'"
        ).fetchone()[0]
        if version not in {"core5.v1", "core5.v2", "core5.v3", "core5.v4", "core5.v5", "core5.v6"}:
            raise RuntimeError(f"grant import requires core5.v1+, got {version}")
        imported = 0
        for project_id, key, policy in pending:
            digest = policy_sha256(policy)
            existing = connection.execute(
                """
                SELECT policy_sha256 FROM assistant.project_grants
                WHERE project_id=%s AND grant_kind='file' AND resource_key=%s
                  AND subject_kind='project' AND status='active'
                """,
                (project_id, key),
            ).fetchone()
            if existing:
                if existing[0] != digest:
                    raise RuntimeError(f"active grant differs from legacy scope: {project_id}/{key}")
                continue
            connection.execute(
                """
                INSERT INTO assistant.project_grants(
                    grant_id,project_id,grant_kind,resource_key,subject_kind,
                    policy_json,policy_sha256
                ) VALUES (%s,%s,'file',%s,'project',%s,%s)
                """,
                (str(uuid.uuid4()), project_id, key, Jsonb(policy), digest),
            )
            imported += 1
        connection.commit()
    print(f"PROJECT_GRANTS_IMPORT_OK imported={imported} total={len(pending)}")


if __name__ == "__main__":
    main()
