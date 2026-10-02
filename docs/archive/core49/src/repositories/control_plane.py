from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from assistant_kernel.config import KernelSettings
from assistant_kernel.repositories.database import AssistantDatabase


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def json_compatible(value: Any) -> Any:
    """Normalize database/native values before crossing a durable JSON boundary."""

    def encode(item: Any) -> Any:
        if isinstance(item, (datetime, date)):
            return item.isoformat()
        if isinstance(item, (uuid.UUID, Path, Decimal)):
            return str(item)
        raise TypeError(f"Object of type {type(item).__name__} is not JSON serializable")

    return json.loads(json.dumps(value, ensure_ascii=False, default=encode))


class ControlPlaneRepository:
    """Transactional repository for the V2 control plane.

    Business-state changes and their outbox/control events are committed together.
    Long-running work is fenced by ``worker_instance_id``.
    """

    def __init__(self, settings: KernelSettings):
        self.settings = settings
        self.database = AssistantDatabase(settings)

    async def ensure_default_agent(self, project_id: str | None = None) -> str:
        agent_id = "system-butler" if project_id is None else f"project:{project_id}:manager"
        agent_type = "butler" if project_id is None else "project_manager"
        display_name = "System Butler" if project_id is None else f"{project_id} Project Manager"
        async with self.database.connection() as connection:
            if project_id is not None:
                await connection.execute(
                    """
                    INSERT INTO assistant.projects(project_id, name)
                    VALUES (%s, %s)
                    ON CONFLICT (project_id) DO UPDATE SET updated_at = now()
                    """,
                    (project_id, project_id),
                )
            await connection.execute(
                """
                INSERT INTO assistant.agents(
                    agent_id, project_id, agent_type, display_name, tool_policy
                ) VALUES (%s, %s, %s, %s, '{"sandbox":"read-only"}'::jsonb)
                ON CONFLICT (agent_id) DO UPDATE SET updated_at = now()
                """,
                (agent_id, project_id, agent_type, display_name),
            )
            await connection.commit()
        return agent_id

    async def ensure_stage_agent(self, project_id: str, stage: str) -> str:
        if not stage.replace("_", "").isalnum():
            raise ValueError("stage must be an alphanumeric slug")
        await self.database.ensure_project(project_id)
        agent_id = f"project:{project_id}:{stage}"
        async with self.database.connection() as connection:
            await connection.execute(
                """
                INSERT INTO assistant.agents(
                    agent_id, project_id, agent_type, display_name, tool_policy
                ) VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (agent_id) DO UPDATE SET updated_at = now()
                """,
                (
                    agent_id,
                    project_id,
                    f"stage_{stage}",
                    f"{project_id} {stage}",
                    Jsonb({"sandbox": "workspace-write" if stage == "implementation" else "read-only"}),
                ),
            )
            await connection.commit()
        return agent_id

    async def ensure_role_version(
        self, agent_id: str, name: str, content: str, change_reason: str = "bootstrap"
    ) -> dict[str, Any]:
        role_id = str(uuid.uuid4())
        role_version_id = str(uuid.uuid4())
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        token_estimate = max(1, len(content) // 4)
        if token_estimate > self.settings.role_hard_limit_tokens:
            raise ValueError("effective role exceeds the hard token limit")
        async with self.database.connection() as connection:
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"role:{agent_id}:{name}",),
            )
            cursor = await connection.execute(
                """
                INSERT INTO assistant.roles(role_id, agent_id, name)
                VALUES (%s, %s, %s)
                ON CONFLICT (agent_id, name) DO UPDATE SET status = 'active'
                RETURNING *
                """,
                (role_id, agent_id, name),
            )
            role = await cursor.fetchone()
            cursor = await connection.execute(
                """
                SELECT * FROM assistant.role_versions
                WHERE role_id = %s AND content_sha256 = %s
                """,
                (role["role_id"], content_hash),
            )
            existing = await cursor.fetchone()
            if existing is not None:
                await connection.commit()
                return existing
            cursor = await connection.execute(
                """
                SELECT COALESCE(MAX(version), 0) AS version,
                       MAX(role_version_id::text) FILTER (WHERE status = 'active') AS active_id
                FROM assistant.role_versions WHERE role_id = %s
                """,
                (role["role_id"],),
            )
            prior = await cursor.fetchone()
            await connection.execute(
                "UPDATE assistant.role_versions SET status = 'retired' WHERE role_id = %s AND status = 'active'",
                (role["role_id"],),
            )
            cursor = await connection.execute(
                """
                INSERT INTO assistant.role_versions(
                    role_version_id, role_id, version, content, content_sha256,
                    token_count, parent_role_version_id, change_reason
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    role_version_id,
                    role["role_id"],
                    int(prior["version"]) + 1,
                    content,
                    content_hash,
                    token_estimate,
                    prior["active_id"],
                    change_reason,
                ),
            )
            version = await cursor.fetchone()
            await connection.commit()
            return version

    async def list_projects(self) -> list[dict[str, Any]]:
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT project_id, name, status, updated_at
                FROM assistant.projects WHERE status = 'active'
                ORDER BY updated_at DESC, project_id
                """
            )
            return list(await cursor.fetchall())

    async def bind_channel(
        self,
        frontend: str,
        user_id: str,
        channel_conversation_id: str,
        project_id: str | None,
        confirmed_by: str,
    ) -> dict[str, Any]:
        agent_id = await self.ensure_default_agent(project_id)
        binding_id = str(uuid.uuid4())
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                """
                INSERT INTO assistant.channel_bindings(
                    binding_id, frontend, user_id, channel_conversation_id,
                    current_project_id, current_agent_id, confirmed_by, confirmed_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (frontend, user_id, channel_conversation_id)
                DO UPDATE SET
                    current_project_id = EXCLUDED.current_project_id,
                    current_agent_id = EXCLUDED.current_agent_id,
                    binding_version = assistant.channel_bindings.binding_version + 1,
                    confirmed_by = EXCLUDED.confirmed_by,
                    confirmed_at = now(), updated_at = now()
                RETURNING *
                """,
                (
                    binding_id,
                    frontend,
                    user_id,
                    channel_conversation_id,
                    project_id,
                    agent_id,
                    confirmed_by,
                ),
            )
            binding = await cursor.fetchone()
            await connection.execute(
                """
                UPDATE assistant.conversations
                SET current_binding_version = %s, updated_at = now()
                WHERE frontend = %s AND user_id = %s AND channel_conversation_id = %s
                """,
                (
                    binding["binding_version"],
                    frontend,
                    user_id,
                    channel_conversation_id,
                ),
            )
            await self._event(
                connection,
                "channel_binding",
                str(binding["binding_id"]),
                "binding.changed",
                {
                    "binding_version": binding["binding_version"],
                    "project_id": project_id,
                    "agent_id": agent_id,
                },
            )
            await connection.commit()
            return binding

    async def ingest_message(
        self,
        *,
        frontend: str,
        user_id: str,
        channel_conversation_id: str,
        external_message_id: str,
        content: dict[str, Any],
        metadata: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any], bool]:
        conversation_id = str(uuid.uuid4())
        message_id = str(uuid.uuid4())
        job_id = str(uuid.uuid4())
        async with self.database.connection() as connection:
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"intake:{frontend}:{user_id}:{channel_conversation_id}:{external_message_id}",),
            )
            cursor = await connection.execute(
                """
                INSERT INTO assistant.conversations(
                    conversation_id, frontend, user_id, channel_conversation_id
                ) VALUES (%s, %s, %s, %s)
                ON CONFLICT (frontend, user_id, channel_conversation_id)
                DO UPDATE SET updated_at = now()
                RETURNING *
                """,
                (conversation_id, frontend, user_id, channel_conversation_id),
            )
            conversation = await cursor.fetchone()
            cursor = await connection.execute(
                """
                SELECT m.*, j.job_id, j.status AS job_status
                FROM assistant.messages m
                LEFT JOIN assistant.control_jobs j ON j.message_id = m.message_id
                WHERE m.conversation_id = %s AND m.direction = 'inbound'
                  AND m.external_message_id = %s
                """,
                (conversation["conversation_id"], external_message_id),
            )
            existing = await cursor.fetchone()
            if existing is not None:
                await connection.commit()
                return conversation, existing, True

            cursor = await connection.execute(
                """
                SELECT * FROM assistant.conversations
                WHERE conversation_id = %s FOR UPDATE
                """,
                (conversation["conversation_id"],),
            )
            conversation = await cursor.fetchone()
            sequence_no = int(conversation["next_message_sequence"])
            cursor = await connection.execute(
                """
                SELECT * FROM assistant.channel_bindings
                WHERE frontend = %s AND user_id = %s AND channel_conversation_id = %s
                  AND status = 'active'
                """,
                (frontend, user_id, channel_conversation_id),
            )
            binding = await cursor.fetchone()
            binding_version = int(binding["binding_version"]) if binding else 1
            project_id = binding["current_project_id"] if binding else None
            agent_id = binding["current_agent_id"] if binding else "system-butler"
            await connection.execute(
                """
                UPDATE assistant.conversations
                SET next_message_sequence = next_message_sequence + 1,
                    current_binding_version = %s, updated_at = now()
                WHERE conversation_id = %s
                """,
                (binding_version, conversation["conversation_id"]),
            )
            cursor = await connection.execute(
                """
                INSERT INTO assistant.messages(
                    message_id, conversation_id, sequence_no, direction,
                    external_message_id, binding_version, project_id_snapshot,
                    agent_id_snapshot, content, metadata
                ) VALUES (%s, %s, %s, 'inbound', %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    message_id,
                    conversation["conversation_id"],
                    sequence_no,
                    external_message_id,
                    binding_version,
                    project_id,
                    agent_id,
                    Jsonb(content),
                    Jsonb(metadata or {}),
                ),
            )
            message = await cursor.fetchone()
            await connection.execute(
                """
                INSERT INTO assistant.control_jobs(
                    job_id, job_type, conversation_id, message_id, sequence_no, payload
                ) VALUES (%s, 'conversation_turn', %s, %s, %s, %s)
                """,
                (
                    job_id,
                    conversation["conversation_id"],
                    message_id,
                    sequence_no,
                    Jsonb({"binding_version": binding_version}),
                ),
            )
            await self._event(
                connection,
                "conversation",
                str(conversation["conversation_id"]),
                "message.accepted",
                {"message_id": message_id, "job_id": job_id, "sequence_no": sequence_no},
            )
            await connection.commit()
            message["job_id"] = job_id
            message["job_status"] = "queued"
            return conversation, message, False

    async def get_conversation(self, conversation_id: str) -> dict[str, Any] | None:
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "SELECT * FROM assistant.conversations WHERE conversation_id = %s",
                (conversation_id,),
            )
            return await cursor.fetchone()

    async def get_job(self, job_id: str) -> dict[str, Any] | None:
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "SELECT * FROM assistant.control_jobs WHERE job_id = %s", (job_id,)
            )
            return await cursor.fetchone()

    async def request_job_cancel(self, job_id: str) -> bool:
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                """
                UPDATE assistant.control_jobs
                SET cancel_requested_at = COALESCE(cancel_requested_at, now()),
                    status = CASE WHEN status = 'queued' THEN 'cancelled' ELSE status END,
                    ended_at = CASE WHEN status = 'queued' THEN now() ELSE ended_at END,
                    updated_at = now()
                WHERE job_id = %s AND status IN ('queued', 'running')
                RETURNING job_id
                """,
                (job_id,),
            )
            updated = await cursor.fetchone() is not None
            from assistant_kernel.repositories.stages import StageRepository
            await StageRepository.finish_aborted_job(connection, job_id, "cancelled")
            await connection.commit()
            return updated

    async def get_messages(self, conversation_id: str, limit: int = 100) -> list[dict[str, Any]]:
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT * FROM (
                    SELECT * FROM assistant.messages
                    WHERE conversation_id = %s
                    ORDER BY sequence_no DESC LIMIT %s
                ) recent ORDER BY sequence_no
                """,
                (conversation_id, min(max(limit, 1), 500)),
            )
            return list(await cursor.fetchall())

    async def claim_job(
        self,
        worker_id: str,
        worker_instance_id: str,
        lease_seconds: int,
        job_types: list[str] | None = None,
    ) -> dict[str, Any] | None:
        async with self.database.connection() as connection:
            expiry_cursor = await connection.execute(
                """
                UPDATE assistant.control_jobs
                SET status = CASE
                        WHEN cancel_requested_at IS NOT NULL THEN 'cancelled'
                        WHEN attempt >= max_attempts THEN 'dead_letter'
                        ELSE 'queued'
                    END,
                    worker_id = NULL, worker_instance_id = NULL, lease_until = NULL,
                    available_at = CASE
                        WHEN cancel_requested_at IS NOT NULL OR attempt >= max_attempts
                            THEN available_at ELSE now() END,
                    ended_at = CASE
                        WHEN cancel_requested_at IS NOT NULL OR attempt >= max_attempts
                            THEN now() ELSE ended_at END,
                    updated_at = now()
                WHERE status = 'running' AND lease_until < now()
                RETURNING job_id, status
                """
            )
            expired_jobs = await expiry_cursor.fetchall()
            for expired in expired_jobs:
                if expired["status"] == "cancelled":
                    from assistant_kernel.repositories.stages import StageRepository
                    await StageRepository.finish_aborted_job(connection, str(expired["job_id"]), "cancelled")
            cursor = await connection.execute(
                """
                WITH candidate AS (
                    SELECT j.job_id
                    FROM assistant.control_jobs j
                    WHERE j.status = 'queued' AND j.available_at <= now()
                      AND j.cancel_requested_at IS NULL
                      AND (%s::text[] IS NULL OR j.job_type = ANY(%s::text[]))
                      AND NOT EXISTS (
                          SELECT 1 FROM assistant.control_jobs earlier
                          WHERE j.conversation_id IS NOT NULL
                            AND earlier.conversation_id = j.conversation_id
                            AND earlier.status IN ('queued', 'running')
                            AND (
                                earlier.sequence_no < j.sequence_no OR
                                (earlier.sequence_no = j.sequence_no AND earlier.created_at < j.created_at)
                            )
                      )
                    ORDER BY j.priority, j.created_at
                    FOR UPDATE SKIP LOCKED LIMIT 1
                )
                UPDATE assistant.control_jobs j
                SET status = 'running', attempt = attempt + 1, worker_id = %s,
                    worker_instance_id = %s,
                    lease_until = now() + (%s * interval '1 second'),
                    started_at = COALESCE(started_at, now()), updated_at = now()
                FROM candidate WHERE j.job_id = candidate.job_id
                RETURNING j.*
                """,
                (job_types, job_types, worker_id, worker_instance_id, lease_seconds),
            )
            row = await cursor.fetchone()
            await connection.commit()
            for expired in expired_jobs:
                if expired["status"] == "dead_letter":
                    await self.fail_task_for_dead_letter(str(expired["job_id"]),
                        {"type":"LeaseExpired", "message":"job retry budget exhausted after lease expiry"})
            return row

    async def heartbeat_job(
        self, job_id: str, worker_instance_id: str, lease_seconds: int
    ) -> bool:
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                """
                UPDATE assistant.control_jobs
                SET lease_until = now() + (%s * interval '1 second'), updated_at = now()
                WHERE job_id = %s AND worker_instance_id = %s AND status = 'running'
                RETURNING cancel_requested_at
                """,
                (lease_seconds, job_id, worker_instance_id),
            )
            row = await cursor.fetchone()
            await connection.commit()
            return bool(row and row["cancel_requested_at"])

    async def finish_job(
        self,
        job_id: str,
        worker_instance_id: str,
        status: str = "succeeded",
        error: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
    ) -> bool:
        if status not in {"succeeded", "failed", "cancelled", "dead_letter"}:
            raise ValueError("invalid terminal job status")
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                """
                UPDATE assistant.control_jobs
                SET status = %s, error_json = %s, result_json = %s, lease_until = NULL,
                    ended_at = now(), updated_at = now()
                WHERE job_id = %s AND worker_instance_id = %s AND status = 'running'
                RETURNING job_id
                """,
                (
                    status,
                    Jsonb(error) if error else None,
                    Jsonb(json_compatible(result)) if result is not None else None,
                    job_id,
                    worker_instance_id,
                ),
            )
            updated = await cursor.fetchone() is not None
            await connection.commit()
            return updated

    async def retry_job(
        self, job_id: str, worker_instance_id: str, error: dict[str, Any]
    ) -> str:
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                """
                UPDATE assistant.control_jobs
                SET status = CASE WHEN attempt >= max_attempts THEN 'dead_letter' ELSE 'queued' END,
                    error_json = %s, worker_id = NULL, worker_instance_id = NULL,
                    lease_until = NULL,
                    available_at = now() + (LEAST(attempt * attempt, 60) * interval '1 second'),
                    ended_at = CASE WHEN attempt >= max_attempts THEN now() ELSE NULL END,
                    updated_at = now()
                WHERE job_id = %s AND worker_instance_id = %s AND status = 'running'
                RETURNING status
                """,
                (Jsonb(error), job_id, worker_instance_id),
            )
            row = await cursor.fetchone()
            await connection.commit()
            if row is None:
                raise RuntimeError("job lease was lost")
            return str(row["status"])

    async def fail_task_for_dead_letter(self, job_id: str, error: dict[str, Any]) -> bool:
        from assistant_kernel.repositories.stages import StageRepository
        async with self.database.connection() as connection:
            cursor = await connection.execute("""
                UPDATE assistant.tasks t SET status='failed', status_version=status_version+1,
                    result_json=COALESCE(t.result_json,'{}'::jsonb)
                        || jsonb_build_object('error',%s::jsonb,'failed_job_id',j.job_id),updated_at=now()
                FROM assistant.control_jobs j WHERE j.job_id=%s AND j.task_id=t.task_id
                    AND j.status='dead_letter' AND j.job_type<>'stage_run'
                    AND t.status NOT IN ('succeeded','failed','cancelled')
                RETURNING t.task_id
            """, (Jsonb(error),job_id))
            updated = await cursor.fetchone() is not None
            stage_updated = await StageRepository.finish_aborted_job(connection,job_id,"failed",error)
            await connection.commit()
            return updated or stage_updated

    async def conversation_job_context(self, job_id: str) -> dict[str, Any] | None:
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT j.*, m.content AS message_content, m.metadata AS message_metadata,
                       m.binding_version,
                       m.project_id_snapshot, m.agent_id_snapshot,
                       c.frontend, c.user_id, c.channel_conversation_id,
                       a.default_harness, a.default_provider, a.default_model,
                       rv.role_version_id, rv.content AS role_content
                FROM assistant.control_jobs j
                JOIN assistant.messages m ON m.message_id = j.message_id
                JOIN assistant.conversations c ON c.conversation_id = j.conversation_id
                JOIN assistant.agents a ON a.agent_id = m.agent_id_snapshot
                LEFT JOIN assistant.roles r ON r.agent_id = m.agent_id_snapshot AND r.status = 'active'
                LEFT JOIN assistant.role_versions rv ON rv.role_id = r.role_id AND rv.status = 'active'
                WHERE j.job_id = %s
                """,
                (job_id,),
            )
            return await cursor.fetchone()

    async def append_reply_and_outbox(
        self,
        *,
        job_id: str,
        source_message_id: str,
        conversation_id: str,
        binding_version: int,
        project_id: str | None,
        agent_id: str,
        text: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        message_id = str(uuid.uuid4())
        outbox_id = str(uuid.uuid4())
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "SELECT * FROM assistant.conversations WHERE conversation_id = %s FOR UPDATE",
                (conversation_id,),
            )
            conversation = await cursor.fetchone()
            if conversation is None:
                raise ValueError("conversation not found")
            cursor = await connection.execute(
                """
                SELECT * FROM assistant.messages
                WHERE conversation_id = %s AND source_message_id = %s
                  AND direction = 'outbound'
                """,
                (conversation_id, source_message_id),
            )
            existing_reply = await cursor.fetchone()
            if existing_reply is not None:
                cursor = await connection.execute(
                    "SELECT outbox_event_id FROM assistant.outbox_events WHERE dedupe_key = %s",
                    (f"message.reply:{source_message_id}",),
                )
                existing_outbox = await cursor.fetchone()
                existing_reply["outbox_event_id"] = (
                    str(existing_outbox["outbox_event_id"]) if existing_outbox else None
                )
                await connection.commit()
                return existing_reply
            sequence_no = int(conversation["next_message_sequence"])
            await connection.execute(
                """
                UPDATE assistant.conversations
                SET next_message_sequence = next_message_sequence + 1, updated_at = now()
                WHERE conversation_id = %s
                """,
                (conversation_id,),
            )
            cursor = await connection.execute(
                """
                INSERT INTO assistant.messages(
                    message_id, conversation_id, sequence_no, direction, source_message_id,
                    binding_version, project_id_snapshot, agent_id_snapshot, content, metadata
                ) VALUES (%s, %s, %s, 'outbound', %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    message_id,
                    conversation_id,
                    sequence_no,
                    source_message_id,
                    binding_version,
                    project_id,
                    agent_id,
                    Jsonb({"text": text}),
                    Jsonb(metadata or {}),
                ),
            )
            message = await cursor.fetchone()
            await connection.execute(
                """
                INSERT INTO assistant.outbox_events(
                    outbox_event_id, aggregate_type, aggregate_id, event_type,
                    payload, dedupe_key
                ) VALUES (%s, 'message', %s, 'message.reply', %s, %s)
                ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING
                """,
                (
                    outbox_id,
                    message_id,
                    Jsonb(
                        {
                            "message_id": message_id,
                            "conversation_id": conversation_id,
                            "source_message_id": source_message_id,
                            "frontend": conversation["frontend"],
                            "user_id": conversation["user_id"],
                            "channel_conversation_id": conversation["channel_conversation_id"],
                            "project_id": project_id,
                            "agent_id": agent_id,
                            "binding_version": binding_version,
                            "text": text,
                        }
                    ),
                    f"message.reply:{source_message_id}",
                ),
            )
            await self._event(
                connection,
                "conversation",
                conversation_id,
                "reply.created",
                {"message_id": message_id, "job_id": job_id, "outbox_event_id": outbox_id},
            )
            await connection.commit()
            message["outbox_event_id"] = outbox_id
            return message

    async def resolve_logical_session(
        self,
        *,
        agent_id: str,
        scope_type: str,
        scope_id: str,
        project_id: str | None,
    ) -> dict[str, Any]:
        logical_session_id = str(uuid.uuid4())
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                """
                INSERT INTO assistant.logical_sessions(
                    logical_session_id, project_id, agent_id, scope_type, scope_id
                ) VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (agent_id, scope_type, scope_id)
                DO UPDATE SET updated_at = now()
                RETURNING *
                """,
                (logical_session_id, project_id, agent_id, scope_type, scope_id),
            )
            logical = await cursor.fetchone()
            if logical["current_physical_session_record_id"] is not None:
                cursor = await connection.execute(
                    """
                    SELECT * FROM assistant.physical_sessions
                    WHERE physical_session_record_id = %s
                    """,
                    (logical["current_physical_session_record_id"],),
                )
                logical["physical_session"] = await cursor.fetchone()
            else:
                logical["physical_session"] = None
            if logical["current_checkpoint_id"] is not None:
                cursor = await connection.execute(
                    "SELECT * FROM assistant.structured_checkpoints WHERE checkpoint_id = %s",
                    (logical["current_checkpoint_id"],),
                )
                logical["checkpoint"] = await cursor.fetchone()
            else:
                logical["checkpoint"] = None
            await connection.commit()
            return logical

    async def record_successful_turn(
        self,
        *,
        logical_session_id: str,
        expected_current_record_id: str | None,
        native_session_id: str,
        harness: str,
        provider: str | None,
        model: str | None,
        role_version_id: str | None,
        prompt_manifest: dict[str, Any],
        message_id: str | None,
        active_input_tokens: int,
        context_window_tokens: int,
        archive_uri: str | None,
        archive_sha256: str | None,
        starts_new_physical_session: bool,
    ) -> dict[str, Any]:
        manifest_id = str(uuid.uuid4())
        manifest_hash = canonical_sha256(prompt_manifest)
        ratio = active_input_tokens / context_window_tokens if context_window_tokens else None
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "SELECT * FROM assistant.logical_sessions WHERE logical_session_id = %s FOR UPDATE",
                (logical_session_id,),
            )
            logical = await cursor.fetchone()
            if logical is None:
                raise ValueError("logical session not found")
            current = logical["current_physical_session_record_id"]
            if (str(current) if current is not None else None) != expected_current_record_id:
                raise RuntimeError("logical session pointer changed during turn")

            if starts_new_physical_session:
                record_id = str(uuid.uuid4())
                cursor = await connection.execute(
                    """
                    INSERT INTO assistant.physical_sessions(
                        physical_session_record_id, logical_session_id,
                        parent_physical_session_record_id, harness, provider, model,
                        native_session_id, status, role_version_id, active_input_tokens,
                        context_window_tokens, context_ratio, archive_uri, archive_sha256
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'active', %s, %s, %s, %s, %s, %s)
                    RETURNING *
                    """,
                    (
                        record_id,
                        logical_session_id,
                        expected_current_record_id,
                        harness,
                        provider,
                        model,
                        native_session_id,
                        role_version_id,
                        active_input_tokens,
                        context_window_tokens,
                        ratio,
                        archive_uri,
                        archive_sha256,
                    ),
                )
                physical = await cursor.fetchone()
                if expected_current_record_id is not None:
                    await connection.execute(
                        """
                        UPDATE assistant.physical_sessions
                        SET status = 'sealed', ended_at = now()
                        WHERE physical_session_record_id = %s AND status = 'rotation_required'
                        """,
                        (expected_current_record_id,),
                    )
                await connection.execute(
                    """
                    UPDATE assistant.logical_sessions
                    SET current_physical_session_record_id = %s,
                        status = 'active', lock_version = lock_version + 1, updated_at = now()
                    WHERE logical_session_id = %s
                    """,
                    (record_id, logical_session_id),
                )
            else:
                if expected_current_record_id is None:
                    raise ValueError("resume requires a current physical session")
                cursor = await connection.execute(
                    """
                    UPDATE assistant.physical_sessions
                    SET native_session_id = %s, active_input_tokens = %s,
                        context_window_tokens = %s, context_ratio = %s,
                        archive_uri = COALESCE(%s, archive_uri),
                        archive_sha256 = COALESCE(%s, archive_sha256)
                    WHERE physical_session_record_id = %s AND status = 'active'
                    RETURNING *
                    """,
                    (
                        native_session_id,
                        active_input_tokens,
                        context_window_tokens,
                        ratio,
                        archive_uri,
                        archive_sha256,
                        expected_current_record_id,
                    ),
                )
                physical = await cursor.fetchone()
                if physical is None:
                    raise RuntimeError("physical session is not resumable")

            await connection.execute(
                """
                INSERT INTO assistant.prompt_manifests(
                    prompt_manifest_id, logical_session_id, physical_session_record_id,
                    role_version_id, checkpoint_id, message_id, manifest, manifest_sha256
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    manifest_id,
                    logical_session_id,
                    physical["physical_session_record_id"],
                    role_version_id,
                    logical["current_checkpoint_id"],
                    message_id,
                    Jsonb(prompt_manifest),
                    manifest_hash,
                ),
            )
            await connection.execute(
                """
                UPDATE assistant.physical_sessions SET prompt_manifest_id = %s
                WHERE physical_session_record_id = %s
                """,
                (manifest_id, physical["physical_session_record_id"]),
            )
            await connection.commit()
            physical["prompt_manifest_id"] = manifest_id
            return physical

    async def require_rotation(
        self,
        *,
        logical_session_id: str,
        physical_session_record_id: str,
        project_id: str | None,
        task_id: str | None,
        checkpoint_state: dict[str, Any],
    ) -> dict[str, Any]:
        checkpoint_id = str(uuid.uuid4())
        rotation_job_id = str(uuid.uuid4())
        content_hash = canonical_sha256(checkpoint_state)
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                """
                UPDATE assistant.physical_sessions
                SET status = 'rotation_required'
                WHERE physical_session_record_id = %s AND logical_session_id = %s
                  AND status = 'active'
                RETURNING *
                """,
                (physical_session_record_id, logical_session_id),
            )
            physical = await cursor.fetchone()
            if physical is None:
                raise RuntimeError("session cannot enter rotation_required")
            await connection.execute(
                """
                INSERT INTO assistant.structured_checkpoints(
                    checkpoint_id, task_id, project_id,
                    physical_session_id, state_json, schema_version,
                    logical_session_id, physical_session_record_id,
                    checkpoint_kind, content_sha256, validation_status
                ) VALUES (%s, %s, %s, %s, %s, '2.0', %s, %s,
                          'session_rotation', %s, 'valid')
                """,
                (
                    checkpoint_id,
                    task_id,
                    project_id,
                    physical["native_session_id"],
                    Jsonb(checkpoint_state),
                    logical_session_id,
                    physical_session_record_id,
                    content_hash,
                ),
            )
            await connection.execute(
                """
                UPDATE assistant.logical_sessions
                SET current_checkpoint_id = %s, lock_version = lock_version + 1,
                    updated_at = now()
                WHERE logical_session_id = %s
                """,
                (checkpoint_id, logical_session_id),
            )
            await connection.execute(
                """
                INSERT INTO assistant.control_jobs(
                    job_id, job_type, priority, payload, dedupe_key
                ) VALUES (%s, 'session_rotation', 10, %s, %s)
                ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING
                """,
                (
                    rotation_job_id,
                    Jsonb(
                        {
                            "logical_session_id": logical_session_id,
                            "checkpoint_id": checkpoint_id,
                        }
                    ),
                    f"session_rotation:{logical_session_id}:{checkpoint_id}",
                ),
            )
            await connection.commit()
            return {
                "checkpoint_id": checkpoint_id,
                "content_sha256": content_hash,
                "rotation_job_id": rotation_job_id,
            }

    async def session_rotation_context(
        self, logical_session_id: str, checkpoint_id: str
    ) -> dict[str, Any] | None:
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT l.*, p.native_session_id AS parent_native_session_id,
                       p.physical_session_record_id AS parent_physical_session_record_id,
                       c.state_json AS checkpoint_state, c.checkpoint_id,
                       rv.role_version_id, rv.content AS role_content,
                       a.default_provider, a.default_model
                FROM assistant.logical_sessions l
                JOIN assistant.physical_sessions p
                  ON p.physical_session_record_id = l.current_physical_session_record_id
                JOIN assistant.structured_checkpoints c
                  ON c.checkpoint_id = l.current_checkpoint_id
                JOIN assistant.agents a ON a.agent_id = l.agent_id
                LEFT JOIN assistant.roles r ON r.agent_id = a.agent_id AND r.status = 'active'
                LEFT JOIN assistant.role_versions rv
                  ON rv.role_id = r.role_id AND rv.status = 'active'
                WHERE l.logical_session_id = %s AND c.checkpoint_id = %s
                  AND p.status = 'rotation_required'
                """,
                (logical_session_id, checkpoint_id),
            )
            return await cursor.fetchone()

    async def record_harness_events(
        self,
        *,
        logical_session_id: str,
        physical_session_record_id: str,
        job_id: str | None,
        events: list[dict[str, Any]],
        raw_trace_uri: str | None,
    ) -> None:
        tool_item_types = {
            "command_execution", "function_call", "mcp_tool_call", "web_search",
            "file_change", "dynamic_tool_call",
        }
        async with self.database.connection() as connection:
            for ordinal, event in enumerate(events):
                item = event.get("item") if isinstance(event, dict) else None
                item_type = item.get("type") if isinstance(item, dict) else None
                if item_type in tool_item_types:
                    await connection.execute(
                        """
                        INSERT INTO assistant.tool_events(
                            tool_event_id, logical_session_id, physical_session_record_id,
                            job_id, ordinal, tool_name, event_type, payload, raw_trace_uri
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (physical_session_record_id, job_id, ordinal) DO NOTHING
                        """,
                        (
                            str(uuid.uuid4()), logical_session_id, physical_session_record_id,
                            job_id, ordinal, item.get("name") if isinstance(item, dict) else None,
                            str(event.get("type") or "unknown"), Jsonb(event), raw_trace_uri,
                        ),
                    )
                else:
                    await connection.execute(
                        """
                        INSERT INTO assistant.model_events(
                            model_event_id, logical_session_id, physical_session_record_id,
                            job_id, ordinal, event_type, payload, raw_trace_uri
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (physical_session_record_id, job_id, ordinal) DO NOTHING
                        """,
                        (
                            str(uuid.uuid4()), logical_session_id, physical_session_record_id,
                            job_id, ordinal, str(event.get("type") or "unknown"),
                            Jsonb(event), raw_trace_uri,
                        ),
                    )
            await connection.commit()

    async def record_model_execution_profile(
        self,
        *,
        project_id: str | None,
        task_id: str | None,
        stage_id: str | None,
        harness: str,
        provider: str | None,
        model: str | None,
        reasoning_effort: str | None,
        timeout_seconds: int,
        sandbox_policy: str,
        approval_policy: str,
        requested_profile: dict[str, Any],
        resolved_profile: dict[str, Any],
    ) -> str:
        profile_id = str(uuid.uuid4())
        async with self.database.connection() as connection:
            await connection.execute(
                """
                INSERT INTO assistant.model_execution_profiles(
                    model_execution_profile_id, project_id, task_id, stage_id,
                    harness, provider, model, reasoning_effort, timeout_seconds,
                    sandbox_policy, approval_policy, requested_profile, resolved_profile
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    profile_id, project_id, task_id, stage_id, harness, provider,
                    model, reasoning_effort, timeout_seconds, sandbox_policy,
                    approval_policy, Jsonb(requested_profile), Jsonb(resolved_profile),
                ),
            )
            await connection.commit()
        return profile_id

    async def claim_outbox(
        self, worker_id: str, worker_instance_id: str, lease_seconds: int, limit: int = 10, channel: str | None = None
    ) -> list[dict[str, Any]]:
        async with self.database.connection() as connection:
            await connection.execute("""
                UPDATE assistant.outbox_events SET status='dead_letter',lease_until=NULL,
                    last_error='delivery lease expired after retry limit',updated_at=now()
                WHERE status='delivering' AND lease_until<now() AND attempts>=max_attempts
            """)
            await connection.execute(
                """
                UPDATE assistant.outbox_events
                SET status = 'pending', worker_id = NULL, worker_instance_id = NULL,
                    lease_until = NULL, updated_at = now()
                WHERE status = 'delivering' AND lease_until < now() AND attempts < max_attempts
                """
            )
            cursor = await connection.execute(
                """
                WITH candidates AS (
                    SELECT outbox_event_id FROM assistant.outbox_events
                    WHERE status = 'pending' AND next_attempt_at <= now()
                      AND (%s::text IS NULL OR payload->'delivery'->>'channel'=%s OR payload->>'frontend'=%s)
                    ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT %s
                )
                UPDATE assistant.outbox_events o
                SET status = 'delivering', attempts = attempts + 1,
                    worker_id = %s, worker_instance_id = %s,
                    lease_until = now() + (%s * interval '1 second'), updated_at = now()
                FROM candidates c WHERE o.outbox_event_id = c.outbox_event_id
                RETURNING o.*
                """,
                (channel, channel, channel, min(max(limit, 1), 100), worker_id, worker_instance_id, lease_seconds),
            )
            rows = list(await cursor.fetchall())
            await connection.commit()
            return rows

    async def acknowledge_outbox(
        self, outbox_event_id: str, worker_instance_id: str, acknowledged_by: str
    ) -> bool:
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                """
                UPDATE assistant.outbox_events
                SET status = 'delivered', delivered_at = now(), lease_until = NULL,
                    acknowledged_by = %s, updated_at = now()
                WHERE outbox_event_id = %s AND worker_instance_id = %s
                  AND status = 'delivering'
                RETURNING outbox_event_id
                """,
                (acknowledged_by, outbox_event_id, worker_instance_id),
            )
            updated = await cursor.fetchone() is not None
            await connection.commit()
            return updated

    async def fail_outbox(
        self, outbox_event_id: str, worker_instance_id: str, error: str
    ) -> str:
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                """
                UPDATE assistant.outbox_events
                SET status = CASE WHEN attempts >= max_attempts THEN 'dead_letter' ELSE 'pending' END,
                    next_attempt_at = now() + (LEAST(attempts * attempts, 300) * interval '1 second'),
                    lease_until = NULL, worker_id = NULL, worker_instance_id = NULL,
                    last_error = %s, updated_at = now()
                WHERE outbox_event_id = %s AND worker_instance_id = %s
                  AND status = 'delivering'
                RETURNING status
                """,
                (error[:2000], outbox_event_id, worker_instance_id),
            )
            row = await cursor.fetchone()
            await connection.commit()
            if row is None:
                raise RuntimeError("outbox lease was lost")
            return str(row["status"])

    async def register_workflow_version(
        self,
        *,
        project_id: str,
        workflow_id: str,
        definition_path: str,
        definition_sha256: str,
        input_schema: dict[str, Any],
        output_schema: dict[str, Any],
        spec: dict[str, Any],
    ) -> dict[str, Any]:
        await self.database.ensure_project(project_id)
        version_id = str(uuid.uuid4())
        async with self.database.connection() as connection:
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"workflow-version:{project_id}:{workflow_id}",),
            )
            cursor = await connection.execute(
                """
                SELECT COALESCE(MAX(version), 0) AS version
                FROM assistant.workflow_versions
                WHERE project_id = %s AND workflow_id = %s
                """,
                (project_id, workflow_id),
            )
            latest = await cursor.fetchone()
            cursor = await connection.execute(
                """
                INSERT INTO assistant.workflow_versions(
                    workflow_version_id, project_id, workflow_id, version,
                    definition_path, definition_sha256, input_schema,
                    output_schema, spec_json, status
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'draft')
                RETURNING *
                """,
                (
                    version_id,
                    project_id,
                    workflow_id,
                    int(latest["version"]) + 1,
                    definition_path,
                    definition_sha256,
                    Jsonb(input_schema),
                    Jsonb(output_schema),
                    Jsonb(spec),
                ),
            )
            row = await cursor.fetchone()
            await self._event(
                connection,
                "workflow_version",
                version_id,
                "workflow_version.created",
                {"project_id": project_id, "workflow_id": workflow_id, "version": row["version"]},
            )
            await connection.commit()
            return row

    async def activate_workflow_version(
        self,
        workflow_version_id: str,
        *,
        tests_uri: str,
        review_uri: str,
        accepted_by: str,
        acceptance_evidence: dict[str, Any],
    ) -> dict[str, Any]:
        if not tests_uri or not review_uri or not accepted_by or not acceptance_evidence:
            raise ValueError("tests, review, acceptance actor and evidence are required")
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT * FROM assistant.workflow_versions
                WHERE workflow_version_id = %s FOR UPDATE
                """,
                (workflow_version_id,),
            )
            version = await cursor.fetchone()
            if version is None:
                raise ValueError("workflow version not found")
            if version["project_id"] is None:
                raise ValueError("legacy workflow version has no project scope")
            if version["status"] not in {"draft", "candidate"}:
                raise ValueError("only draft or candidate versions can be activated")
            if (version["spec_json"] or {}).get("executor", {}).get("type") != "python_script":
                raise ValueError("only python_script versions can be activated here")
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"workflow-activation:{version['project_id']}:{version['workflow_id']}",),
            )
            await connection.execute(
                """
                UPDATE assistant.workflow_versions
                SET status = 'retired', retired_at = now()
                WHERE project_id = %s AND workflow_id = %s AND status = 'active'
                """,
                (version["project_id"], version["workflow_id"]),
            )
            spec = dict(version["spec_json"] or {})
            spec["activation"] = {
                "accepted_by": accepted_by,
                "acceptance_evidence": acceptance_evidence,
            }
            cursor = await connection.execute(
                """
                UPDATE assistant.workflow_versions
                SET status = 'active', tests_uri = %s, review_uri = %s,
                    spec_json = %s, activated_at = now(), retired_at = NULL
                WHERE workflow_version_id = %s
                RETURNING *
                """,
                (tests_uri, review_uri, Jsonb(spec), workflow_version_id),
            )
            active = await cursor.fetchone()
            await connection.execute(
                """
                INSERT INTO assistant.workflow_current_versions(
                    project_id, workflow_id, workflow_version_id
                ) VALUES (%s, %s, %s)
                ON CONFLICT (project_id, workflow_id)
                DO UPDATE SET workflow_version_id = EXCLUDED.workflow_version_id,
                    pointer_version = assistant.workflow_current_versions.pointer_version + 1,
                    updated_at = now()
                """,
                (active["project_id"], active["workflow_id"], workflow_version_id),
            )
            await self._event(
                connection,
                "workflow_version",
                workflow_version_id,
                "workflow_version.activated",
                {"accepted_by": accepted_by},
            )
            await connection.commit()
            return active

    async def start_workflow_run(
        self,
        *,
        project_id: str,
        workflow_id: str,
        input_data: dict[str, Any],
        workflow_version_id: str | None = None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        task_id = str(uuid.uuid4())
        workflow_run_id = str(uuid.uuid4())
        job_id = str(uuid.uuid4())
        request_hash = canonical_sha256({"project_id": project_id, "workflow_id": workflow_id,
                                         "input": input_data, "workflow_version_id": workflow_version_id})
        async with self.database.connection() as connection:
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"idempotency:workflow_run:{idempotency_key}",),
            )
            cursor = await connection.execute(
                """
                SELECT response_json, request_sha256 FROM assistant.idempotency_records
                WHERE scope = 'workflow_run' AND idempotency_key = %s
                """,
                (idempotency_key,),
            )
            existing = await cursor.fetchone()
            if existing is not None and existing["response_json"]:
                legacy_hash = canonical_sha256({"project_id":project_id,"workflow_id":workflow_id,"input":input_data})
                if existing["request_sha256"] != request_hash and not (
                    workflow_version_id is None and existing["request_sha256"] == legacy_hash
                ):
                    raise ValueError("idempotency key reused for a different workflow request")
                await connection.commit()
                return dict(existing["response_json"])
            if workflow_version_id:
                cursor = await connection.execute(
                    """
                    SELECT * FROM assistant.workflow_versions
                    WHERE workflow_version_id = %s AND project_id = %s AND workflow_id = %s
                      AND status IN ('active', 'retired')
                    """,
                    (workflow_version_id, project_id, workflow_id),
                )
            else:
                cursor = await connection.execute(
                    """
                    SELECT v.* FROM assistant.workflow_current_versions c
                    JOIN assistant.workflow_versions v
                      ON v.workflow_version_id = c.workflow_version_id
                    WHERE c.project_id = %s AND c.workflow_id = %s AND v.status = 'active'
                    """,
                    (project_id, workflow_id),
                )
            version = await cursor.fetchone()
            if version is None:
                raise ValueError("active workflow version not found")
            if (version["spec_json"] or {}).get("executor", {}).get("type") != "python_script":
                raise ValueError("use the dedicated builds API for built-in stage workflows")
            await connection.execute(
                """
                INSERT INTO assistant.tasks(
                    task_id, project_id, workflow_version_id, task_type, status,
                    request_json
                ) VALUES (%s, %s, %s, 'routine_workflow', 'queued', %s)
                """,
                (task_id, project_id, version["workflow_version_id"], Jsonb(input_data)),
            )
            await connection.execute(
                """
                INSERT INTO assistant.workflow_runs(
                    workflow_run_id, task_id, workflow_version_id,
                    status, input_json
                ) VALUES (%s, %s, %s, 'queued', %s)
                """,
                (
                    workflow_run_id,
                    task_id,
                    version["workflow_version_id"],
                    Jsonb(input_data),
                ),
            )
            await connection.execute(
                """
                INSERT INTO assistant.control_jobs(
                    job_id, job_type, task_id, workflow_run_id, payload
                ) VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    job_id,
                    "routine_workflow",
                    task_id,
                    workflow_run_id,
                    Jsonb({"workflow_version_id": str(version["workflow_version_id"])}),
                ),
            )
            response = {
                "task_id": task_id,
                "workflow_run_id": workflow_run_id,
                "workflow_version_id": str(version["workflow_version_id"]),
                "job_id": job_id,
                "status": "queued",
            }
            await connection.execute(
                """
                INSERT INTO assistant.idempotency_records(
                    idempotency_id, scope, idempotency_key, request_sha256,
                    response_status, response_json
                ) VALUES (%s, 'workflow_run', %s, %s, 202, %s)
                """,
                (
                    str(uuid.uuid4()),
                    idempotency_key,
                    request_hash,
                    Jsonb(response),
                ),
            )
            await connection.commit()
            return response

    async def routine_workflow_context(self, workflow_run_id: str) -> dict[str, Any] | None:
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT r.*, t.project_id, v.workflow_id, v.definition_path,
                       v.definition_sha256, v.input_schema, v.output_schema, v.spec_json
                FROM assistant.workflow_runs r
                JOIN assistant.tasks t ON t.task_id = r.task_id
                JOIN assistant.workflow_versions v ON v.workflow_version_id = r.workflow_version_id
                WHERE r.workflow_run_id = %s
                """,
                (workflow_run_id,),
            )
            return await cursor.fetchone()

    async def dispatch_routine_python_execution(
        self,
        *,
        workflow_run_id: str,
        source_uri: str,
        input_data: dict[str, Any],
        output_schema: dict[str, Any],
        timeout_seconds: int,
        args: list[str],
    ) -> dict[str, Any]:
        execution_id = str(uuid.uuid4())
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT r.*, t.project_id FROM assistant.workflow_runs r
                JOIN assistant.tasks t ON t.task_id = r.task_id
                WHERE r.workflow_run_id = %s FOR UPDATE
                """,
                (workflow_run_id,),
            )
            run = await cursor.fetchone()
            if run is None:
                raise ValueError("workflow run not found")
            if run["status"] != "queued":
                raise ValueError("workflow run is not queued")
            await connection.execute(
                """
                INSERT INTO assistant.python_executions(
                    execution_id, task_id, project_id, status, mode, request_json, source_uri
                ) VALUES (%s, %s, %s, 'queued', 'script', %s, %s)
                """,
                (
                    execution_id,
                    run["task_id"],
                    run["project_id"],
                    Jsonb(
                        {
                            "args": args,
                            "timeout_seconds": timeout_seconds,
                            "input": input_data,
                            "output_schema": output_schema,
                            "workflow_run_id": workflow_run_id,
                        }
                    ),
                    source_uri,
                ),
            )
            await connection.execute(
                """
                UPDATE assistant.workflow_runs
                SET status = 'running', status_version = status_version + 1,
                    executor_ref = %s, started_at = now()
                WHERE workflow_run_id = %s
                """,
                (f"python-execution:{execution_id}", workflow_run_id),
            )
            await connection.execute(
                """
                UPDATE assistant.tasks SET status = 'running', status_version = status_version + 1,
                    updated_at = now() WHERE task_id = %s
                """,
                (run["task_id"],),
            )
            await connection.commit()
            return {"execution_id": execution_id, "status": "queued"}

    async def get_workflow_run(self, workflow_run_id: str) -> dict[str, Any] | None:
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT r.*, t.project_id, t.status AS task_status
                FROM assistant.workflow_runs r JOIN assistant.tasks t ON t.task_id = r.task_id
                WHERE r.workflow_run_id = %s
                """,
                (workflow_run_id,),
            )
            return await cursor.fetchone()

    async def start_project_build(
        self, *, project_id: str, request_data: dict[str, Any], idempotency_key: str,
    ) -> dict[str, Any]:
        from assistant_kernel.repositories.stages import StageRepository
        return await StageRepository(self.settings).start(
            project_id=project_id, request_data=request_data, idempotency_key=idempotency_key,
        )


    async def create_artifact_version(
        self,
        *,
        project_id: str,
        task_id: str,
        stage_id: str,
        artifact_type: str,
        storage_uri: str,
        content_sha256: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        artifact_id = f"{task_id}:{stage_id}"
        artifact_version_id = str(uuid.uuid4())
        async with self.database.connection() as connection:
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"artifact:{project_id}:{artifact_id}",),
            )
            cursor = await connection.execute(
                """
                SELECT COALESCE(MAX(version), 0) AS version
                FROM assistant.artifact_versions
                WHERE project_id = %s AND artifact_id = %s
                """,
                (project_id, artifact_id),
            )
            latest = await cursor.fetchone()
            cursor = await connection.execute(
                """
                INSERT INTO assistant.artifact_versions(
                    artifact_version_id, project_id, artifact_id, task_id, stage_id,
                    artifact_type, version, storage_uri, content_sha256, metadata
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    artifact_version_id, project_id, artifact_id, task_id, stage_id,
                    artifact_type, int(latest["version"]) + 1, storage_uri,
                    content_sha256, Jsonb(metadata or {}),
                ),
            )
            row = await cursor.fetchone()
            await connection.commit()
            return row


    async def decide_approval(
        self,
        *,
        approval_id: str,
        decision: str,
        edited_payload: dict[str, Any] | None,
        decided_by: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        from assistant_kernel.repositories.stages import StageRepository
        return await StageRepository(self.settings).respond(
            approval_id=approval_id,
            answer={"action": decision, "edited_payload": edited_payload or {}},
            decision=decision,
            decided_by=decided_by,
            idempotency_key=idempotency_key,
        )


    async def get_approval(self, approval_id: str) -> dict[str, Any] | None:
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "SELECT * FROM assistant.approval_requests WHERE approval_id = %s",
                (approval_id,),
            )
            return await cursor.fetchone()

    async def list_task_approvals(self, task_id: str) -> list[dict[str, Any]]:
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT * FROM assistant.approval_requests
                WHERE task_id = %s ORDER BY created_at
                """,
                (task_id,),
            )
            return list(await cursor.fetchall())

    async def get_active_role(self, agent_id: str) -> dict[str, Any] | None:
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT r.role_id, r.agent_id, r.name, rv.*
                FROM assistant.roles r JOIN assistant.role_versions rv ON rv.role_id = r.role_id
                WHERE r.agent_id = %s AND r.name = 'primary' AND r.status = 'active' AND rv.status = 'active'
                ORDER BY rv.version DESC LIMIT 1
                """,
                (agent_id,),
            )
            return await cursor.fetchone()

    async def replace_project_context_entry(
        self,
        *,
        project_id: str,
        entry_id: str,
        entry_type: str,
        statement: str,
        rationale: str,
        evidence_refs: list[str],
        supersedes_entry_id: str | None,
    ) -> dict[str, Any]:
        await self.database.ensure_project(project_id)
        content_hash = canonical_sha256(
            {
                "entry_type": entry_type,
                "statement": statement,
                "rationale": rationale,
                "evidence_refs": evidence_refs,
                "supersedes_entry_id": supersedes_entry_id,
            }
        )
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT COALESCE(MAX(version), 0) AS version
                FROM assistant.project_context_entries
                WHERE project_id = %s AND entry_id = %s
                """,
                (project_id, entry_id),
            )
            latest = await cursor.fetchone()
            await connection.execute(
                """
                UPDATE assistant.project_context_entries SET status = 'retired'
                WHERE project_id = %s AND entry_id = %s AND status = 'active'
                """,
                (project_id, entry_id),
            )
            cursor = await connection.execute(
                """
                INSERT INTO assistant.project_context_entries(
                    project_id, entry_id, version, entry_type, statement, rationale,
                    evidence_refs, supersedes_entry_id, content_sha256
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    project_id, entry_id, int(latest["version"]) + 1, entry_type,
                    statement, rationale, Jsonb(evidence_refs), supersedes_entry_id,
                    content_hash,
                ),
            )
            row = await cursor.fetchone()
            await connection.commit()
            return row

    async def list_project_context(self, project_id: str) -> list[dict[str, Any]]:
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT * FROM assistant.project_context_entries
                WHERE project_id = %s AND status = 'active'
                ORDER BY entry_type, entry_id
                """,
                (project_id,),
            )
            return list(await cursor.fetchall())

    async def create_change_request(
        self,
        *,
        project_id: str,
        task_id: str | None,
        target_type: str,
        target_id: str,
        impact: str,
        confidence: float | None,
        proposal: dict[str, Any],
        evidence_refs: list[str],
    ) -> dict[str, Any]:
        change_request_id = str(uuid.uuid4())
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                """
                INSERT INTO assistant.change_requests(
                    change_request_id, project_id, task_id, target_type, target_id,
                    impact, confidence, proposal, evidence_refs
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    change_request_id, project_id, task_id, target_type, target_id,
                    impact, confidence, Jsonb(proposal), Jsonb(evidence_refs),
                ),
            )
            row = await cursor.fetchone()
            await connection.commit()
            return row

    async def list_artifacts(self, project_id: str, task_id: str | None = None) -> list[dict[str, Any]]:
        async with self.database.connection() as connection:
            if task_id:
                cursor = await connection.execute(
                    """
                    SELECT * FROM assistant.artifact_versions
                    WHERE project_id = %s AND task_id = %s ORDER BY created_at
                    """,
                    (project_id, task_id),
                )
            else:
                cursor = await connection.execute(
                    """
                    SELECT * FROM assistant.artifact_versions
                    WHERE project_id = %s ORDER BY created_at DESC LIMIT 500
                    """,
                    (project_id,),
                )
            return list(await cursor.fetchall())

    @staticmethod
    async def _event(
        connection,
        aggregate_type: str,
        aggregate_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        await connection.execute(
            """
            INSERT INTO assistant.control_events(
                control_event_id, aggregate_type, aggregate_id, event_type, payload
            ) VALUES (%s, %s, %s, %s, %s)
            """,
            (str(uuid.uuid4()), aggregate_type, aggregate_id, event_type, Jsonb(payload)),
        )
