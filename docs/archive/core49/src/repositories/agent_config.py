from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from psycopg.types.json import Jsonb

from assistant_kernel.repositories.database import AssistantDatabase


def _sha256_json(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class AgentConfigRepository:
    def __init__(self, database: AssistantDatabase):
        self.database = database

    async def ensure_scope_version(
        self, *, agent_id: str, policy: dict[str, Any], accepted_by: str = "bootstrap"
    ) -> dict[str, Any]:
        digest = _sha256_json(policy)
        async with self.database.connection() as connection:
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (f"agent-scope:{agent_id}",),
            )
            existing = await (
                await connection.execute(
                    "SELECT * FROM assistant.agent_scope_versions "
                    "WHERE agent_id=%s AND policy_sha256=%s",
                    (agent_id, digest),
                )
            ).fetchone()
            if existing is not None:
                await connection.commit()
                return existing
            current = await (
                await connection.execute(
                    "SELECT * FROM assistant.agent_scope_versions "
                    "WHERE agent_id=%s AND status='active' FOR UPDATE",
                    (agent_id,),
                )
            ).fetchone()
            if current is not None:
                await connection.commit()
                return current
            row = await (
                await connection.execute(
                    """
                    INSERT INTO assistant.agent_scope_versions(
                        agent_scope_version_id,agent_id,version,policy,policy_sha256,
                        status,change_reason,accepted_by,activated_at
                    ) VALUES (%s,%s,1,%s,%s,'active','bootstrap',%s,now())
                    RETURNING *
                    """,
                    (str(uuid.uuid4()), agent_id, Jsonb(policy), digest, accepted_by),
                )
            ).fetchone()
            await connection.execute(
                "UPDATE assistant.agents SET active_scope_version_id=%s,updated_at=now() "
                "WHERE agent_id=%s",
                (row["agent_scope_version_id"], agent_id),
            )
            await connection.commit()
            return row

    async def ensure_config_version(
        self,
        *,
        agent_id: str,
        scope_version_id: str,
        config: dict[str, Any],
        config_uri: str,
        accepted_by: str = "bootstrap",
    ) -> dict[str, Any]:
        digest = _sha256_json(config)
        async with self.database.connection() as connection:
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (f"agent-config:{agent_id}",),
            )
            existing = await (
                await connection.execute(
                    "SELECT * FROM assistant.agent_config_versions "
                    "WHERE agent_id=%s AND config_sha256=%s",
                    (agent_id, digest),
                )
            ).fetchone()
            if existing is not None:
                await connection.commit()
                return existing
            current = await (
                await connection.execute(
                    "SELECT * FROM assistant.agent_config_versions "
                    "WHERE agent_id=%s AND status='active' FOR UPDATE",
                    (agent_id,),
                )
            ).fetchone()
            if current is not None:
                raise ValueError(
                    "agent-config.yaml differs from the active version; publish a reviewed config version"
                )
            row = await (
                await connection.execute(
                    """
                    INSERT INTO assistant.agent_config_versions(
                        agent_config_version_id,agent_id,version,agent_scope_version_id,
                        config_json,config_uri,config_sha256,status,change_reason,
                        accepted_by,accepted_at,activated_at
                    ) VALUES (%s,%s,1,%s,%s,%s,%s,'active','bootstrap',%s,now(),now())
                    RETURNING *
                    """,
                    (
                        str(uuid.uuid4()), agent_id, scope_version_id, Jsonb(config),
                        config_uri, digest, accepted_by,
                    ),
                )
            ).fetchone()
            await connection.execute(
                "UPDATE assistant.agents SET active_agent_config_version_id=%s,updated_at=now() "
                "WHERE agent_id=%s",
                (row["agent_config_version_id"], agent_id),
            )
            await connection.commit()
            return row

    async def get_active(self, agent_id: str) -> dict[str, Any] | None:
        async with self.database.connection() as connection:
            return await (
                await connection.execute(
                    """
                    SELECT c.*,s.agent_scope_version_id AS effective_scope_version_id,
                           s.policy,s.policy_sha256
                    FROM assistant.agent_config_versions c
                    JOIN assistant.agents a ON a.agent_id=c.agent_id
                    JOIN assistant.agent_scope_versions s
                      ON s.agent_scope_version_id=a.active_scope_version_id
                    WHERE c.agent_id=%s AND c.status='active'
                    ORDER BY c.version DESC LIMIT 1
                    """,
                    (agent_id,),
                )
            ).fetchone()

    async def publish_scope_version(
        self, *, agent_id: str, policy: dict[str, Any], change_reason: str, accepted_by: str
    ) -> dict[str, Any]:
        digest = _sha256_json(policy)
        async with self.database.connection() as connection:
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (f"agent-scope:{agent_id}",),
            )
            current = await (
                await connection.execute(
                    "SELECT * FROM assistant.agent_scope_versions "
                    "WHERE agent_id=%s AND status='active' FOR UPDATE",
                    (agent_id,),
                )
            ).fetchone()
            if current is None:
                raise ValueError("active Agent scope not found")
            if current["policy_sha256"] == digest:
                await connection.commit()
                return current
            await connection.execute(
                "UPDATE assistant.agent_scope_versions SET status='retired',retired_at=now() "
                "WHERE agent_scope_version_id=%s",
                (current["agent_scope_version_id"],),
            )
            row = await (
                await connection.execute(
                    """
                    INSERT INTO assistant.agent_scope_versions(
                        agent_scope_version_id,agent_id,version,parent_scope_version_id,
                        policy,policy_sha256,status,change_reason,accepted_by,activated_at
                    ) VALUES (%s,%s,%s,%s,%s,%s,'active',%s,%s,now())
                    RETURNING *
                    """,
                    (
                        str(uuid.uuid4()), agent_id, int(current["version"]) + 1,
                        current["agent_scope_version_id"], Jsonb(policy), digest,
                        change_reason, accepted_by,
                    ),
                )
            ).fetchone()
            await connection.execute(
                "UPDATE assistant.agents SET active_scope_version_id=%s,updated_at=now() "
                "WHERE agent_id=%s",
                (row["agent_scope_version_id"], agent_id),
            )
            await connection.commit()
            return row

    async def ensure_tool_definition(
        self,
        *,
        tool_key: str,
        description: str,
        read_only: bool,
        risk_class: str,
        code_sha256: str,
    ) -> dict[str, Any]:
        config = {
            "tool_key": tool_key,
            "description": description,
            "read_only": read_only,
            "risk_class": risk_class,
            "code_sha256": code_sha256,
        }
        digest = _sha256_json(config)
        release = f"builtin-{code_sha256[:12]}"
        async with self.database.connection() as connection:
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (f"mcp-tool:{tool_key}",),
            )
            current = await (
                await connection.execute(
                    "SELECT * FROM assistant.mcp_tool_definitions "
                    "WHERE tool_key=%s AND status='active'",
                    (tool_key,),
                )
            ).fetchone()
            if current is not None and current["config_sha256"] == digest:
                await connection.commit()
                return current
            version = int(current["version"]) + 1 if current is not None else 1
            if current is not None:
                await connection.execute(
                    "UPDATE assistant.mcp_tool_definitions SET status='retired' "
                    "WHERE tool_definition_id=%s",
                    (current["tool_definition_id"],),
                )
            row = await (
                await connection.execute(
                    """
                    INSERT INTO assistant.mcp_tool_definitions(
                        tool_definition_id,tool_key,server_key,native_tool_name,version,
                        description,read_only,risk_class,tool_release_id,code_sha256,
                        config_uri,config_sha256,status
                    ) VALUES (%s,%s,'kernel_agent',%s,%s,%s,%s,%s,%s,%s,
                              %s,%s,'active')
                    RETURNING *
                    """,
                    (
                        str(uuid.uuid4()), tool_key, tool_key, version, description, read_only,
                        risk_class, release, code_sha256,
                        f"builtin://kernel-agent-mcp/{release}", digest,
                    ),
                )
            ).fetchone()
            await connection.commit()
            return row

    async def record_invocation_tools(
        self,
        *,
        invocation_id: str,
        bindings: list[dict[str, Any]],
        effective_scope_sha256: str,
    ) -> None:
        async with self.database.connection() as connection:
            for binding in bindings:
                await connection.execute(
                    """
                    INSERT INTO assistant.invocation_tool_bindings(
                        invocation_id,tool_definition_id,binding_mode,
                        effective_default_parameters,effective_scope_sha256,selection_source
                    ) VALUES (%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(invocation_id,tool_definition_id) DO NOTHING
                    """,
                    (
                        invocation_id,
                        binding["tool_definition_id"],
                        binding["binding_mode"],
                        Jsonb(binding.get("defaults") or {}),
                        effective_scope_sha256,
                        binding.get("selection_source", "agent"),
                    ),
                )
            await connection.commit()
