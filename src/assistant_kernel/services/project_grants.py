"""Durable, Project-owned authorization facts.

The database is authoritative.  ``project-scopes.yaml`` remains a discovery and
legacy projection, not a second active-grant ledger.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

import psycopg

from assistant_kernel.config import KernelSettings


def policy_sha256(policy: dict[str, Any]) -> str:
    encoded = json.dumps(
        policy, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class ProjectGrantService:
    def __init__(self, settings: KernelSettings):
        self.settings = settings

    def active_file_roots(self, project_id: str, run_id: str | None = None) -> list[dict[str, Any]]:
        with psycopg.connect(
            host=self.settings.postgres_host,
            port=self.settings.postgres_port,
            dbname=self.settings.postgres_database,
            user=self.settings.postgres_user,
            password=self.settings.postgres_password.get_secret_value(),
        ) as connection:
            if connection.execute(
                "SELECT to_regclass('assistant.project_grants')"
            ).fetchone()[0] is None:
                # Fail closed during a rolling migration or an isolated
                # filesystem-only test: no table means no external grants.
                return []
            cutoff = None
            if run_id:
                try:
                    uuid.UUID(run_id)
                except ValueError:
                    pass  # isolated tests and administrative inspection
                else:
                    row = connection.execute(
                        "SELECT created_at FROM assistant.runs WHERE run_id=%s AND project_id=%s",
                        (run_id, project_id),
                    ).fetchone()
                    if row is None:
                        raise ValueError("Run identity was not found while resolving grants")
                    cutoff = row[0]
            rows = connection.execute(
                """
                SELECT resource_key,policy_json,policy_sha256,grant_id
                FROM assistant.project_grants
                WHERE project_id=%s AND grant_kind='file'
                  AND subject_kind='project' AND status='active'
                  AND (expires_at IS NULL OR expires_at>now())
                  AND (%s::timestamptz IS NULL OR activated_at<=%s::timestamptz)
                ORDER BY resource_key
                """,
                (project_id, cutoff, cutoff),
            ).fetchall()
        roots: list[dict[str, Any]] = []
        for resource_key, raw_policy, expected_hash, grant_id in rows:
            policy = dict(raw_policy or {})
            if policy_sha256(policy) != expected_hash:
                raise ValueError(f"Project grant policy hash differs: {grant_id}")
            if str(policy.get("key") or "") != str(resource_key):
                raise ValueError(f"Project grant root key differs: {grant_id}")
            roots.append({**policy, "grant_id": str(grant_id), "grant_sha256": expected_hash})
        return roots
