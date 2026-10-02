from __future__ import annotations

import base64
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb
import yaml

from assistant_kernel.config import KernelSettings
from assistant_kernel.repositories.database import AssistantDatabase
from assistant_kernel.storage import ProjectRuntimeManager


def json_compatible(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): json_compatible(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_compatible(item) for item in value]
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        json_compatible(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _superseded_supervisor_card_content(
    content: dict[str, Any], *, reason: str, context_epoch: int,
) -> dict[str, Any]:
    """Return the durable disabled projection for one superseded v2 card."""
    superseded_at = datetime.now(timezone.utc).isoformat()
    return {
        **json_compatible(content),
        "action_status": "superseded",
        "superseded_reason": str(reason),
        "superseded_at": superseded_at,
        "superseded_context_epoch": int(context_epoch),
        "resolved_at": superseded_at,
        "resolution_action": "superseded",
        "actions": [
            {**dict(item), "disabled": True}
            for item in content.get("actions") or []
            if isinstance(item, dict)
        ],
    }


def _encode_history_cursor(created_at: datetime, message_id: str) -> str:
    payload = json.dumps(
        {"created_at": created_at.isoformat(), "message_id": message_id},
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_history_cursor(cursor: str | None) -> tuple[datetime | None, str | None]:
    if not cursor:
        return None, None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        created_at = datetime.fromisoformat(str(payload["created_at"]))
        message_id = str(uuid.UUID(str(payload["message_id"])))
        return created_at, message_id
    except Exception as exc:
        raise ValueError("invalid recent-history cursor") from exc


def _history_text(content: dict[str, Any]) -> str:
    for key in ("text", "question_text", "approval_question", "summary"):
        value = str(content.get(key) or "").strip()
        if value:
            return value
    questions = content.get("questions") or []
    if isinstance(questions, list):
        return "\n".join(
            str(item.get("question_text") or "").strip()
            for item in questions if isinstance(item, dict) and item.get("question_text")
        )
    return ""


def approval_decision(content: dict[str, Any]) -> str | None:
    nested = content.get("action") or content.get("metadata") or {}
    raw = str(
        content.get("decision") or nested.get("decision") or
        nested.get("value") or content.get("text") or ""
    ).strip().lower()
    if raw in {"approve", "approved", "yes", "ok", "同意", "批准", "批准创建", "确认", "通过"}:
        return "approve"
    if raw in {"revise", "changes", "request_changes", "修改", "需要修改", "退回修改"}:
        return "revise"
    if raw in {"reject", "rejected", "no", "cancel", "拒绝", "不同意", "取消"}:
        return "reject"
    return None


def conversation_key(project_id: str, agent_key: str, channel: str, chat_id: str) -> str:
    return "conversation:" + canonical_sha256([project_id, agent_key, channel, chat_id])


def scheduling_lane(run_type: str, agent_key: str) -> str:
    """Keep interactive chats out of the Project execution serialization set.

    v6.1: supervisor/operator chats are classified as conversation runs.
    Conversation runs serialize per conversation_key only; the historical
    cross-agent same-lane project exclusion was removed (see
    migrations/upgrade_core5_v5.sql). Keep this classification in sync with
    the SQL function assistant.run_lane used by the partial unique indexes.
    """
    if run_type == "chat" and (
        agent_key == "system-butler"
        or agent_key.endswith(":manager")
        or agent_key.endswith(":supervisor")
        or agent_key.endswith(":operator")
    ):
        return "conversation"
    return "execution"


def review_rework_stage(stage_plan: list[str]) -> str:
    """Return the stage immediately preceding Review in the pinned task plan."""
    try:
        review_index = stage_plan.index("review")
    except ValueError as exc:
        raise ValueError("Review rework requires a Review stage") from exc
    if review_index <= 0 or stage_plan[review_index - 1] not in {
        "requirements", "implementation",
    }:
        raise ValueError("Review rework requires Requirements or Implementation before Review")
    return stage_plan[review_index - 1]


def stage_executor(request: dict[str, Any], stage: str) -> dict[str, Any]:
    executor = {
        **dict(request.get("task_executor") or {}),
        **dict((request.get("stage_bindings") or {}).get(stage) or {}),
    }
    executor["scheduling_lane"] = "execution"
    return json_compatible(executor)


class CoreRepository:
    """Persistence boundary for the four-table first-release core."""

    def __init__(self, settings: KernelSettings):
        self.settings = settings
        self.database = AssistantDatabase(settings)

    def _single_agent_project(self, project_id: str) -> bool:
        vault = ProjectRuntimeManager(self.settings.data_root).describe(project_id).vault_root
        project_file = vault / "project.yaml"
        if not project_file.is_file():
            return False
        project = yaml.safe_load(project_file.read_text(encoding="utf-8")) or {}
        if not isinstance(project, dict):
            raise ValueError("Project configuration is invalid")
        return project.get("task_protocol") == "single-agent.v2"

    def _retired_stage(self, project_id: str, run: dict[str, Any]) -> bool:
        """A cut-over Project must not resume its historical Stage chain."""
        return (
            str(run.get("run_type") or "") == "stage"
            and self._single_agent_project(project_id)
            and (run.get("request_json") or {}).get("task_protocol") != "single-agent.v2"
        )

    async def schema_version(self) -> str | None:
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "SELECT value #>> '{}' AS value FROM assistant.kernel_meta WHERE key='schema_version'"
            )
            row = await cursor.fetchone()
            return str(row["value"]) if row else None

    async def table_names(self) -> list[str]:
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname='assistant' ORDER BY tablename"
            )
            return [str(row["tablename"]) for row in await cursor.fetchall()]

    async def ingest_message(
        self,
        *,
        project_id: str,
        agent_key: str,
        channel: str,
        chat_id: str,
        user_id: str | None,
        external_event_id: str,
        external_message_id: str | None,
        content: dict[str, Any],
        message_type: str = "text",
        reply_to_message_id: str | None = None,
        executor: dict[str, Any] | None = None,
        auto_reply_to_unambiguous: bool = False,
        before_resume: Callable[[], Any] | None = None,
    ) -> dict[str, Any]:
        message_id = str(uuid.uuid4())
        run_id = str(uuid.uuid4())
        superseded_run_ids: list[str] = []
        single_agent_project = self._single_agent_project(project_id)
        async with self.database.connection() as connection:
            if reply_to_message_id:
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                    (f"conversation-command:{project_id}:{channel}:{chat_id}",),
                )
            cursor = await connection.execute(
                """
                INSERT INTO assistant.messages(
                    message_id,project_id,channel,chat_id,user_id,direction,message_type,
                    external_event_id,external_message_id,reply_to_message_id,content_json,
                    delivery_status,idempotency_key
                ) VALUES (%s,%s,%s,%s,%s,'inbound',%s,%s,%s,%s,%s,'received',%s)
                ON CONFLICT (channel,external_event_id)
                    WHERE external_event_id IS NOT NULL DO NOTHING
                RETURNING *
                """,
                (
                    message_id, project_id, channel, chat_id, user_id, message_type,
                    external_event_id, external_message_id, reply_to_message_id,
                    Jsonb(json_compatible(content)), f"inbound:{channel}:{external_event_id}",
                ),
            )
            message = await cursor.fetchone()
            if message is None:
                cursor = await connection.execute(
                    "SELECT * FROM assistant.messages WHERE channel=%s AND external_event_id=%s",
                    (channel, external_event_id),
                )
                message = await cursor.fetchone()
                expected = {
                    "project_id": project_id,
                    "chat_id": chat_id,
                    "user_id": user_id,
                    "message_type": message_type,
                    "reply_to_message_id": uuid.UUID(reply_to_message_id) if reply_to_message_id else None,
                    "content_json": json_compatible(content),
                }
                auto_correlated_duplicate = bool(
                    auto_reply_to_unambiguous
                    and message_type == "text"
                    and reply_to_message_id is None
                    and message["message_type"] == "answer"
                    and message["reply_to_message_id"] is not None
                )
                compared = (
                    {key: value for key, value in expected.items()
                     if key not in {"message_type", "reply_to_message_id"}}
                    if auto_correlated_duplicate else expected
                )
                if any(message[key] != value for key, value in compared.items()):
                    raise ValueError("external_event_id was reused with different message content")
                await connection.commit()
                return {
                    "message": message, "run": None, "duplicate": True,
                    "resumed": False, "superseded_run_ids": [],
                }

            semantic_text = str(content.get("text") or "").strip()
            semantic_attachments = list(
                (content.get("discord") or {}).get("attachments") or []
            )
            is_semantic_project_message = bool(
                project_id != "system"
                and message_type in {"text", "answer"}
                and (semantic_text or semantic_attachments)
                and not semantic_text.startswith("/")
                and not bool((content.get("discord") or {}).get("author_bot"))
                and (
                    agent_key == f"project:{project_id}:manager"
                    or agent_key == f"project:{project_id}:supervisor"
                )
            )
            if is_semantic_project_message:
                # A successfully inserted semantic PM/Supervisor event changes
                # the Task head immediately.  Do this in the inbound transaction
                # so an old card cannot be adopted in the routing gap below.
                task = await (await connection.execute(
                    """
                    SELECT * FROM assistant.runs
                    WHERE project_id=%s
                      AND request_json->>'task_protocol'='single-agent.v2'
                      AND status IN ('queued','running','waiting_user','waiting_supervisor','waiting_children','blocked','reconciling')
                    ORDER BY created_at,run_id LIMIT 1
                    FOR UPDATE
                    """,
                    (project_id,),
                )).fetchone()
                if task is not None:
                    await self._advance_supervisor_context_epoch_locked(
                        connection, task=task,
                        reason=(
                            "new_supervisor_message"
                            if agent_key.endswith(":supervisor")
                            else "new_pm_message"
                        ),
                        source_id=str(message["message_id"]),
                    )

            resumed = False
            stale_reply = False
            decision_required = False
            decision = None
            partial_answer = False
            correction_run = False
            run = None
            question = None
            if (
                auto_reply_to_unambiguous
                and message_type == "text"
                and reply_to_message_id is None
                and str(content.get("text") or "").strip()
            ):
                cursor = await connection.execute(
                    """
                    SELECT m.message_id,m.message_type,m.delivery_status,r.agent_key
                    FROM assistant.messages m
                    JOIN assistant.runs r ON r.run_id=m.run_id
                    WHERE m.project_id=%s AND m.channel=%s AND m.chat_id=%s
                      AND m.user_id IS NOT DISTINCT FROM %s
                      AND m.direction='outbound'
                      AND m.message_type IN ('question','approval')
                      AND r.status='waiting_user'
                      AND (NOT %s OR r.run_type<>'stage')
                      AND m.created_at > COALESCE((
                        SELECT max(reset.created_at)
                        FROM assistant.messages reset
                        WHERE reset.project_id=m.project_id
                          AND reset.channel=m.channel AND reset.chat_id=m.chat_id
                          AND reset.direction='system'
                          AND reset.message_type='conversation_reset'
                          AND reset.content_json->>'schema_version'='conversation-reset.v1'
                      ), '-infinity'::timestamptz)
                      AND (
                        (m.message_type='approval'
                         AND r.waiting_question_message_id=m.message_id)
                        OR
                        (m.message_type='question' AND (
                          r.waiting_question_message_id=m.message_id
                          OR COALESCE(m.content_json->>'interaction_seq','')
                             = r.interaction_seq::text
                        ))
                      )
                    ORDER BY m.created_at DESC,m.message_id DESC
                    LIMIT 2
                    FOR UPDATE OF r
                    """,
                    (project_id, channel, chat_id, user_id, single_agent_project),
                )
                candidates = list(await cursor.fetchall())
                if len(candidates) == 1 and candidates[0]["delivery_status"] == "sent":
                    candidate = candidates[0]
                    reply_to_message_id = str(candidate["message_id"])
                    message_type = "answer"
                    agent_key = str(candidate["agent_key"])
                    await connection.execute(
                        """
                        UPDATE assistant.messages
                        SET message_type='answer',reply_to_message_id=%s,updated_at=now()
                        WHERE message_id=%s
                        """,
                        (reply_to_message_id, message_id),
                    )
                    message["message_type"] = "answer"
                    message["reply_to_message_id"] = uuid.UUID(reply_to_message_id)
            if reply_to_message_id:
                cursor = await connection.execute(
                    """
                    SELECT m.*,r.status AS run_status,r.waiting_question_message_id,
                           r.interaction_seq AS run_interaction_seq,
                           r.stage AS run_stage,r.run_type AS run_type,
                           r.agent_key AS run_agent_key,
                           r.request_json AS run_request_json,r.result_json AS run_result_json,
                           (SELECT max(reset.created_at) FROM assistant.messages reset
                            WHERE reset.project_id=m.project_id AND reset.channel=m.channel
                              AND reset.chat_id=m.chat_id AND reset.direction='system'
                              AND reset.message_type='conversation_reset'
                              AND reset.content_json->>'schema_version'='conversation-reset.v1'
                           ) AS latest_reset_at
                    FROM assistant.messages m JOIN assistant.runs r ON r.run_id=m.run_id
                    WHERE m.message_id=%s AND m.direction='outbound'
                      AND m.message_type IN ('question','approval')
                      AND m.project_id=%s AND m.channel=%s AND m.chat_id=%s
                      AND m.user_id IS NOT DISTINCT FROM %s
                    FOR UPDATE OF r
                    """,
                    (reply_to_message_id, project_id, channel, chat_id, user_id),
                )
                question = await cursor.fetchone()
                retired_stage_reply = bool(
                    question and single_agent_project and self._retired_stage(
                        project_id, {
                            "run_type": question["run_type"],
                            "request_json": question["run_request_json"],
                        },
                    )
                )
                closed_release_reply = bool(
                    question
                    and (question["run_request_json"] or {}).get("task_protocol")
                        == "single-agent.v2"
                    and (question["run_request_json"] or {}).get("approval_kind")
                        == "release"
                    and str(question["waiting_question_message_id"])
                        != str(question["message_id"])
                )
                current_question = False
                if question and question["message_type"] == "question":
                    result = dict(question["run_result_json"] or {})
                    current_ids = [str(item) for item in result.get("question_message_ids") or []]
                    current_question = (
                        str(question["message_id"]) in current_ids
                        if current_ids else (
                            str(question["waiting_question_message_id"])
                            == str(question["message_id"])
                            or int((question["content_json"] or {}).get("interaction_seq") or -1)
                            == int(question["run_interaction_seq"])
                        )
                    )
                    reset_at = question["latest_reset_at"]
                    current_question = current_question and (
                        reset_at is None or question["created_at"] > reset_at
                    )
                current_interaction = bool(
                    question and not retired_stage_reply
                    and question["run_id"] and question["run_status"] == "waiting_user"
                    and (
                        (
                            question["message_type"] == "approval"
                            and str(question["waiting_question_message_id"])
                            == str(question["message_id"])
                        )
                        or current_question
                    )
                )
                if before_resume is not None:
                    if not current_interaction:
                        raise PermissionError("interaction is no longer current")
                    side_effect = before_resume()
                    if isinstance(side_effect, dict):
                        content = {**content, "credential_result": json_compatible(side_effect)}
                        await connection.execute(
                            "UPDATE assistant.messages SET content_json=%s,updated_at=now() WHERE message_id=%s",
                            (Jsonb(content), message_id),
                        )
                        message["content_json"] = content
                if current_interaction:
                    decision = approval_decision(content) if question["message_type"] == "approval" else None
                    if (
                        question["message_type"] == "approval"
                        and decision is None
                        and str(content.get("text") or "").strip()
                    ):
                        decision = "revise"
                    if question["message_type"] == "approval" and decision is None:
                        decision_required = True
                    elif decision == "approve":
                        current_request = dict(question["run_request_json"] or {})
                        single_requirements = bool(
                            current_request.get("task_protocol") == "single-agent.v2"
                            and current_request.get("approval_kind") == "requirements"
                        )
                        if single_requirements:
                            task_contract_ref = dict(
                                current_request.get("task_contract_ref") or {}
                            )
                            if not task_contract_ref:
                                raise RuntimeError("Requirements approval has no Task Contract")
                            from assistant_kernel.services.document_runtime import (
                                TaskDocumentService,
                            )
                            contract = TaskDocumentService(self.settings).read(
                                project_id, task_contract_ref
                            )
                            expected = str(current_request.get("task_contract_sha256") or "")
                            if (
                                str(task_contract_ref.get("sha256") or "") != expected
                                or not list((contract.get("payload") or {}).get("requirements") or [])
                            ):
                                raise RuntimeError(
                                    "Task Contract changed after the approval card was created"
                                )
                            run = await (await connection.execute(
                                """
                                UPDATE assistant.runs SET status='queued',available_at=now(),
                                    waiting_question_message_id=NULL,ended_at=NULL,
                                    request_json=request_json || %s,
                                    result_json=COALESCE(result_json,'{}'::jsonb) || %s,
                                    executor_json=executor_json
                                      - 'config_snapshot_uri' - 'config_bundle_hash'
                                      - 'agent_config_snapshot_uri' - 'agent_snapshot_sha256'
                                      - 'resolved_plan_uri' - 'resolved_plan_sha256'
                                      - 'native_permission_profile'
                                      - 'native_permission_profile_sha256'
                                      - 'prompt_bundle_uri' - 'prompt_bundle_sha256',
                                    updated_at=now()
                                WHERE run_id=%s AND status='waiting_user' RETURNING *
                                """,
                                (
                                    Jsonb({
                                        "current_stage": "build", "task_phase": "working",
                                        "execution_plan_variant": "build",
                                        "approval_kind": None,
                                        "answer_message_id": message_id,
                                        "requirements_sha256": expected,
                                        "requirements_approval": {
                                            "message_id": message_id,
                                            "approved_document_sha256": expected,
                                            "task_contract_ref": task_contract_ref,
                                        },
                                    }),
                                    Jsonb({
                                        "approval": "approved",
                                        "approval_message_id": message_id,
                                        "transition": "requirements_to_build",
                                    }),
                                    question["run_id"],
                                ),
                            )).fetchone()
                            if run is None:
                                raise RuntimeError("Requirements Task is no longer waiting")
                            await connection.execute(
                                "UPDATE assistant.messages SET run_id=%s WHERE message_id=%s",
                                (run["run_id"], message_id),
                            )
                            await connection.execute(
                                """
                                INSERT INTO assistant.messages(
                                    message_id,project_id,run_id,channel,chat_id,user_id,
                                    direction,message_type,reply_to_message_id,content_json,
                                    delivery_status,idempotency_key
                                ) VALUES (%s,%s,%s,%s,%s,%s,'outbound','kernel_notice',%s,%s,'pending',%s)
                                ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL
                                DO NOTHING
                                """,
                                (
                                    str(uuid.uuid4()), project_id, run["run_id"], channel,
                                    chat_id, user_id, message_id,
                                    Jsonb({
                                        "text": "Requirements已批准；同一Task已切换到Build模式。",
                                        "origin": "kernel", "context_visibility": "ui_only",
                                    }),
                                    f"single-requirements-approved:{question['run_id']}:{message_id}",
                                ),
                            )
                            await connection.commit()
                            message["run_id"] = run["run_id"]
                            return {
                                "message": message, "run": run, "duplicate": False,
                                "resumed": True, "stale_reply": False,
                                "approval_decision": "approve", "decision_required": False,
                                "partial_answer": False, "correction_run": False,
                                "superseded_run_ids": [],
                            }
                        single_release = bool(
                            current_request.get("task_protocol") == "single-agent.v2"
                            and current_request.get("approval_kind") == "release"
                        )
                        stage_plan = [str(item) for item in current_request.get("stage_plan") or []]
                        stage_index = int(current_request.get("stage_index") or 0)
                        approved_refs = [
                            {
                                **dict(item), "approval_status": "approved",
                                "approval_message_id": message_id,
                            }
                            for item in (question["run_result_json"] or {}).get("artifact_refs") or []
                            if isinstance(item, dict)
                        ]
                        await connection.execute(
                            """
                            UPDATE assistant.runs
                            SET status=CASE WHEN %s THEN 'waiting_user' ELSE 'succeeded' END,
                                waiting_question_message_id=NULL,
                                result_json=COALESCE(result_json,'{}'::jsonb) || %s,
                                ended_at=CASE WHEN %s THEN NULL ELSE now() END,
                                updated_at=now()
                            WHERE run_id=%s AND status='waiting_user'
                            """,
                            (
                                single_release,
                                Jsonb({
                                    "approval": "approved", "approval_message_id": message_id,
                                    "approved_artifact_refs": json_compatible(approved_refs),
                                    **({"publication_state": "queued"} if single_release else {}),
                                }),
                                single_release,
                                question["run_id"],
                            ),
                        )
                        if stage_index + 1 < len(stage_plan):
                            next_stage = stage_plan[stage_index + 1]
                            next_run_id = str(uuid.uuid4())
                            root_id = str(current_request.get("task_root_run_id") or question["run_id"])
                            next_request = {
                                **current_request,
                                "stage_index": stage_index + 1,
                                "task_root_run_id": root_id,
                                "prior_artifacts": json_compatible(
                                    list(current_request.get("prior_artifacts") or [])
                                    + approved_refs
                                ),
                            }
                            if (
                                question["run_stage"] == "requirements"
                                or current_request.get("task_protocol") == "task.v1"
                            ) and approved_refs:
                                approved_requirement = next(
                                    (
                                        item for item in reversed(approved_refs)
                                        if "/submissions/" in str(item.get("uri") or "")
                                    ),
                                    approved_refs[-1],
                                )
                                next_request["requirements_approval"] = {
                                    "requirements_run_id": str(question["run_id"]),
                                    "approval_message_id": message_id,
                                    "artifact_uri": approved_requirement.get("uri"),
                                    "artifact_sha256": approved_requirement.get("sha256"),
                                }
                                if current_request.get("task_protocol") == "task.v1":
                                    next_request["work_order_approval"] = dict(
                                        next_request["requirements_approval"]
                                    )
                            cursor = await connection.execute(
                                """
                                INSERT INTO assistant.runs(
                                    run_id,project_id,agent_key,trigger_message_id,parent_run_id,
                                    run_type,stage,status,conversation_key,request_json,
                                    executor_json,idempotency_key
                                ) VALUES (%s,%s,%s,%s,%s,'stage',%s,'queued',%s,%s,%s,%s)
                                ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL
                                DO UPDATE SET idempotency_key=EXCLUDED.idempotency_key RETURNING *
                                """,
                                (
                                    next_run_id, project_id, f"project:{project_id}:{next_stage}",
                                    message_id, question["run_id"], next_stage,
                                    conversation_key(
                                        project_id, f"project:{project_id}:{next_stage}",
                                        str(question["channel"]), str(question["chat_id"]),
                                    ),
                                    Jsonb(next_request),
                                    Jsonb(stage_executor(next_request, next_stage)),
                                    f"task-stage:{root_id}:{stage_index + 1}:{next_stage}",
                                ),
                            )
                            run = await cursor.fetchone()
                            await connection.execute(
                                "UPDATE assistant.messages SET run_id=%s WHERE message_id=%s",
                                (run["run_id"], message_id),
                            )
                            await connection.execute(
                                """
                                INSERT INTO assistant.messages(
                                    message_id,project_id,run_id,channel,chat_id,user_id,direction,
                                    message_type,reply_to_message_id,content_json,delivery_status,
                                    idempotency_key
                                ) VALUES (%s,%s,%s,%s,%s,%s,'outbound','kernel_notice',%s,%s,'pending',%s)
                                ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL
                                DO NOTHING
                                """,
                                (
                                    str(uuid.uuid4()), project_id, run["run_id"], channel,
                                    chat_id, user_id, message_id,
                                    Jsonb({
                                        "text": (
                                            f"已批准 {question['run_stage'] or 'work_order'}，已创建下一阶段 "
                                            f"{next_stage}。\nRun ID：{run['run_id']}\n"
                                            f"当前状态：{run['status']}"
                                        ),
                                        "origin": "kernel",
                                        "context_visibility": "ui_only",
                                        "transition": {
                                            "from_run_id": str(question["run_id"]),
                                            "from_stage": str(question["run_stage"] or "plan"),
                                            "to_run_id": str(run["run_id"]),
                                            "to_stage": next_stage,
                                        },
                                    }),
                                    f"stage-transition:{question['run_id']}:{message_id}:{next_stage}",
                                ),
                            )
                        else:
                            publish_run = None
                            candidate = next((
                                    item for item in reversed(approved_refs)
                                    if item.get("kind") == "candidate_manifest"
                                    or str(item.get("uri") or "").endswith("/candidate/manifest.json")
                                ), None)
                            if single_release and candidate is None:
                                raise ValueError("release approval has no candidate manifest")
                            if (
                                question["run_stage"] == "review"
                                or (
                                    current_request.get("task_protocol") == "single-agent.v2"
                                    and current_request.get("approval_kind") == "release"
                                )
                            ) and (
                                candidate is not None
                                or current_request.get("task_kind") == "skill_change"
                            ):
                                if candidate is None:
                                    raise ValueError("approved Skill Review has no candidate manifest")
                                publish_request = {
                                    **current_request,
                                    "candidate_manifest_uri": candidate["uri"],
                                    "candidate_bundle_uri": str(
                                        (current_request.get("sealed_candidate_bundle") or {}).get("uri")
                                        or (current_request.get("sealed_candidate_bundle") or {}).get("bundle_uri")
                                        or ""
                                    ) or None,
                                    "candidate_bundle_file_sha256": (
                                        current_request.get("sealed_candidate_bundle") or {}
                                    ).get("sha256"),
                                    "implementation_run_id": candidate.get("producer_run_id"),
                                    "review_run_id": str(question["run_id"]),
                                    "approval_message_id": message_id,
                                    "approved_artifacts": approved_refs,
                                }
                                target_skill = str(
                                    (current_request.get("capability_change") or {}).get("target_skill") or ""
                                )
                                publish_kind = "skill" if target_skill else "files"
                                publish_run = await (
                                    await connection.execute(
                                        """
                                        INSERT INTO assistant.runs(
                                            run_id,project_id,agent_key,trigger_message_id,parent_run_id,
                                            run_type,stage,status,conversation_key,request_json,
                                            executor_json,idempotency_key
                                        ) VALUES (%s,%s,%s,%s,%s,'publish','publish','queued',%s,%s,'{}'::jsonb,%s)
                                        ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL
                                        DO UPDATE SET idempotency_key=EXCLUDED.idempotency_key RETURNING *
                                        """,
                                        (
                                            str(uuid.uuid4()), project_id,
                                            f"project:{project_id}:publisher", message_id,
                                            question["run_id"], f"publish:{project_id}:{target_skill or 'files'}",
                                            Jsonb(json_compatible(publish_request)),
                                            f"{publish_kind}-publish:{question['run_id']}:{message_id}",
                                        ),
                                    )
                                ).fetchone()
                            if single_release:
                                if publish_run is None:
                                    raise RuntimeError("release approval did not create a Publisher Run")
                                await connection.execute(
                                    """
                                    UPDATE assistant.runs SET
                                        request_json=request_json || %s,
                                        updated_at=now()
                                    WHERE run_id=%s AND status='waiting_user'
                                    """,
                                    (
                                        Jsonb({
                                            "task_phase": "publishing",
                                            "publish_run_id": str(publish_run["run_id"]),
                                        }),
                                        question["run_id"],
                                    ),
                                )
                            run = publish_run or await (
                                await connection.execute(
                                    "SELECT * FROM assistant.runs WHERE run_id=%s",
                                    (question["run_id"],),
                                )
                            ).fetchone()
                            await connection.execute(
                                "UPDATE assistant.messages SET run_id=%s WHERE message_id=%s",
                                (run["run_id"], message_id),
                            )
                            if publish_run is None:
                                await connection.execute(
                                    """
                                    INSERT INTO assistant.messages(
                                        message_id,project_id,run_id,channel,chat_id,user_id,direction,
                                        message_type,reply_to_message_id,content_json,delivery_status,
                                        idempotency_key
                                    ) VALUES (%s,%s,%s,%s,%s,%s,'outbound','reply',%s,%s,'pending',%s)
                                    ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL
                                    DO NOTHING
                                    """,
                                    (
                                        str(uuid.uuid4()), project_id, question["run_id"], channel,
                                        chat_id, user_id, message_id,
                                        Jsonb({
                                            "text": "已记录最终批准，任务阶段链已完成。",
                                            "origin": "kernel",
                                            "context_visibility": "ui_only",
                                        }),
                                        f"approval-receipt:{question['run_id']}:{message_id}",
                                    ),
                                )
                        resumed = True
                    elif (
                        decision == "revise"
                        and (question["run_request_json"] or {}).get("task_protocol")
                            == "single-agent.v2"
                        and (question["run_request_json"] or {}).get("approval_kind")
                            == "requirements"
                    ):
                        run = await (await connection.execute(
                            """
                            UPDATE assistant.runs SET status='queued',available_at=now(),
                                waiting_question_message_id=NULL,ended_at=NULL,
                                request_json=request_json || %s,
                                result_json=COALESCE(result_json,'{}'::jsonb) || %s,
                                updated_at=now()
                            WHERE run_id=%s AND status='waiting_user' RETURNING *
                            """,
                            (
                                Jsonb({
                                    "task_phase": "working", "approval_kind": None,
                                    "current_stage": "requirements",
                                    "execution_plan_variant": "requirements",
                                    "answer_message_id": message_id,
                                    "requirements_feedback": json_compatible(content),
                                    "requirements_approval": None,
                                }),
                                Jsonb({
                                    "approval": "revision_requested",
                                    "approval_message_id": message_id,
                                }),
                                question["run_id"],
                            ),
                        )).fetchone()
                        if run is None:
                            raise RuntimeError("Requirements Task is no longer waiting")
                        await connection.execute(
                            "UPDATE assistant.messages SET run_id=%s WHERE message_id=%s",
                            (run["run_id"], message_id),
                        )
                        resumed = True
                    elif decision == "revise" and question["run_stage"] == "review":
                        current_request = dict(question["run_request_json"] or {})
                        stage_plan = [str(item) for item in current_request.get("stage_plan") or []]
                        target_stage = review_rework_stage(stage_plan)
                        round_number = int(current_request.get("stage_round") or 1) + 1
                        await connection.execute(
                            """
                            UPDATE assistant.runs SET status='succeeded',waiting_question_message_id=NULL,
                                result_json=COALESCE(result_json,'{}'::jsonb) || %s,
                                ended_at=now(),updated_at=now()
                            WHERE run_id=%s AND status='waiting_user'
                            """,
                            (
                                Jsonb({"approval": "revision_requested",
                                       "approval_message_id": message_id}),
                                question["run_id"],
                            ),
                        )
                        root_id = str(current_request.get("task_root_run_id") or question["run_id"])
                        next_request = {
                            **current_request,
                            "stage_index": stage_plan.index(target_stage),
                            "stage_round": round_number,
                            "review_feedback": {
                                "source_review_run_id": str(question["run_id"]),
                                "user_revision_request": json_compatible(content),
                            },
                        }
                        run = await (
                            await connection.execute(
                                """
                                INSERT INTO assistant.runs(
                                    run_id,project_id,agent_key,trigger_message_id,parent_run_id,
                                    run_type,stage,status,conversation_key,request_json,
                                    executor_json,idempotency_key
                                ) VALUES (%s,%s,%s,%s,%s,'stage',%s,'queued',%s,%s,%s,%s)
                                ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL
                                DO UPDATE SET idempotency_key=EXCLUDED.idempotency_key RETURNING *
                                """,
                                (
                                    str(uuid.uuid4()), project_id,
                                    f"project:{project_id}:{target_stage}", message_id,
                                    question["run_id"], target_stage,
                                    conversation_key(
                                        project_id, f"project:{project_id}:{target_stage}",
                                        str(question["channel"]), str(question["chat_id"]),
                                    ),
                                    Jsonb(json_compatible(next_request)),
                                    Jsonb(stage_executor(next_request, target_stage)),
                                    f"task-rework:{root_id}:{round_number}:{target_stage}",
                                ),
                            )
                        ).fetchone()
                        await connection.execute(
                            "UPDATE assistant.messages SET run_id=%s WHERE message_id=%s",
                            (run["run_id"], message_id),
                        )
                        resumed = True
                    elif decision == "reject":
                        await connection.execute(
                            """
                            UPDATE assistant.runs SET status='cancelled',waiting_question_message_id=NULL,
                                result_json=COALESCE(result_json,'{}'::jsonb) || %s,
                                ended_at=now(),updated_at=now()
                            WHERE run_id=%s AND status='waiting_user'
                            """,
                            (Jsonb({"approval": "rejected", "approval_message_id": message_id}), question["run_id"]),
                        )
                        run = await (
                            await connection.execute(
                                "SELECT * FROM assistant.runs WHERE run_id=%s", (question["run_id"],)
                            )
                        ).fetchone()
                        await connection.execute(
                            "UPDATE assistant.messages SET run_id=%s WHERE message_id=%s",
                            (question["run_id"], message_id),
                        )
                        await connection.execute(
                            """
                            INSERT INTO assistant.messages(
                                message_id,project_id,run_id,channel,chat_id,user_id,direction,
                                message_type,reply_to_message_id,content_json,delivery_status,
                                idempotency_key
                            ) VALUES (%s,%s,%s,%s,%s,%s,'outbound','reply',%s,%s,'pending',%s)
                            ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL
                            DO NOTHING
                            """,
                            (
                                str(uuid.uuid4()), project_id, question["run_id"], channel,
                                chat_id, user_id, message_id,
                                Jsonb({
                                    "text": "已记录拒绝决定，任务阶段链已停止。",
                                    "origin": "kernel",
                                    "context_visibility": "ui_only",
                                }),
                                f"rejection-receipt:{question['run_id']}:{message_id}",
                            ),
                        )
                    elif not decision_required:
                        answer_payload: dict[str, Any] = json_compatible({
                            key: value for key, value in content.items()
                            if key not in {"raw", "delivery", "correlation"}
                        })
                        if question["message_type"] == "question":
                            question_content = dict(question["content_json"] or {})
                            question_id = str(question_content.get("question_id") or "").strip()
                            legacy_questions = [
                                dict(item) for item in question_content.get("questions") or []
                            ]
                            if not question_id and len(legacy_questions) == 1:
                                question_id = str(
                                    legacy_questions[0].get("question_id") or ""
                                ).strip()
                            if not question_id:
                                raise ValueError("the replied message has no stable question_id")
                            allowed = {
                                str(item)
                                for item in (question["run_result_json"] or {}).get("question_ids") or []
                            }
                            required = {
                                str(item)
                                for item in (question["run_result_json"] or {}).get(
                                    "required_question_ids"
                                ) or []
                            }
                            if not allowed:
                                allowed = {
                                    str(item.get("question_id")) for item in legacy_questions
                                    if item.get("question_id")
                                }
                                required = {
                                    str(item.get("question_id")) for item in legacy_questions
                                    if item.get("question_id") and item.get("required", True)
                                }
                            if question_id not in allowed:
                                raise ValueError("the replied question is not part of the current interaction")
                            provided = (
                                content.get("answers")
                                or (content.get("metadata") or {}).get("answers")
                            )
                            if isinstance(provided, dict):
                                unexpected = set(str(key) for key in provided) - allowed
                                if unexpected:
                                    raise ValueError("answer contains a question_id from another interaction")
                                normalized = {str(key): value for key, value in provided.items()}
                            else:
                                normalized = {
                                    question_id: content.get("text")
                                    if "text" in content else json_compatible(content)
                                }
                            current_request = dict(question["run_request_json"] or {})
                            merged = dict(current_request.get("answer_state") or {})
                            merged.update(normalized)
                            unanswered = {
                                item for item in required
                                if item not in merged
                                or merged[item] is None
                                or merged[item] == ""
                            }
                            if unanswered:
                                await connection.execute(
                                    """
                                    UPDATE assistant.runs
                                    SET request_json=jsonb_set(request_json,'{answer_state}',%s),
                                        updated_at=now()
                                    WHERE run_id=%s AND status='waiting_user'
                                    """,
                                    (Jsonb(json_compatible(merged)), question["run_id"]),
                                )
                                await connection.execute(
                                    "UPDATE assistant.messages SET run_id=%s WHERE message_id=%s",
                                    (question["run_id"], message_id),
                                )
                                run = await (
                                    await connection.execute(
                                        "SELECT * FROM assistant.runs WHERE run_id=%s",
                                        (question["run_id"],),
                                    )
                                ).fetchone()
                                partial_answer = True
                            else:
                                answer_payload = {"answers": merged}
                        if not partial_answer:
                            cursor = await connection.execute(
                                """
                                UPDATE assistant.runs
                                SET status='queued',available_at=now(),waiting_question_message_id=NULL,
                                    request_json=jsonb_set(
                                        request_json,'{answer_history}',
                                        COALESCE(request_json->'answer_history','[]'::jsonb) || %s
                                    ) || %s,updated_at=now()
                                WHERE run_id=%s AND status='waiting_user' RETURNING *
                                """,
                                (
                                    Jsonb([{"answer_message_id": message_id, "answer": answer_payload}]),
                                    Jsonb({"answer_message_id": message_id, "answer": answer_payload}),
                                    question["run_id"],
                                ),
                            )
                            run = await cursor.fetchone()
                            if run:
                                resumed = True
                                await connection.execute(
                                    "UPDATE assistant.messages SET run_id=%s WHERE message_id=%s",
                                    (run["run_id"], message_id),
                                )
                else:
                    stale_reply = True

            detached_by_reset = bool(
                stale_reply and question is not None
                and question["message_type"] == "question"
                and question["latest_reset_at"] is not None
                and question["created_at"] <= question["latest_reset_at"]
            )
            if detached_by_reset and not retired_stage_reply and not closed_release_reply:
                await connection.execute(
                    """
                    INSERT INTO assistant.messages(
                        message_id,project_id,channel,chat_id,user_id,direction,
                        message_type,reply_to_message_id,content_json,delivery_status,
                        idempotency_key
                    ) VALUES (%s,%s,%s,%s,%s,'outbound','reply',%s,%s,'pending',%s)
                    """,
                    (
                        str(uuid.uuid4()), project_id, channel, chat_id, user_id,
                        message_id,
                        Jsonb({
                            "text": (
                                "这条旧问题已失效。请回复最新的问题消息；若需恢复旧任务，"
                                f"先发送 /resume {question['run_id']}。"
                            ),
                            "origin": "kernel",
                            "context_visibility": "ui_only",
                        }),
                        f"expired-question-reply:{message_id}",
                    ),
                )
            if stale_reply and question is not None and retired_stage_reply:
                await connection.execute(
                    """
                    INSERT INTO assistant.messages(
                        message_id,project_id,channel,chat_id,user_id,direction,
                        message_type,reply_to_message_id,content_json,delivery_status,
                        idempotency_key
                    ) VALUES (%s,%s,%s,%s,%s,'outbound','reply',%s,%s,'pending',%s)
                    ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL
                    DO NOTHING
                    """,
                    (
                        str(uuid.uuid4()), project_id, channel, chat_id, user_id,
                        message_id,
                        Jsonb({
                            "text": "旧阶段问题已失效；请发送新的任务请求。历史记录保留。",
                            "origin": "kernel", "context_visibility": "ui_only",
                        }),
                        f"retired-stage-reply:{message_id}",
                    ),
                )
            if stale_reply and question is not None and closed_release_reply:
                await connection.execute(
                    """
                    INSERT INTO assistant.messages(
                        message_id,project_id,channel,chat_id,user_id,direction,
                        message_type,reply_to_message_id,content_json,delivery_status,
                        idempotency_key
                    ) VALUES (%s,%s,%s,%s,%s,'outbound','reply',%s,%s,'pending',%s)
                    ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL
                    DO NOTHING
                    """,
                    (
                        str(uuid.uuid4()), project_id, channel, chat_id, user_id,
                        message_id,
                        Jsonb({
                            "text": "这张发布审批卡已处理；请查询当前Task或Publisher状态。",
                            "origin": "kernel", "context_visibility": "ui_only",
                        }),
                        f"closed-release-reply:{message_id}",
                    ),
                )
            if (
                stale_reply and question is not None and not detached_by_reset
                and not retired_stage_reply and not closed_release_reply
            ):
                correction_agent = (
                    "system-butler" if project_id == "system"
                    else f"project:{project_id}:manager"
                )
                question_content = dict(question["content_json"] or {})
                correction_request = {
                    "message_id": message_id,
                    "correction": {
                        "kind": "stale_question_answer",
                        "original_run_id": str(question["run_id"]),
                        "original_run_status": str(question["run_status"]),
                        "question_message_id": str(question["message_id"]),
                        "question_id": str(question_content.get("question_id") or ""),
                    },
                }
                run = await (
                    await connection.execute(
                        """
                        INSERT INTO assistant.runs(
                            run_id,project_id,agent_key,trigger_message_id,parent_run_id,
                            run_type,status,conversation_key,request_json,executor_json,
                            idempotency_key
                        ) VALUES (%s,%s,%s,%s,%s,'chat','queued',%s,%s,%s,%s)
                        ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL
                        DO UPDATE SET idempotency_key=EXCLUDED.idempotency_key RETURNING *
                        """,
                        (
                            run_id, project_id, correction_agent, message_id,
                            question["run_id"],
                            conversation_key(project_id, correction_agent, channel, chat_id),
                            Jsonb(json_compatible(correction_request)),
                            Jsonb(json_compatible({
                                **dict(executor or {}),
                                "scheduling_lane": "conversation",
                            })),
                            f"run:correction:{message_id}",
                        ),
                    )
                ).fetchone()
                await connection.execute(
                    "UPDATE assistant.messages SET run_id=%s WHERE message_id=%s",
                    (run["run_id"], message_id),
                )
                correction_run = True

            if not resumed and not stale_reply and not reply_to_message_id:
                lane = scheduling_lane("chat", agent_key)
                if (
                    message_type == "text"
                    and agent_key == f"project:{project_id}:manager"
                ):
                    # Only PM-manager texts can open a second Task; supervisor
                    # and operator conversation messages always flow through.
                    active_task = await (
                        await connection.execute(
                            """
                            SELECT * FROM assistant.runs
                            WHERE project_id=%s
                              AND request_json->>'task_protocol'='single-agent.v2'
                              AND (
                                status IN ('queued','running','reconciling')
                                OR (status IN ('waiting_user','waiting_supervisor','waiting_children','blocked') AND
                                    request_json->>'task_phase' IN
                                    ('publishing','publication_reconciling'))
                              )
                            ORDER BY created_at,run_id LIMIT 1
                            FOR UPDATE
                            """,
                            (project_id,),
                        )
                    ).fetchone()
                    if active_task is not None:
                        await connection.execute(
                            "UPDATE assistant.messages SET run_id=%s,content_json=%s,updated_at=now() "
                            "WHERE message_id=%s",
                            (
                                active_task["run_id"],
                                Jsonb({
                                    **json_compatible(content),
                                    "deferred_task_message": True,
                                    "task_id": str(active_task["run_id"]),
                                }),
                                message_id,
                            ),
                        )
                        notice = await (
                            await connection.execute(
                                """
                                INSERT INTO assistant.messages(
                                    message_id,project_id,run_id,channel,chat_id,user_id,
                                    direction,message_type,reply_to_message_id,content_json,
                                    delivery_status,idempotency_key
                                ) VALUES (%s,%s,%s,%s,%s,%s,'outbound','kernel_notice',%s,%s,'pending',%s)
                                RETURNING *
                                """,
                                (
                                    str(uuid.uuid4()), project_id, active_task["run_id"],
                                    channel, chat_id, user_id, message_id,
                                    Jsonb({
                                        "text": (
                                            "当前Task正在执行；本消息已记录但不会中断或创建新Task。"
                                            "请等待当前Turn结束；需要改变目标时先取消当前Task。\n"
                                            f"Task ID：{active_task['run_id']}\n"
                                            f"状态：{active_task['status']}"
                                        ),
                                        "origin": "kernel",
                                        "context_visibility": "ui_only",
                                    }),
                                    f"single-task-busy:{message_id}",
                                ),
                            )
                        ).fetchone()
                        await connection.commit()
                        return {
                            "message": await (
                                await connection.execute(
                                    "SELECT * FROM assistant.messages WHERE message_id=%s",
                                    (message_id,),
                                )
                            ).fetchone(),
                            "run": None, "duplicate": False, "resumed": False,
                            "active_task": active_task, "outbound": notice,
                            "superseded_run_ids": [],
                        }
                input_fingerprint = canonical_sha256({
                    "text": str(content.get("text") or "").strip(),
                    "attachments": list(
                        (content.get("discord") or {}).get("attachments") or []
                    ),
                })
                if (
                    lane == "conversation" and message_type == "text"
                    and not agent_key.endswith(":supervisor")
                ):
                    duplicate_cursor = await connection.execute(
                        """
                        UPDATE assistant.runs prior
                        SET status='cancelled',ended_at=now(),updated_at=now(),
                            error_json=COALESCE(error_json,'{}'::jsonb) || %s
                        FROM assistant.messages trigger
                        WHERE prior.trigger_message_id=trigger.message_id
                          AND prior.project_id=%s AND prior.agent_key=%s
                          AND prior.conversation_key=%s
                          AND prior.run_type='chat' AND prior.status='queued'
                          AND trigger.user_id IS NOT DISTINCT FROM %s
                          AND prior.request_json->>'input_fingerprint'=%s
                        RETURNING prior.run_id
                        """,
                        (
                            Jsonb({
                                "type": "SupersededDuplicate",
                                "message": "a newer identical conversation input replaced this queued Run",
                                "superseded_by_message_id": message_id,
                            }),
                            project_id, agent_key,
                            conversation_key(project_id, agent_key, channel, chat_id),
                            user_id, input_fingerprint,
                        ),
                    )
                    superseded_run_ids = [
                        str(item["run_id"]) for item in await duplicate_cursor.fetchall()
                    ]
                if not agent_key.endswith(":supervisor"):
                    cursor = await connection.execute(
                        """
                        INSERT INTO assistant.runs(
                        run_id,project_id,agent_key,trigger_message_id,run_type,status,
                            conversation_key,request_json,executor_json,idempotency_key
                        ) VALUES (%s,%s,%s,%s,'chat','queued',%s,%s,%s,%s)
                        RETURNING *
                        """,
                        (
                            run_id, project_id, agent_key, message_id,
                            conversation_key(project_id, agent_key, channel, chat_id),
                            Jsonb({
                                "message_id": message_id,
                                "input_fingerprint": input_fingerprint,
                            }),
                            Jsonb(json_compatible({
                                **dict(executor or {}), "scheduling_lane": lane,
                            })),
                            f"run:message:{message_id}",
                        ),
                    )
                    run = await cursor.fetchone()
                    await connection.execute(
                        "UPDATE assistant.messages SET run_id=%s WHERE message_id=%s",
                        (run_id, message_id),
                    )
            await connection.commit()
            message["run_id"] = run["run_id"] if run else None
            return {
                "message": message, "run": run, "duplicate": False,
                "resumed": resumed, "stale_reply": stale_reply,
                "approval_decision": decision, "decision_required": decision_required,
                "partial_answer": partial_answer,
                "correction_run": correction_run,
                "superseded_run_ids": superseded_run_ids,
            }

    async def record_question_choice(
        self, *, project_id: str, channel: str, chat_id: str, user_id: str,
        external_event_id: str, question_message_id: str, run_id: str,
        question_id: str, interaction_seq: int, choice_id: str,
    ) -> dict[str, Any]:
        """Accept one card choice under the waiting Run lock; never make a correction Run."""
        if not user_id:
            raise PermissionError("a card choice requires an identified user")
        async with self.database.connection() as connection:
            question = await (await connection.execute(
                """
                SELECT m.*,r.status AS run_status,r.request_json AS run_request_json,
                       r.result_json AS run_result_json,r.interaction_seq AS run_interaction_seq,
                       r.waiting_question_message_id
                FROM assistant.messages m JOIN assistant.runs r ON r.run_id=m.run_id
                WHERE m.message_id=%s AND m.direction='outbound'
                  AND m.message_type='question' AND m.project_id=%s
                  AND m.channel=%s AND m.chat_id=%s
                  AND m.user_id IS NOT DISTINCT FROM %s
                FOR UPDATE OF r
                """,
                (question_message_id, project_id, channel, chat_id, user_id),
            )).fetchone()
            if question is None or str(question["run_id"]) != run_id:
                raise PermissionError("card choice does not belong to this Project and Run")
            body = dict(question["content_json"] or {})
            if (
                str(body.get("question_id") or "") != question_id
                or int(body.get("interaction_seq") or -1) != interaction_seq
            ):
                raise PermissionError("card choice question identity differs")
            delivery = dict(body.get("delivery") or {})
            if question["delivery_status"] != "sent" or delivery.get("msg_type") != "interactive":
                raise PermissionError("card choice was not delivered as an interactive card")
            options = [dict(item) for item in body.get("options") or [] if isinstance(item, dict)]
            selected = next(
                (item for item in options if str(item.get("id") or "") == choice_id), None
            )
            if selected is None:
                raise ValueError("card choice is not an option of this question")
            answer_text = str(selected.get("label") or "").strip()
            if not answer_text:
                raise ValueError("card choice has no answer label")
            payload = {
                "schema_version": "choice-answer.v1",
                "question_id": question_id, "choice_id": choice_id,
                "text": answer_text, "choice_status": "received",
            }
            incoming = await (await connection.execute(
                """
                INSERT INTO assistant.messages(
                    message_id,project_id,channel,chat_id,user_id,direction,
                    message_type,external_event_id,reply_to_message_id,
                    content_json,delivery_status,idempotency_key
                ) VALUES (%s,%s,%s,%s,%s,'inbound','action',%s,%s,%s,'received',%s)
                ON CONFLICT (channel,external_event_id)
                    WHERE external_event_id IS NOT NULL DO NOTHING
                RETURNING *
                """,
                (
                    str(uuid.uuid4()), project_id, channel, chat_id, user_id,
                    external_event_id, question_message_id, Jsonb(payload),
                    f"inbound:{channel}:{external_event_id}",
                ),
            )).fetchone()
            if incoming is None:
                existing = await (await connection.execute(
                    "SELECT * FROM assistant.messages WHERE channel=%s AND external_event_id=%s",
                    (channel, external_event_id),
                )).fetchone()
                prior = dict(existing["content_json"] or {}) if existing else {}
                if (
                    existing is None or str(existing["project_id"]) != project_id
                    or str(existing["chat_id"]) != chat_id
                    or str(existing["user_id"]) != user_id
                    or str(existing["reply_to_message_id"]) != question_message_id
                    or prior.get("choice_id") != choice_id
                ):
                    raise ValueError("card event ID was reused with different content")
                await connection.commit()
                return {
                    "choice_status": str(prior.get("choice_status") or "recorded"),
                    "duplicate": True, "run_id": run_id,
                }
            result = dict(question["run_result_json"] or {})
            current_ids = [str(item) for item in result.get("question_message_ids") or []]
            if not current_ids and question.get("waiting_question_message_id"):
                current_ids = [str(question["waiting_question_message_id"])]
            latest_reset = await (await connection.execute(
                """
                SELECT max(created_at) AS created_at FROM assistant.messages
                WHERE project_id=%s AND channel=%s AND chat_id=%s
                  AND direction='system' AND message_type='conversation_reset'
                  AND content_json->>'schema_version'='conversation-reset.v1'
                """,
                (project_id, channel, chat_id),
            )).fetchone()
            reset_at = latest_reset["created_at"] if latest_reset else None
            request = dict(question["run_request_json"] or {})
            answer_state = dict(request.get("answer_state") or {})
            if (
                question["run_status"] != "waiting_user"
                or str(question["message_id"]) not in current_ids
                or int(question["run_interaction_seq"]) != interaction_seq
                or (reset_at is not None and question["created_at"] <= reset_at)
            ):
                status = "expired"
            elif (
                question_id in answer_state
                and answer_state[question_id] is not None
                and answer_state[question_id] != ""
            ):
                status = "already_answered"
            else:
                status = "recorded"
            if status != "recorded":
                await connection.execute(
                    "UPDATE assistant.messages SET content_json=content_json || %s WHERE message_id=%s",
                    (Jsonb({"choice_status": status}), incoming["message_id"]),
                )
                await connection.commit()
                return {"choice_status": status, "duplicate": False, "run_id": run_id}
            if question_id not in {str(item) for item in result.get("question_ids") or []}:
                raise ValueError("card choice is not part of the current interaction")
            merged = {**answer_state, question_id: answer_text}
            required = {str(item) for item in result.get("required_question_ids") or []}
            unanswered = {item for item in required if not merged.get(item)}
            if unanswered:
                await connection.execute(
                    """
                    UPDATE assistant.runs
                    SET request_json=jsonb_set(request_json,'{answer_state}',%s),
                        updated_at=now()
                    WHERE run_id=%s AND status='waiting_user'
                    """,
                    (Jsonb(merged), run_id),
                )
                resumed = False
            else:
                answer_payload = {"answers": merged}
                updated = await (await connection.execute(
                    """
                    UPDATE assistant.runs
                    SET status='queued',available_at=now(),waiting_question_message_id=NULL,
                        request_json=jsonb_set(
                            request_json,'{answer_history}',
                            COALESCE(request_json->'answer_history','[]'::jsonb) || %s
                        ) || %s,updated_at=now()
                    WHERE run_id=%s AND status='waiting_user' RETURNING run_id
                    """,
                    (
                        Jsonb([{"answer_message_id": str(incoming["message_id"]),
                                "answer": answer_payload}]),
                        Jsonb({"answer_message_id": str(incoming["message_id"]),
                               "answer": answer_payload}), run_id,
                    ),
                )).fetchone()
                if updated is None:
                    raise RuntimeError("waiting Run changed while accepting card choice")
                resumed = True
            await connection.execute(
                """
                UPDATE assistant.messages
                SET message_type='answer',run_id=%s,
                    content_json=content_json || %s,updated_at=now()
                WHERE message_id=%s
                """,
                (run_id, Jsonb({"choice_status": "recorded"}), incoming["message_id"]),
            )
            await connection.commit()
            return {
                "choice_status": "recorded", "duplicate": False,
                "resumed": resumed, "partial_answer": not resumed, "run_id": run_id,
            }

    async def get_message(self, message_id: str) -> dict[str, Any] | None:
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "SELECT * FROM assistant.messages WHERE message_id=%s", (message_id,)
            )
            return await cursor.fetchone()

    async def delivery_reply_external_id(self, outbound_message_id: str) -> str | None:
        """Resolve the Feishu message that an outbound message should quote."""
        async with self.database.connection() as connection:
            parent = await (
                await connection.execute(
                    """
                    SELECT source.external_message_id,source.message_type,source.content_json
                    FROM assistant.messages outbound
                    JOIN assistant.messages source
                      ON source.message_id=outbound.reply_to_message_id
                    WHERE outbound.message_id=%s AND outbound.direction='outbound'
                    """,
                    (outbound_message_id,),
                )
            ).fetchone()
            if parent is None:
                return None
            if parent["external_message_id"]:
                return str(parent["external_message_id"])
            if parent["message_type"] != "handoff":
                return None
            source_message_id = str(
                (parent["content_json"] or {}).get("source_message_id") or ""
            )
            if not source_message_id:
                return None
            source = await (
                await connection.execute(
                    "SELECT external_message_id FROM assistant.messages WHERE message_id=%s",
                    (source_message_id,),
                )
            ).fetchone()
            return str(source["external_message_id"]) if source and source["external_message_id"] else None

    async def list_messages(
        self, project_id: str, channel: str | None = None,
        chat_id: str | None = None, limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses = ["project_id=%s"]
        values: list[Any] = [project_id]
        if channel:
            clauses.append("channel=%s")
            values.append(channel)
        if chat_id:
            clauses.append("chat_id=%s")
            values.append(chat_id)
        values.append(min(max(limit, 1), 500))
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                f"SELECT * FROM assistant.messages WHERE {' AND '.join(clauses)} "
                "ORDER BY created_at DESC,message_id DESC LIMIT %s",
                values,
            )
            return list(await cursor.fetchall())

    async def list_recent_history(
        self, *, project_id: str, lookback_days: int = 365,
        limit: int = 50, cursor: str | None = None,
        channel: str | None = None, chat_id: str | None = None,
        include_system: bool = False,
    ) -> dict[str, Any]:
        """Return a bounded, Project-scoped recent transcript without semantic search."""
        if not 1 <= lookback_days <= 3650:
            raise ValueError("lookback_days must be between 1 and 3650")
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        if (channel is None) != (chat_id is None):
            raise ValueError("channel and chat_id must be supplied together")
        before_at, before_id = _decode_history_cursor(cursor)
        clauses = [
            "project_id=%s",
            "created_at >= now() - (%s * interval '1 day')",
        ]
        values: list[Any] = [project_id, lookback_days]
        if not include_system:
            clauses.append("direction <> 'system'")
        if channel is not None and chat_id is not None:
            clauses.extend(["channel=%s", "chat_id=%s"])
            values.extend([channel, chat_id])
        clauses.append(
            "(%s::timestamptz IS NULL OR "
            "(created_at,message_id) < (%s::timestamptz,%s::uuid))"
        )
        values.extend([before_at, before_at, before_id])
        values.append(limit + 1)
        async with self.database.connection() as connection:
            rows = list(await (
                await connection.execute(
                    f"SELECT * FROM assistant.messages WHERE {' AND '.join(clauses)} "
                    "ORDER BY created_at DESC,message_id DESC LIMIT %s",
                    values,
                )
            ).fetchall())
        has_more = len(rows) > limit
        selected = rows[:limit]
        items = []
        for row in selected:
            content = dict(row["content_json"] or {})
            text = _history_text(content)
            items.append({
                "message_id": str(row["message_id"]),
                "session_id": str(row["session_id"]) if row.get("session_id") else None,
                "run_id": str(row["run_id"]) if row.get("run_id") else None,
                "channel": str(row["channel"]),
                "chat_id": str(row["chat_id"]),
                "direction": str(row["direction"]),
                "message_type": str(row["message_type"]),
                "reply_to_message_id": (
                    str(row["reply_to_message_id"])
                    if row.get("reply_to_message_id") else None
                ),
                "text": text[:4000],
                "text_truncated": len(text) > 4000,
                "created_at": row["created_at"].isoformat(),
            })
        next_cursor = None
        if has_more and selected:
            last = selected[-1]
            next_cursor = _encode_history_cursor(
                last["created_at"], str(last["message_id"])
            )
        return {
            "project_id": project_id,
            "lookback_days": lookback_days,
            "items": items,
            "next_cursor": next_cursor,
        }

    async def list_messages_through(
        self, project_id: str, channel: str, chat_id: str,
        boundary_message_id: str, limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return a causal history ending at one persisted input message."""
        async with self.database.connection() as connection:
            boundary = await (
                await connection.execute(
                    """
                    SELECT message_id,project_id,channel,chat_id,created_at
                    FROM assistant.messages WHERE message_id=%s
                    """,
                    (boundary_message_id,),
                )
            ).fetchone()
            if boundary is None:
                raise ValueError("history boundary message was not found")
            if (
                str(boundary["project_id"]) != project_id
                or str(boundary["channel"]) != channel
                or str(boundary["chat_id"]) != chat_id
            ):
                raise ValueError("history boundary belongs to another conversation")
            cursor = await connection.execute(
                """
                WITH reset AS (
                  SELECT max(created_at) AS created_at
                  FROM assistant.messages
                  WHERE project_id=%s AND channel=%s AND chat_id=%s
                    AND direction='system' AND message_type='conversation_reset'
                    AND content_json->>'schema_version'='conversation-reset.v1'
                    AND created_at<=%s
                )
                SELECT m.* FROM assistant.messages m,reset
                WHERE m.project_id=%s AND m.channel=%s AND m.chat_id=%s
                  AND (m.created_at<%s OR m.message_id=%s)
                  AND (reset.created_at IS NULL OR m.created_at>reset.created_at)
                ORDER BY m.created_at DESC,m.message_id DESC LIMIT %s
                """,
                (
                    project_id, channel, chat_id, boundary["created_at"],
                    project_id, channel, chat_id, boundary["created_at"],
                    boundary_message_id, min(max(limit, 1), 500),
                ),
            )
            return list(await cursor.fetchall())

    async def current_conversation_goal(
        self, project_id: str, channel: str, chat_id: str,
    ) -> dict[str, Any] | None:
        """Return the latest explicit goal after the most recent /new boundary."""
        async with self.database.connection() as connection:
            row = await (
                await connection.execute(
                    """
                    WITH reset AS (
                      SELECT max(created_at) AS created_at
                      FROM assistant.messages
                      WHERE project_id=%s AND channel=%s AND chat_id=%s
                        AND direction='system' AND message_type='conversation_reset'
                        AND content_json->>'schema_version'='conversation-reset.v1'
                    )
                    SELECT m.* FROM assistant.messages m,reset
                    WHERE m.project_id=%s AND m.channel=%s AND m.chat_id=%s
                      AND m.direction='system' AND m.message_type='goal_update'
                      AND m.content_json->>'schema_version'='conversation-goal.v1'
                      AND (reset.created_at IS NULL OR m.created_at>reset.created_at)
                    ORDER BY m.created_at DESC,m.message_id DESC LIMIT 1
                    """,
                    (project_id, channel, chat_id, project_id, channel, chat_id),
                )
            ).fetchone()
        if row is None:
            return None
        content = dict(row["content_json"] or {})
        if content.get("status") != "active" or not str(content.get("goal") or "").strip():
            return None
        return {
            "message_id": str(row["message_id"]),
            "goal": str(content["goal"]),
            "source_message_id": content.get("source_message_id"),
            "created_at": row["created_at"],
        }

    async def record_conversation_goal(
        self, *, project_id: str, channel: str, chat_id: str,
        user_id: str | None, source_message_id: str, goal: str | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        cleaned = str(goal or "").strip()
        content = {
            "schema_version": "conversation-goal.v1",
            "status": "active" if cleaned else "cleared",
            "goal": cleaned,
            "source_message_id": source_message_id,
            "updated_by_user_id": user_id,
        }
        async with self.database.connection() as connection:
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (f"conversation-command:{project_id}:{channel}:{chat_id}",),
            )
            row = await (
                await connection.execute(
                    """
                    INSERT INTO assistant.messages(
                        message_id,project_id,channel,chat_id,user_id,direction,
                        message_type,content_json,delivery_status,idempotency_key
                    ) VALUES (%s,%s,%s,%s,NULL,'system','goal_update',%s,'received',%s)
                    ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL
                    DO UPDATE SET idempotency_key=EXCLUDED.idempotency_key RETURNING *
                    """,
                    (str(uuid.uuid4()), project_id, channel, chat_id, Jsonb(content), idempotency_key),
                )
            ).fetchone()
            if dict(row["content_json"] or {}) != content:
                raise ValueError("goal idempotency key was reused with different content")
            await connection.commit()
            return row

    async def reset_conversation(
        self, *, project_id: str, channel: str, chat_id: str,
        user_id: str | None, source_message_id: str, idempotency_key: str,
    ) -> dict[str, Any]:
        """Create an auditable chat reset without cancelling background project work."""
        async with self.database.connection() as connection:
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (f"conversation-command:{project_id}:{channel}:{chat_id}",),
            )
            existing = await (
                await connection.execute(
                    "SELECT * FROM assistant.messages WHERE idempotency_key=%s",
                    (idempotency_key,),
                )
            ).fetchone()
            if existing is not None:
                await connection.commit()
                return {
                    "reset": existing,
                    "cancelled_run_ids": list(
                        (existing["content_json"] or {}).get("cancelled_run_ids") or []
                    ),
                    "cancel_requested_run_ids": list(
                        (existing["content_json"] or {}).get("cancel_requested_run_ids") or []
                    ),
                    "sealed_session_ids": list(
                        (existing["content_json"] or {}).get("sealed_session_ids") or []
                    ),
                    "duplicate": True,
                }

            runs = list(await (
                await connection.execute(
                    """
                    SELECT r.run_id,r.project_id,r.status,r.request_json
                    FROM assistant.runs r
                    JOIN assistant.messages trigger ON trigger.message_id=r.trigger_message_id
                    WHERE r.project_id=%s AND trigger.channel=%s AND trigger.chat_id=%s
                      AND r.status IN ('queued','running')
                      AND (r.run_type='chat' OR (r.run_type='stage' AND r.stage='requirements'))
                    FOR UPDATE OF r
                    """,
                    (project_id, channel, chat_id),
                )
            ).fetchall())
            cancelled = [str(item["run_id"]) for item in runs if item["status"] == "queued"]
            cancel_requested = [
                str(item["run_id"]) for item in runs if item["status"] == "running"
            ]
            if cancelled:
                await connection.execute(
                    """
                    UPDATE assistant.runs
                    SET status='cancelled',cancel_requested_at=COALESCE(cancel_requested_at,now()),
                        ended_at=now(),updated_at=now()
                    WHERE run_id=ANY(%s::uuid[]) AND status='queued'
                    """,
                    (cancelled,),
                )
                for item in runs:
                    if (
                        str(item["run_id"]) in cancelled
                        and (item.get("request_json") or {}).get("task_protocol")
                        == "single-agent.v2"
                    ):
                        await self._advance_supervisor_context_epoch_locked(
                            connection, task=item, reason="task_reset",
                            source_id=source_message_id,
                        )
            if cancel_requested:
                await connection.execute(
                    """
                    UPDATE assistant.runs
                    SET cancel_requested_at=COALESCE(cancel_requested_at,now()),updated_at=now()
                    WHERE run_id=ANY(%s::uuid[]) AND status='running'
                    """,
                    (cancel_requested,),
                )
            affected_runs = [*cancelled, *cancel_requested]
            if affected_runs:
                await connection.execute(
                    """
                    UPDATE assistant.messages
                    SET delivery_status='cancelled',updated_at=now(),
                        error_text='suppressed by conversation reset'
                    WHERE run_id=ANY(%s::uuid[]) AND direction='outbound'
                      AND delivery_status='pending'
                    """,
                    (affected_runs,),
                )

            sealed_rows = list(await (
                await connection.execute(
                    """
                    UPDATE assistant.sessions
                    SET status='sealed',ended_at=COALESCE(ended_at,now()),updated_at=now()
                    WHERE project_id=%s AND channel=%s AND chat_id=%s
                      AND status IN ('active','rotation_required')
                      AND (
                        agent_key='system-butler' OR agent_key LIKE '%%:manager'
                        OR agent_key LIKE '%%:requirements'
                      )
                    RETURNING session_id
                    """,
                    (project_id, channel, chat_id),
                )
            ).fetchall())
            sealed = [str(item["session_id"]) for item in sealed_rows]
            reset_id = str(uuid.uuid4())
            content = {
                "schema_version": "conversation-reset.v1",
                "reset_id": reset_id,
                "scope": "chat",
                "reason": "user_new_command",
                "source_message_id": source_message_id,
                "initiated_by_user_id": user_id,
                "cancelled_run_ids": cancelled,
                "cancel_requested_run_ids": cancel_requested,
                "sealed_session_ids": sealed,
            }
            reset = await (
                await connection.execute(
                    """
                    INSERT INTO assistant.messages(
                        message_id,project_id,channel,chat_id,user_id,direction,
                        message_type,content_json,delivery_status,idempotency_key
                    ) VALUES (%s,%s,%s,%s,NULL,'system','conversation_reset',%s,'received',%s)
                    RETURNING *
                    """,
                    (str(uuid.uuid4()), project_id, channel, chat_id, Jsonb(content), idempotency_key),
                )
            ).fetchone()
            await connection.commit()
            return {
                "reset": reset,
                "cancelled_run_ids": cancelled,
                "cancel_requested_run_ids": cancel_requested,
                "sealed_session_ids": sealed,
                "duplicate": False,
            }

    async def list_pending_channel_runs(
        self, *, project_id: str, channel: str, chat_id: str,
        user_id: str | None, limit: int = 20,
    ) -> list[dict[str, Any]]:
        """List user-actionable Runs in one exact platform conversation."""
        async with self.database.connection() as connection:
            rows = list(await (
                await connection.execute(
                    """
                    SELECT r.run_id,r.project_id,r.agent_key,r.run_type,r.stage,r.status,
                           r.created_at,r.updated_at,r.waiting_question_message_id,
                           question.message_type AS question_message_type,
                           question.content_json AS question_content
                    FROM assistant.runs r
                    JOIN assistant.messages trigger ON trigger.message_id=r.trigger_message_id
                    LEFT JOIN assistant.messages question
                      ON question.message_id=r.waiting_question_message_id
                    WHERE r.project_id=%s AND trigger.channel=%s AND trigger.chat_id=%s
                      AND r.status IN ('waiting_user','waiting_supervisor','waiting_children','blocked')
                      AND (trigger.user_id IS NULL OR trigger.user_id IS NOT DISTINCT FROM %s)
                    ORDER BY r.updated_at DESC,r.run_id DESC LIMIT %s
                    """,
                    (project_id, channel, chat_id, user_id, min(max(limit, 1), 25)),
                )
            ).fetchall())
        return [
            {
                **dict(row),
                "run_id": str(row["run_id"]),
                "waiting_question_message_id": (
                    str(row["waiting_question_message_id"])
                    if row.get("waiting_question_message_id") else None
                ),
                "question_content": dict(row.get("question_content") or {}),
            }
            for row in rows
        ]

    async def list_channel_run_status(
        self, *, project_id: str, channel: str, chat_id: str,
        user_id: str | None, limit: int = 10,
    ) -> list[dict[str, Any]]:
        """List recent Run state for one exact platform conversation and user."""
        async with self.database.connection() as connection:
            rows = list(await (
                await connection.execute(
                    """
                    SELECT r.run_id,r.project_id,r.agent_key,r.run_type,r.stage,r.status,
                           r.attempt,r.max_attempts,r.created_at,r.updated_at,r.ended_at
                    FROM assistant.runs r
                    JOIN assistant.messages trigger ON trigger.message_id=r.trigger_message_id
                    WHERE r.project_id=%s AND trigger.channel=%s AND trigger.chat_id=%s
                      AND (trigger.user_id IS NULL OR trigger.user_id IS NOT DISTINCT FROM %s)
                    ORDER BY
                      CASE WHEN r.status IN (
                        'queued','running','waiting_user','waiting_supervisor','waiting_children','blocked','reconciling'
                      ) THEN 0 ELSE 1 END,
                      r.updated_at DESC,r.run_id DESC
                    LIMIT %s
                    """,
                    (project_id, channel, chat_id, user_id, min(max(limit, 1), 25)),
                )
            ).fetchall())
        return [
            {**dict(row), "run_id": str(row["run_id"])}
            for row in rows
        ]

    async def resume_waiting_run(
        self, *, run_id: str, project_id: str, channel: str, chat_id: str,
        user_id: str | None, source_message_id: str, idempotency_key: str,
    ) -> dict[str, Any]:
        """Re-surface a detached waiting interaction in the current chat generation."""
        async with self.database.connection() as connection:
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (f"resume-run:{run_id}",),
            )
            run = await (
                await connection.execute(
                    """
                    SELECT r.*,trigger.channel,trigger.chat_id,
                           trigger.user_id AS trigger_user_id
                    FROM assistant.runs r
                    JOIN assistant.messages trigger ON trigger.message_id=r.trigger_message_id
                    WHERE r.run_id=%s FOR UPDATE OF r
                    """,
                    (run_id,),
                )
            ).fetchone()
            if run is None:
                raise ValueError("Run不存在")
            if self._retired_stage(project_id, run):
                raise PermissionError("旧阶段任务已退役；请提交新的Task请求")
            if (
                str(run["project_id"]) != project_id
                or str(run["channel"]) != channel
                or str(run["chat_id"]) != chat_id
                or run.get("trigger_user_id") is not None
                   and str(run.get("trigger_user_id")) != str(user_id)
            ):
                raise PermissionError("Run不属于当前Project、会话或用户")
            if run["status"] == "blocked":
                await connection.commit()
                return {"run": run, "message": None, "blocked": True, "duplicate": False}
            if run["status"] != "waiting_user" or not run.get("waiting_question_message_id"):
                raise ValueError("Run当前不是可恢复的待答状态")

            existing = await (
                await connection.execute(
                    "SELECT * FROM assistant.messages WHERE idempotency_key=%s",
                    (idempotency_key,),
                )
            ).fetchone()
            if existing is not None:
                await connection.commit()
                return {"run": run, "message": existing, "blocked": False, "duplicate": True}

            question = await (
                await connection.execute(
                    "SELECT * FROM assistant.messages WHERE message_id=%s",
                    (run["waiting_question_message_id"],),
                )
            ).fetchone()
            if question is None or question["message_type"] not in {"question", "approval"}:
                raise ValueError("Run的待答消息无效")
            if question.get("user_id") is not None and str(question.get("user_id")) != str(user_id):
                raise PermissionError("Run的待答消息属于其他用户")
            session_id = str(run["session_id"]) if run.get("session_id") else None
            if session_id:
                active = await (
                    await connection.execute(
                        """
                        SELECT session_id FROM assistant.sessions
                        WHERE project_id=%s AND agent_key=%s AND channel=%s AND chat_id=%s
                          AND status IN ('active','rotation_required')
                        LIMIT 1 FOR UPDATE
                        """,
                        (project_id, run["agent_key"], channel, chat_id),
                    )
                ).fetchone()
                if active is not None and str(active["session_id"]) != session_id:
                    raise ValueError("同一Agent已有新的活动会话，不能同时恢复旧任务")
                await connection.execute(
                    """
                    UPDATE assistant.sessions SET status='active',ended_at=NULL,updated_at=now()
                    WHERE session_id=%s AND status='sealed'
                    """,
                    (session_id,),
                )
            prior_messages = [question]
            if question["message_type"] == "question":
                result = dict(run.get("result_json") or {})
                prior_ids = [str(item) for item in result.get("question_message_ids") or []]
                if prior_ids:
                    prior_messages = []
                    for prior_id in prior_ids:
                        prior = await (await connection.execute(
                            """
                            SELECT * FROM assistant.messages
                            WHERE message_id=%s AND run_id=%s AND message_type='question'
                            """,
                            (prior_id, run_id),
                        )).fetchone()
                        if prior is None:
                            raise ValueError("Run的待答问题列表无效")
                        prior_messages.append(prior)
                answered = dict((run.get("request_json") or {}).get("answer_state") or {})
                prior_messages = [
                    item for item in prior_messages
                    if str((item["content_json"] or {}).get("question_id") or "") not in answered
                    or answered[str((item["content_json"] or {}).get("question_id") or "")] in (None, "")
                ]
                if not prior_messages:
                    raise ValueError("Run没有尚待回答的问题")
            messages = []
            for index, prior in enumerate(prior_messages):
                payload = dict(prior["content_json"] or {})
                payload["resumed_from_message_id"] = str(prior["message_id"])
                payload["resume_source_message_id"] = source_message_id
                message = await (await connection.execute(
                    """
                    INSERT INTO assistant.messages(
                        message_id,project_id,session_id,run_id,channel,chat_id,user_id,
                        direction,message_type,reply_to_message_id,content_json,
                        delivery_status,idempotency_key
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,'outbound',%s,%s,%s,'pending',%s)
                    RETURNING *
                    """,
                    (
                        str(uuid.uuid4()), project_id, session_id, run_id, channel, chat_id,
                        user_id, prior["message_type"], source_message_id,
                        Jsonb(json_compatible(payload)),
                        idempotency_key if index == 0 else f"{idempotency_key}:{index}",
                    ),
                )).fetchone()
                messages.append(message)
            if question["message_type"] == "question":
                await connection.execute(
                    """
                    UPDATE assistant.runs
                    SET waiting_question_message_id=%s,
                        result_json=COALESCE(result_json,'{}'::jsonb) || %s,
                        updated_at=now()
                    WHERE run_id=%s AND status='waiting_user'
                    """,
                    (
                        messages[0]["message_id"],
                        Jsonb({"question_message_ids": [
                            str(item["message_id"]) for item in messages
                        ]}), run_id,
                    ),
                )
            else:
                await connection.execute(
                    """
                    UPDATE assistant.runs
                    SET waiting_question_message_id=%s,updated_at=now()
                    WHERE run_id=%s AND status='waiting_user'
                    """,
                    (messages[0]["message_id"], run_id),
                )
            await connection.commit()
            return {
                "run": run, "message": messages[0], "messages": messages,
                "blocked": False, "duplicate": False,
            }

    async def list_session_messages(
        self, session_id: str, limit: int = 2000,
    ) -> list[dict[str, Any]]:
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                """SELECT * FROM assistant.messages WHERE session_id=%s
                ORDER BY created_at,message_id LIMIT %s""",
                (session_id, min(max(limit, 1), 5000)),
            )
            return list(await cursor.fetchall())

    async def list_session_runs(
        self, session_id: str, limit: int = 1000,
    ) -> list[dict[str, Any]]:
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                """SELECT * FROM assistant.runs WHERE session_id=%s
                ORDER BY created_at,run_id LIMIT %s""",
                (session_id, min(max(limit, 1), 5000)),
            )
            return list(await cursor.fetchall())

    async def latest_session_run(self, session_id: str) -> dict[str, Any] | None:
        """Return the latest durable execution record attached to one Session."""
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                """SELECT * FROM assistant.runs WHERE session_id=%s
                ORDER BY updated_at DESC,run_id DESC LIMIT 1""",
                (session_id,),
            )
            return await cursor.fetchone()

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        async with self.database.connection() as connection:
            cursor = await connection.execute("SELECT * FROM assistant.runs WHERE run_id=%s", (run_id,))
            return await cursor.fetchone()

    async def latest_session_compatibility_hash(self, session_id: str) -> str | None:
        """Return the last execution contract that actually used this Session."""
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT executor_json->>'session_compatibility_hash' AS compatibility_hash
                FROM assistant.runs
                WHERE session_id=%s
                  AND executor_json ? 'session_compatibility_hash'
                ORDER BY created_at DESC,run_id DESC LIMIT 1
                """,
                (session_id,),
            )
            row = await cursor.fetchone()
            return str(row["compatibility_hash"]) if row and row["compatibility_hash"] else None

    async def resolve_route_binding(
        self, channel: str, chat_id: str, user_id: str | None,
    ) -> tuple[str, str] | None:
        """Return the latest durable PM binding for one channel participant."""
        async with self.database.connection() as connection:
            row = await (
                await connection.execute(
                    """
                    SELECT project_id,content_json
                    FROM assistant.messages
                    WHERE direction='system' AND message_type='route_binding'
                      AND channel=%s AND chat_id=%s
                      AND user_id IS NOT DISTINCT FROM %s
                    ORDER BY created_at DESC,message_id DESC LIMIT 1
                    """,
                    (channel, chat_id, user_id),
                )
            ).fetchone()
        if row is None:
            return None
        content = dict(row["content_json"] or {})
        if content.get("status") != "active":
            return None
        project_id = str(content.get("project_id") or row["project_id"])
        agent_key = str(content.get("agent_key") or (
            "system-butler" if project_id == "system"
            else f"project:{project_id}:manager"
        ))
        return project_id, agent_key

    async def resolve_chat_project_binding(
        self, channel: str, chat_id: str,
    ) -> dict[str, Any] | None:
        """Return the latest group-wide Project binding for one external chat."""
        async with self.database.connection() as connection:
            row = await (
                await connection.execute(
                    """
                    SELECT message_id,project_id,content_json,created_at
                    FROM assistant.messages
                    WHERE direction='system' AND message_type='route_binding'
                      AND channel=%s AND chat_id=%s AND user_id IS NULL
                    ORDER BY created_at DESC,message_id DESC LIMIT 1
                    """,
                    (channel, chat_id),
                )
            ).fetchone()
        if row is None:
            return None
        content = dict(row["content_json"] or {})
        if content.get("status") != "active" or content.get("binding_scope") != "chat":
            return None
        project_id = str(content.get("project_id") or row["project_id"])
        return {
            "message_id": str(row["message_id"]),
            "project_id": project_id,
            "agent_key": str(content.get("agent_key") or f"project:{project_id}:manager"),
            "routing_hash": content.get("project_routing_hash"),
            "bound_by_user_id": content.get("bound_by_user_id"),
            "created_at": row["created_at"],
        }

    async def resolve_project_chat_binding(
        self, channel: str, project_id: str,
    ) -> dict[str, Any] | None:
        """Return the one active top-level chat binding for a Project on a platform."""
        async with self.database.connection() as connection:
            row = await (
                await connection.execute(
                    """
                    WITH latest AS (
                        SELECT DISTINCT ON (chat_id)
                               message_id,project_id,chat_id,content_json,created_at
                        FROM assistant.messages
                        WHERE direction='system' AND message_type='route_binding'
                          AND channel=%s AND user_id IS NULL
                        ORDER BY chat_id,created_at DESC,message_id DESC
                    )
                    SELECT * FROM latest
                    WHERE content_json->>'status'='active'
                      AND content_json->>'binding_scope'='chat'
                      AND COALESCE(content_json->>'project_id',project_id)=%s
                    ORDER BY created_at DESC,message_id DESC LIMIT 1
                    """,
                    (channel, project_id),
                )
            ).fetchone()
        if row is None:
            return None
        content = dict(row["content_json"] or {})
        return {
            "message_id": str(row["message_id"]),
            "project_id": str(content.get("project_id") or row["project_id"]),
            "chat_id": str(row["chat_id"]),
            "agent_key": str(content.get("agent_key") or f"project:{project_id}:manager"),
            "routing_hash": content.get("project_routing_hash"),
            "bound_by_user_id": content.get("bound_by_user_id"),
            "created_at": row["created_at"],
        }

    async def bind_chat_project(
        self, *, channel: str, chat_id: str, project_id: str, agent_key: str,
        bound_by_user_id: str, source_message_id: str, project_routing_hash: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Bind one chat to one Project; many chats may bind the same Project."""
        content = {
            "schema_version": "route-binding.v4",
            "binding_scope": "chat",
            "status": "active",
            "project_id": project_id,
            "agent_key": agent_key,
            "bound_by_user_id": bound_by_user_id,
            "source_message_id": source_message_id,
            "project_routing_hash": project_routing_hash,
            "reason": f"{channel}_bind_project",
        }
        async with self.database.connection() as connection:
            for lock_key in sorted((
                f"route-chat:{channel}:{chat_id}",
                f"route-project:{channel}:{project_id}",
            )):
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                    (lock_key,),
                )
            current = await (
                await connection.execute(
                    """
                    SELECT message_id,project_id,content_json,created_at
                    FROM assistant.messages
                    WHERE direction='system' AND message_type='route_binding'
                      AND channel=%s AND chat_id=%s AND user_id IS NULL
                    ORDER BY created_at DESC,message_id DESC LIMIT 1
                    """,
                    (channel, chat_id),
                )
            ).fetchone()
            if current is not None:
                current_content = dict(current["content_json"] or {})
                if (
                    current_content.get("status") == "active"
                    and current_content.get("binding_scope") == "chat"
                ):
                    current_project = str(
                        current_content.get("project_id") or current["project_id"]
                    )
                    if current_project != project_id:
                        raise ValueError(
                            f"chat is already bound to Project {current_project}; unbind it first"
                        )
                    await connection.commit()
                    return {"binding": current, "changed": False}
            project_binding = await (
                await connection.execute(
                    """
                    WITH latest AS (
                        SELECT DISTINCT ON (chat_id)
                               message_id,project_id,chat_id,content_json,created_at
                        FROM assistant.messages
                        WHERE direction='system' AND message_type='route_binding'
                          AND channel=%s AND user_id IS NULL
                        ORDER BY chat_id,created_at DESC,message_id DESC
                    )
                    SELECT * FROM latest
                    WHERE content_json->>'status'='active'
                      AND content_json->>'binding_scope'='chat'
                      AND COALESCE(content_json->>'project_id',project_id)=%s
                    ORDER BY created_at DESC,message_id DESC LIMIT 1
                    """,
                    (channel, project_id),
                )
            ).fetchone()
            if project_binding is not None and str(project_binding["chat_id"]) != chat_id:
                raise ValueError(
                    f"Project {project_id} is already bound to chat "
                    f"{project_binding['chat_id']} on {channel}; unbind it first"
                )
            row = await (
                await connection.execute(
                    """
                    INSERT INTO assistant.messages(
                        message_id,project_id,channel,chat_id,user_id,direction,message_type,
                        content_json,delivery_status,idempotency_key
                    ) VALUES (%s,%s,%s,%s,NULL,'system','route_binding',%s,'received',%s)
                    ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL
                    DO UPDATE SET idempotency_key=EXCLUDED.idempotency_key RETURNING *
                    """,
                    (
                        str(uuid.uuid4()), project_id, channel, chat_id,
                        Jsonb(content), idempotency_key,
                    ),
                )
            ).fetchone()
            if (row["content_json"] or {}) != content:
                raise ValueError("chat binding idempotency key was reused with different content")
            await connection.commit()
            return {"binding": row, "changed": True}

    async def unbind_chat_project(
        self, *, channel: str, chat_id: str, expected_project_id: str,
        unbound_by_user_id: str, source_message_id: str, idempotency_key: str,
    ) -> dict[str, Any]:
        """Append an auditable tombstone for the current group-wide binding."""
        async with self.database.connection() as connection:
            for lock_key in sorted((
                f"route-chat:{channel}:{chat_id}",
                f"route-project:{channel}:{expected_project_id}",
            )):
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                    (lock_key,),
                )
            current = await (
                await connection.execute(
                    """
                    SELECT message_id,project_id,content_json
                    FROM assistant.messages
                    WHERE direction='system' AND message_type='route_binding'
                      AND channel=%s AND chat_id=%s AND user_id IS NULL
                    ORDER BY created_at DESC,message_id DESC LIMIT 1
                    """,
                    (channel, chat_id),
                )
            ).fetchone()
            if current is None:
                await connection.commit()
                return {"binding": None, "changed": False}
            current_content = dict(current["content_json"] or {})
            if (
                current_content.get("status") != "active"
                or current_content.get("binding_scope") != "chat"
            ):
                await connection.commit()
                return {"binding": current, "changed": False}
            current_project = str(
                current_content.get("project_id") or current["project_id"]
            )
            if current_project != expected_project_id:
                raise ValueError(
                    f"chat is bound to Project {current_project}, not {expected_project_id}"
                )
            content = {
                "schema_version": "route-binding.v4",
                "binding_scope": "chat",
                "status": "inactive",
                "project_id": current_project,
                "agent_key": current_content.get("agent_key"),
                "unbound_by_user_id": unbound_by_user_id,
                "source_message_id": source_message_id,
                "previous_binding_message_id": str(current["message_id"]),
                "reason": f"{channel}_unbind_project",
            }
            row = await (
                await connection.execute(
                    """
                    INSERT INTO assistant.messages(
                        message_id,project_id,channel,chat_id,user_id,direction,message_type,
                        content_json,delivery_status,idempotency_key
                    ) VALUES (%s,%s,%s,%s,NULL,'system','route_binding',%s,'received',%s)
                    ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL
                    DO UPDATE SET idempotency_key=EXCLUDED.idempotency_key RETURNING *
                    """,
                    (
                        str(uuid.uuid4()), current_project, channel, chat_id,
                        Jsonb(content), idempotency_key,
                    ),
                )
            ).fetchone()
            if (row["content_json"] or {}) != content:
                raise ValueError("chat unbinding idempotency key was reused with different content")
            await connection.commit()
            return {"binding": row, "changed": True}

    async def record_channel_command(
        self, *, project_id: str, agent_key: str, channel: str, chat_id: str,
        user_id: str | None, external_event_id: str, external_message_id: str | None,
        content: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist a deterministic channel command without creating an LLM Run."""
        message_id = str(uuid.uuid4())
        async with self.database.connection() as connection:
            row = await (
                await connection.execute(
                    """
                    INSERT INTO assistant.messages(
                        message_id,project_id,channel,chat_id,user_id,direction,message_type,
                        external_event_id,external_message_id,content_json,delivery_status,
                        idempotency_key
                    ) VALUES (%s,%s,%s,%s,%s,'inbound','command',%s,%s,%s,'received',%s)
                    ON CONFLICT (channel,external_event_id)
                        WHERE external_event_id IS NOT NULL DO NOTHING
                    RETURNING *
                    """,
                    (
                        message_id, project_id, channel, chat_id, user_id,
                        external_event_id, external_message_id,
                        Jsonb(json_compatible(content)), f"inbound:{channel}:{external_event_id}",
                    ),
                )
            ).fetchone()
            duplicate = row is None
            if row is None:
                row = await (
                    await connection.execute(
                        "SELECT * FROM assistant.messages WHERE channel=%s AND external_event_id=%s",
                        (channel, external_event_id),
                    )
                ).fetchone()
                expected = {
                    "project_id": project_id,
                    "chat_id": chat_id,
                    "user_id": user_id,
                    "message_type": "command",
                    "content_json": json_compatible(content),
                }
                if row is None or any(row[key] != value for key, value in expected.items()):
                    raise ValueError("external_event_id was reused for a different command")
            await connection.commit()
            return {"message": row, "duplicate": duplicate}

    async def set_route_binding(
        self, *, channel: str, chat_id: str, user_id: str | None,
        project_id: str, agent_key: str, reason: str,
        idempotency_key: str, source_message_id: str | None = None,
    ) -> dict[str, Any]:
        """Atomically append one durable channel-to-Agent route binding."""
        content = {
            "schema_version": "route-binding.v2",
            "status": "active",
            "project_id": project_id,
            "agent_key": agent_key,
            "source_message_id": source_message_id,
            "reason": reason,
        }
        async with self.database.connection() as connection:
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (f"route:{channel}:{chat_id}:{user_id or ''}",),
            )
            row = await (
                await connection.execute(
                    """
                    INSERT INTO assistant.messages(
                        message_id,project_id,channel,chat_id,user_id,direction,message_type,
                        content_json,delivery_status,idempotency_key
                    ) VALUES (%s,%s,%s,%s,%s,'system','route_binding',%s,'received',%s)
                    ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL
                    DO UPDATE SET idempotency_key=EXCLUDED.idempotency_key RETURNING *
                    """,
                    (
                        str(uuid.uuid4()), project_id, channel, chat_id, user_id,
                        Jsonb(content), idempotency_key,
                    ),
                )
            ).fetchone()
            if (row["content_json"] or {}) != content:
                raise ValueError("route idempotency key was reused with different content")
            await connection.commit()
            return row

    async def create_run(
        self, *, project_id: str, agent_key: str, run_type: str,
        request: dict[str, Any], stage: str | None = None,
        parent_run_id: str | None = None, trigger_message_id: str | None = None,
        executor: dict[str, Any] | None = None, idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if run_type not in {"chat", "stage", "python"}:
            raise ValueError("run_type must be chat, stage, or python")
        if run_type != "python" and not trigger_message_id:
            raise ValueError("chat and stage runs require trigger_message_id")
        run_id = str(uuid.uuid4())
        key = idempotency_key or f"run:{run_id}"
        resolved_executor = {
            **dict(executor or {}),
            "scheduling_lane": scheduling_lane(run_type, agent_key),
        }
        async with self.database.connection() as connection:
            if trigger_message_id:
                trigger = await (
                    await connection.execute(
                        "SELECT project_id,channel,chat_id FROM assistant.messages WHERE message_id=%s",
                        (trigger_message_id,),
                    )
                ).fetchone()
                if trigger is None or str(trigger["project_id"]) != project_id:
                    raise ValueError("trigger_message_id must belong to the same project")
            if parent_run_id:
                parent = await (
                    await connection.execute(
                        "SELECT project_id FROM assistant.runs WHERE run_id=%s",
                        (parent_run_id,),
                    )
                ).fetchone()
                if parent is None or str(parent["project_id"]) != project_id:
                    raise ValueError("parent_run_id must belong to the same project")
            run_conversation_key = (
                "supervisor-task:" + canonical_sha256([
                    project_id, agent_key, str(request.get("task_id") or ""),
                ])
                if (
                    agent_key.endswith(":supervisor")
                    and request.get("interaction_kind") == "supervisor_consultation"
                    and request.get("task_id")
                )
                else conversation_key(
                    project_id, agent_key,
                    str(trigger["channel"]), str(trigger["chat_id"]),
                )
                if trigger_message_id else f"python:{project_id}:{run_id}"
            )
            cursor = await connection.execute(
                """
                INSERT INTO assistant.runs(
                    run_id,project_id,agent_key,trigger_message_id,parent_run_id,run_type,
                    stage,status,conversation_key,request_json,executor_json,idempotency_key
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,'queued',%s,%s,%s,%s)
                ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL
                DO UPDATE SET idempotency_key=EXCLUDED.idempotency_key RETURNING *
                """,
                (
                    run_id, project_id, agent_key, trigger_message_id, parent_run_id,
                    run_type, stage,
                    run_conversation_key,
                    Jsonb(json_compatible(request)),
                    Jsonb(json_compatible(resolved_executor)), key,
                ),
            )
            row = await cursor.fetchone()
            if (
                str(row["project_id"]) != project_id
                or str(row["agent_key"]) != agent_key
                or str(row["run_type"]) != run_type
                or (row["request_json"] or {}) != json_compatible(request)
            ):
                raise ValueError("idempotency_key was reused for a different run")
            await connection.commit()
            return row

    async def start_task_from_claim(
        self, *, source_run_id: str, worker_instance_id: str, claim_token: str,
        goal: str, stages: list[str], change_class: str = "major",
        executor: dict[str, Any] | None = None,
        task_metadata: dict[str, Any] | None = None,
        supersede_run_id: str | None = None,
        supersede_reason: str | None = None,
    ) -> dict[str, Any]:
        if not goal.strip():
            raise ValueError("task goal is required")
        if not stages or len(stages) > 8:
            raise ValueError("a task requires between one and eight stages")
        allowed = {"requirements", "implementation", "review"}
        if any(stage not in allowed for stage in stages) or len(set(stages)) != len(stages):
            raise ValueError("task stages must be unique supported stage names")
        if change_class not in {"minor", "major"}:
            raise ValueError("change_class must be minor or major")
        new_run_id = str(uuid.uuid4())
        async with self.database.connection() as connection:
            source = await (
                await connection.execute(
                    """
                    SELECT r.*,m.channel,m.chat_id,m.user_id
                    FROM assistant.runs r
                    JOIN assistant.messages m ON m.message_id=r.trigger_message_id
                    WHERE r.run_id=%s AND r.worker_instance_id=%s AND r.claim_token=%s
                      AND r.status='running' AND r.cancel_requested_at IS NULL
                    FOR UPDATE OF r
                    """,
                    (source_run_id, worker_instance_id, claim_token),
                )
            ).fetchone()
            if source is None:
                raise PermissionError("the scoped source run is no longer leased")
            if self._single_agent_project(str(source["project_id"])):
                raise PermissionError(
                    "Stage Task creation is retired for single-agent.v2 Projects"
                )
            existing_open = await (
                await connection.execute(
                    """
                    SELECT run_id,status,stage,run_type FROM assistant.runs r
                    WHERE r.project_id=%s
                      AND (
                        (
                          COALESCE(r.request_json->>'task_protocol','') IN ('task.v1','single-agent.v2')
                          AND r.status IN ('queued','running','waiting_user','waiting_supervisor','waiting_children','blocked','reconciling')
                        ) OR (
                          r.status IN ('queued','running','reconciling')
                          AND assistant.run_lane(
                                r.run_type, r.agent_key,
                                r.executor_json->>'scheduling_lane'
                              )='execution'
                        )
                      )
                    ORDER BY r.created_at,r.run_id LIMIT 1
                    FOR UPDATE OF r
                    """,
                    (source["project_id"],),
                )
            ).fetchone()
            if (
                existing_open is not None
                and str(existing_open["run_id"]) != str(supersede_run_id or "")
            ):
                raise RuntimeError(
                    "Project already has an open execution task: "
                    f"{existing_open['run_id']} ({existing_open['status']}); "
                    "query its status or explicitly supersede it"
                )
            supersession: dict[str, Any] | None = None
            if supersede_run_id:
                if not str(supersede_reason or "").strip():
                    raise ValueError("supersede_reason is required when supersede_run_id is set")
                target = await (
                    await connection.execute(
                        "SELECT * FROM assistant.runs WHERE run_id=%s FOR UPDATE",
                        (supersede_run_id,),
                    )
                ).fetchone()
                if target is None or str(target["project_id"]) != str(source["project_id"]):
                    raise FileNotFoundError("superseded Run was not found in the current Project")
                if target["run_type"] != "stage":
                    raise ValueError("only a Stage Run can be superseded by a Project task")
                target_request = dict(target.get("request_json") or {})
                target_root = str(
                    target_request.get("task_root_run_id") or target["run_id"]
                )
                supersession = {
                    "schema_version": "task-supersession.v1",
                    "replacement_run_id": new_run_id,
                    "requested_by_run_id": source_run_id,
                    "reason": str(supersede_reason).strip(),
                    "requested_at": datetime.utcnow().isoformat() + "Z",
                }
                target_executor = dict(target.get("executor_json") or {})
                target_executor["supersession"] = supersession
                if target["status"] in {
                    "queued", "waiting_user", "waiting_supervisor",
                    "waiting_children", "blocked",
                }:
                    await connection.execute(
                        """
                        UPDATE assistant.runs
                        SET status='cancelled',executor_json=%s,cancel_requested_at=now(),
                            ended_at=now(),updated_at=now(),waiting_question_message_id=NULL
                        WHERE run_id=%s
                        """,
                        (Jsonb(json_compatible(target_executor)), target["run_id"]),
                    )
                elif target["status"] == "running":
                    await connection.execute(
                        """
                        UPDATE assistant.runs
                        SET executor_json=%s,cancel_requested_at=now(),updated_at=now()
                        WHERE run_id=%s
                        """,
                        (Jsonb(json_compatible(target_executor)), target["run_id"]),
                    )
                elif target["status"] == "reconciling":
                    await connection.execute(
                        "UPDATE assistant.runs SET executor_json=%s,updated_at=now() WHERE run_id=%s",
                        (Jsonb(json_compatible(target_executor)), target["run_id"]),
                    )
                await connection.execute(
                    """
                    UPDATE assistant.runs
                    SET status='cancelled',cancel_requested_at=now(),ended_at=now(),updated_at=now(),
                        result_json=COALESCE(result_json,'{}'::jsonb) || %s
                    WHERE project_id=%s AND run_id<>%s
                      AND request_json->>'task_root_run_id'=%s
                      AND status IN ('queued','waiting_user','waiting_supervisor','waiting_children','blocked')
                    """,
                    (
                        Jsonb({"superseded_by_run_id": new_run_id}),
                        source["project_id"], target["run_id"], target_root,
                    ),
                )
            first_stage = stages[0]
            identity = {
                "goal": goal.strip(), "stage_plan": stages,
                "change_class": change_class, "source_run_id": source_run_id,
                "task_executor": json_compatible(executor or {}),
                **json_compatible(task_metadata or {}),
            }
            if supersession is not None:
                identity["supersedes_run_id"] = supersede_run_id
                identity["supersede_reason"] = supersession["reason"]
            key = f"task:{source_run_id}:{canonical_sha256(identity)}"
            task_message_id = str(uuid.uuid4())
            task_message = await (
                await connection.execute(
                    """
                    INSERT INTO assistant.messages(
                        message_id,project_id,channel,chat_id,user_id,direction,message_type,
                        content_json,delivery_status,idempotency_key
                    ) VALUES (%s,%s,%s,%s,%s,'system','task',%s,'received',%s)
                    ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL
                    DO UPDATE SET idempotency_key=EXCLUDED.idempotency_key RETURNING *
                    """,
                    (
                        task_message_id, source["project_id"], source["channel"],
                        source["chat_id"], source["user_id"], Jsonb(identity),
                        f"task-trigger:{key}",
                    ),
                )
            ).fetchone()
            request = {
                "goal": goal.strip(), "stage_plan": stages, "stage_index": 0,
                "stage_round": 1,
                "task_root_run_id": new_run_id, "source_run_id": source_run_id,
                "change_class": change_class,
                "task_executor": identity["task_executor"],
                **json_compatible(task_metadata or {}),
            }
            if supersession is not None:
                request["supersedes_run_id"] = supersede_run_id
                request["supersede_reason"] = supersession["reason"]
            row = await (
                await connection.execute(
                    """
                    INSERT INTO assistant.runs(
                        run_id,project_id,agent_key,trigger_message_id,parent_run_id,
                        run_type,stage,status,conversation_key,request_json,executor_json,idempotency_key
                    ) VALUES (%s,%s,%s,%s,%s,'stage',%s,'queued',%s,%s,%s,%s)
                    ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL
                    DO UPDATE SET idempotency_key=EXCLUDED.idempotency_key RETURNING *
                    """,
                    (
                        new_run_id, source["project_id"],
                        f"project:{source['project_id']}:{first_stage}",
                        task_message["message_id"], source_run_id, first_stage,
                        conversation_key(
                            str(source["project_id"]),
                            f"project:{source['project_id']}:{first_stage}",
                            str(source["channel"]), str(source["chat_id"]),
                        ),
                        Jsonb(request), Jsonb(stage_executor(request, first_stage)), key,
                    ),
                )
            ).fetchone()
            await connection.execute(
                "UPDATE assistant.messages SET run_id=%s WHERE message_id=%s",
                (row["run_id"], task_message["message_id"]),
            )
            await connection.commit()
            return row

    async def pin_plan_work_order(
        self, *, run_id: str, worker_instance_id: str, claim_token: str,
        request_patch: dict[str, Any],
    ) -> dict[str, Any]:
        """Pin a user-facing plan Run to one immutable approval-gated build chain."""
        async with self.database.connection() as connection:
            row = await (
                await connection.execute(
                    """
                    UPDATE assistant.runs
                    SET request_json=COALESCE(request_json,'{}'::jsonb) || %s,
                        updated_at=now()
                    WHERE run_id=%s AND worker_instance_id=%s AND claim_token=%s
                      AND status='running' AND run_type='chat'
                      AND cancel_requested_at IS NULL
                    RETURNING *
                    """,
                    (
                        Jsonb(json_compatible(request_patch)), run_id,
                        worker_instance_id, claim_token,
                    ),
                )
            ).fetchone()
            if row is None:
                raise PermissionError("the plan Run is no longer leased")
            await connection.commit()
            return row

    async def patch_claimed_run_request(
        self, *, run_id: str, worker_instance_id: str, claim_token: str,
        request_patch: dict[str, Any],
    ) -> dict[str, Any]:
        """Merge deterministic Task-controller state into one leased Run."""
        async with self.database.connection() as connection:
            row = await (
                await connection.execute(
                    """
                    UPDATE assistant.runs
                    SET request_json=COALESCE(request_json,'{}'::jsonb) || %s,
                        updated_at=now()
                    WHERE run_id=%s AND worker_instance_id=%s AND claim_token=%s
                      AND status='running' AND cancel_requested_at IS NULL
                    RETURNING *
                    """,
                    (
                        Jsonb(json_compatible(request_patch)), run_id,
                        worker_instance_id, claim_token,
                    ),
                )
            ).fetchone()
            if row is None:
                raise PermissionError("the Task Run is no longer leased")
            await connection.commit()
            return row

    async def finalize_agent_continue(
        self, *, run_id: str, worker_instance_id: str, claim_token: str,
        project_id: str, channel: str, chat_id: str, user_id: str | None,
        source_message_id: str, summary: str, request_patch: dict[str, Any],
        artifact_refs: list[Any], usage_snapshot: dict[str, Any] | None = None,
        reset_execution_plan: bool = False,
        wait_for_supervisor: bool = False,
    ) -> dict[str, Any]:
        """Finish one physical Turn and requeue the same logical Task Run."""
        async with self.database.connection() as connection:
            locked = await (
                await connection.execute(
                    """
                    SELECT run_id FROM assistant.runs
                    WHERE run_id=%s AND worker_instance_id=%s AND claim_token=%s
                      AND status='running' AND cancel_requested_at IS NULL
                    FOR UPDATE
                    """,
                    (run_id, worker_instance_id, claim_token),
                )
            ).fetchone()
            if locked is None:
                raise RuntimeError("Task lease was lost before continuation")
            reply = await (
                await connection.execute(
                    """
                    INSERT INTO assistant.messages(
                        message_id,project_id,run_id,channel,chat_id,user_id,direction,
                        message_type,reply_to_message_id,content_json,delivery_status,
                        idempotency_key
                    ) VALUES (%s,%s,%s,%s,%s,%s,'outbound','reply',%s,%s,'pending',%s)
                    ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL
                    DO UPDATE SET idempotency_key=EXCLUDED.idempotency_key RETURNING *
                    """,
                    (
                        str(uuid.uuid4()), project_id, run_id, channel, chat_id,
                        user_id, source_message_id,
                        Jsonb({
                            "text": summary,
                            "origin": "agent",
                            "task_continues": True,
                            "context_visibility": "semantic",
                        }),
                        f"task-turn-progress:{run_id}:{request_patch.get('task_turn_count')}",
                    ),
                )
            ).fetchone()
            updated = await (
                await connection.execute(
                    """
                    UPDATE assistant.runs
                    SET status=CASE WHEN %s THEN 'waiting_supervisor' ELSE 'queued' END,
                        available_at=now(),
                        request_json=COALESCE(request_json,'{}'::jsonb) || %s,
                        result_json=%s,artifact_refs=%s,error_json=NULL,
                        executor_json=CASE WHEN %s THEN
                          executor_json
                            - 'config_snapshot_uri' - 'config_bundle_hash'
                            - 'agent_config_snapshot_uri' - 'agent_snapshot_sha256'
                            - 'resolved_plan_uri' - 'resolved_plan_sha256'
                            - 'native_permission_profile' - 'native_permission_profile_sha256'
                            - 'prompt_bundle_uri' - 'prompt_bundle_sha256'
                          ELSE executor_json END,
                        worker_id=NULL,worker_instance_id=NULL,claim_token=NULL,
                        lease_until=NULL,process_pid=NULL,process_start_ticks=NULL,
                        updated_at=now(),ended_at=NULL
                    WHERE run_id=%s RETURNING *
                    """,
                    (
                        wait_for_supervisor,
                        Jsonb(json_compatible(request_patch)),
                        Jsonb({
                            "outcome": "continue", "summary": summary,
                            "artifact_refs": json_compatible(artifact_refs),
                            "token_usage": json_compatible(usage_snapshot or {}),
                        }),
                        Jsonb(json_compatible(artifact_refs)), reset_execution_plan,
                        run_id,
                    ),
                )
            ).fetchone()
            await connection.commit()
            return {"run": updated, "reply": reply}

    async def resume_supervision_degraded(
        self, *, task_id: str, turn_number: int, reason: str,
    ) -> dict[str, Any] | None:
        """Fail open for one PM Turn without applying a partial Supervisor result."""
        async with self.database.connection() as connection:
            row = await (await connection.execute(
                """
                UPDATE assistant.runs
                SET status='queued',available_at=now(),
                    request_json=COALESCE(request_json,'{}'::jsonb) || %s ||
                      CASE WHEN COALESCE(NULLIF(
                        request_json->>'pending_validation_supervisor_turn',''
                      ),'0')::integer=%s
                      THEN jsonb_build_object(
                        'validation_supervision_bypassed_turn',%s,
                        'validation_supervisor_approved_turn',%s,
                        'task_phase','working'
                      ) ELSE '{}'::jsonb END,
                    executor_json=executor_json
                      - 'resolved_plan_uri' - 'resolved_plan_sha256'
                      - 'prompt_bundle_uri' - 'prompt_bundle_sha256',
                    updated_at=now(),ended_at=NULL
                WHERE run_id=%s AND status='waiting_supervisor'
                  AND COALESCE((request_json->>'task_turn_count')::integer,0)=%s
                RETURNING *
                """,
                (
                    Jsonb({
                        "degraded_supervision": True,
                        "degraded_supervision_reason": str(reason)[:500],
                        "degraded_supervision_turn": int(turn_number),
                    }),
                    int(turn_number), int(turn_number), int(turn_number),
                    task_id, int(turn_number),
                ),
            )).fetchone()
            await connection.commit()
            return row

    async def commit_supervisor_action(
        self, *, supervisor_run_id: str, worker_instance_id: str,
        claim_token: str, task_id: str, source_context_epoch: int,
        source_turn_number: int, action_ref: dict[str, Any],
        confirmation_class: str, pm_instruction: str,
        target_work_mode: str | None,
        delegations: list[dict[str, Any]],
        result: dict[str, Any], artifact_refs: list[str],
        session_action: str = "keep",
    ) -> dict[str, Any]:
        """Accept one Action Plan and settle its Supervisor Run atomically."""
        if confirmation_class not in {"safe", "user_required", "invalid"}:
            raise ValueError("unsupported Supervisor confirmation class")
        async with self.database.connection() as connection:
            supervisor = await (await connection.execute(
                """
                SELECT * FROM assistant.runs
                WHERE run_id=%s AND worker_instance_id=%s AND claim_token=%s
                  AND status='running' AND cancel_requested_at IS NULL
                FOR UPDATE
                """,
                (supervisor_run_id, worker_instance_id, claim_token),
            )).fetchone()
            if supervisor is None:
                raise RuntimeError("Supervisor lease was lost before Action Plan commit")
            task = await (await connection.execute(
                "SELECT * FROM assistant.runs WHERE run_id=%s FOR UPDATE",
                (task_id,),
            )).fetchone()
            if task is None or str(task["project_id"]) != str(supervisor["project_id"]):
                raise PermissionError("Supervisor Action Plan Task is unavailable")
            request = dict(task.get("request_json") or {})
            if int(request.get("supervisor_context_epoch") or 0) != int(source_context_epoch):
                raise PermissionError("Supervisor Action Plan belongs to a stale context")
            current_turn = int(request.get("task_turn_count") or 0)
            if source_turn_number and current_turn != int(source_turn_number):
                raise PermissionError("Supervisor Action Plan belongs to a stale Turn")
            accepted = dict(request.get("accepted_documents") or {})
            accepted["current_action_plan"] = json_compatible(action_ref)
            active_delegations = list(accepted.get("active_delegations") or [])
            for delegation in delegations:
                ref = delegation.get("request_ref")
                if ref and ref not in active_delegations:
                    active_delegations.append(json_compatible(ref))
            accepted["active_delegations"] = active_delegations
            state_revision = int(request.get("state_revision") or 0) + 1
            task_status = str(task["status"])
            if task_status == "waiting_supervisor":
                task_status = "waiting_user" if confirmation_class == "user_required" else (
                    "waiting_children" if any(
                        item.get("executor") == "kernel_child_run"
                        for item in delegations
                    ) else "queued"
                )
            request_patch = {
                "accepted_documents": accepted,
                "state_revision": state_revision,
                "degraded_supervision": False,
                "supervisor_action_status": (
                    "auto_applied" if confirmation_class == "safe"
                    else "waiting_user" if confirmation_class == "user_required"
                    else "invalid"
                ),
            }
            pending_validation_turn = int(
                request.get("pending_validation_supervisor_turn") or 0
            )
            if (
                confirmation_class == "safe"
                and pending_validation_turn
                and pending_validation_turn == int(source_turn_number)
            ):
                request_patch.update({
                    "validation_supervisor_approved_turn": pending_validation_turn,
                    "task_phase": "working",
                })
            current_mode = str(
                request.get("work_mode") or request.get("current_stage") or "requirements"
            )
            requested_mode = str(target_work_mode or current_mode)
            allowed_modes = {"requirements", "build", "audit", "operate", "repair"}
            if requested_mode not in allowed_modes:
                raise ValueError("Supervisor selected an unknown Work Mode")
            if requested_mode == "build" and not request.get("requirements_approval"):
                requested_mode = current_mode
            if requested_mode == "audit" and not request.get("validation_artifact"):
                requested_mode = current_mode
            if requested_mode == "operate" and not (
                request.get("publication_state") == "published"
                or request.get("task_phase") == "published"
            ):
                requested_mode = current_mode
            request_patch.update({
                "work_mode": requested_mode,
                "current_stage": requested_mode,
                "execution_plan_variant": requested_mode,
            })
            if session_action == "rotate":
                request_patch["force_session_rotation"] = {
                    "reason": "supervisor_action_plan",
                    "source_action_plan_ref": json_compatible(action_ref),
                }
            elif session_action != "keep":
                raise ValueError("Supervisor selected an unknown Session action")
            updated_task = await (await connection.execute(
                """
                UPDATE assistant.runs
                SET status=%s,available_at=CASE WHEN %s='queued' THEN now() ELSE available_at END,
                    request_json=COALESCE(request_json,'{}'::jsonb) || %s,
                    executor_json=CASE WHEN %s='queued' THEN
                      executor_json - 'resolved_plan_uri' - 'resolved_plan_sha256'
                        - 'prompt_bundle_uri' - 'prompt_bundle_sha256'
                      ELSE executor_json END,
                    updated_at=now(),ended_at=NULL
                WHERE run_id=%s RETURNING *
                """,
                (
                    task_status, task_status, Jsonb(json_compatible(request_patch)),
                    task_status, task_id,
                ),
            )).fetchone()
            created_children: list[dict[str, Any]] = []
            for delegation in delegations:
                if delegation.get("executor") != "kernel_child_run":
                    continue
                delegation_id = str(delegation.get("delegation_id") or "")
                if not delegation_id:
                    raise ValueError("Kernel child delegation requires delegation_id")
                child_id = str(uuid.uuid4())
                child_request = {
                    "schema_version": "child-task.v1",
                    "interaction_kind": "child_task",
                    "parent_task_id": task_id,
                    "delegation": json_compatible(delegation),
                    "delivery_mode": "parent_only",
                }
                child = await (await connection.execute(
                    """
                    INSERT INTO assistant.runs(
                        run_id,project_id,agent_key,trigger_message_id,parent_run_id,
                        run_type,status,conversation_key,request_json,executor_json,
                        idempotency_key
                    ) VALUES (%s,%s,%s,%s,%s,'chat','queued',%s,%s,%s,%s)
                    ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL
                    DO UPDATE SET idempotency_key=EXCLUDED.idempotency_key RETURNING *
                    """,
                    (
                        child_id, task["project_id"],
                        f"project:{task['project_id']}:child",
                        supervisor["trigger_message_id"], task_id,
                        "child-task:" + canonical_sha256([
                            str(task["project_id"]), task_id, delegation_id,
                        ]),
                        Jsonb(child_request),
                        Jsonb({"scheduling_lane": "execution", "may_write_shared": True}),
                        f"child-task:{task_id}:{delegation_id}",
                    ),
                )).fetchone()
                if (
                    str(child["project_id"]) != str(task["project_id"])
                    or str((child["request_json"] or {}).get("parent_task_id") or "") != task_id
                ):
                    raise ValueError("child Task idempotency key was reused")
                created_children.append(child)
            finished = await (await connection.execute(
                """
                UPDATE assistant.runs
                SET status='succeeded',result_json=%s,artifact_refs=%s,
                    worker_id=NULL,worker_instance_id=NULL,claim_token=NULL,
                    lease_until=NULL,process_pid=NULL,process_start_ticks=NULL,
                    ended_at=now(),updated_at=now()
                WHERE run_id=%s RETURNING *
                """,
                (
                    Jsonb(json_compatible(result)),
                    Jsonb(json_compatible(artifact_refs)), supervisor_run_id,
                ),
            )).fetchone()
            await connection.commit()
            return {
                "task": updated_task, "supervisor_run": finished,
                "state_revision": state_revision,
                "children": created_children,
            }

    async def finalize_child_task(
        self, *, run_id: str, worker_instance_id: str, claim_token: str,
        result: dict[str, Any], result_ref: dict[str, Any],
        artifact_refs: list[Any],
    ) -> dict[str, Any]:
        """Finish one parent-only child and resume its parent when the batch settles."""
        async with self.database.connection() as connection:
            child = await (await connection.execute(
                """
                SELECT * FROM assistant.runs
                WHERE run_id=%s AND worker_instance_id=%s AND claim_token=%s
                  AND status='running' AND request_json->>'interaction_kind'='child_task'
                FOR UPDATE
                """,
                (run_id, worker_instance_id, claim_token),
            )).fetchone()
            if child is None or not child.get("parent_run_id"):
                raise RuntimeError("child Task lease or parent was lost")
            parent_id = str(child["parent_run_id"])
            parent = await (await connection.execute(
                "SELECT * FROM assistant.runs WHERE run_id=%s FOR UPDATE",
                (parent_id,),
            )).fetchone()
            if parent is None or str(parent["project_id"]) != str(child["project_id"]):
                raise RuntimeError("child Task parent is unavailable")
            await connection.execute(
                """
                UPDATE assistant.runs
                SET status='succeeded',result_json=%s,artifact_refs=%s,
                    worker_id=NULL,worker_instance_id=NULL,claim_token=NULL,
                    lease_until=NULL,process_pid=NULL,process_start_ticks=NULL,
                    ended_at=now(),updated_at=now()
                WHERE run_id=%s
                """,
                (
                    Jsonb(json_compatible({**result, "result_ref": result_ref})),
                    Jsonb(json_compatible(artifact_refs)), run_id,
                ),
            )
            accepted = dict((parent.get("request_json") or {}).get("accepted_documents") or {})
            active_delegations = list(accepted.get("active_delegations") or [])
            request_ref = dict(
                ((child.get("request_json") or {}).get("delegation") or {}).get(
                    "request_ref"
                ) or {}
            )
            if request_ref:
                active_delegations = [
                    item for item in active_delegations if item != request_ref
                ]
            accepted["active_delegations"] = active_delegations
            child_results = list(accepted.get("child_results") or [])
            if result_ref not in child_results:
                child_results.append(json_compatible(result_ref))
            accepted["child_results"] = child_results
            pending = await (await connection.execute(
                """
                SELECT count(*) AS count FROM assistant.runs
                WHERE parent_run_id=%s AND request_json->>'interaction_kind'='child_task'
                  AND status IN ('queued','running','reconciling')
                  AND run_id<>%s
                """,
                (parent_id, run_id),
            )).fetchone()
            parent_status = "queued" if int(pending["count"] or 0) == 0 else "waiting_children"
            updated_parent = await (await connection.execute(
                """
                UPDATE assistant.runs
                SET status=%s,available_at=CASE WHEN %s='queued' THEN now() ELSE available_at END,
                    request_json=COALESCE(request_json,'{}'::jsonb) || %s,
                    executor_json=CASE WHEN %s='queued' THEN
                      executor_json - 'resolved_plan_uri' - 'resolved_plan_sha256'
                        - 'prompt_bundle_uri' - 'prompt_bundle_sha256'
                      ELSE executor_json END,
                    updated_at=now(),ended_at=NULL
                WHERE run_id=%s AND status='waiting_children' RETURNING *
                """,
                (
                    parent_status, parent_status,
                    Jsonb({
                        "accepted_documents": accepted,
                        "state_revision": int(
                            (parent.get("request_json") or {}).get("state_revision") or 0
                        ) + 1,
                    }),
                    parent_status, parent_id,
                ),
            )).fetchone()
            await connection.commit()
            return {"child_run_id": run_id, "parent": updated_parent}

    async def settle_terminal_child_failure(self, run_id: str) -> dict[str, Any] | None:
        """Release a parent waiting on a child that reached terminal failure."""
        async with self.database.connection() as connection:
            child = await (await connection.execute(
                """
                SELECT * FROM assistant.runs
                WHERE run_id=%s AND request_json->>'interaction_kind'='child_task'
                  AND status IN ('failed','cancelled','dead_letter')
                FOR UPDATE
                """,
                (run_id,),
            )).fetchone()
            if child is None or not child.get("parent_run_id"):
                await connection.commit()
                return None
            parent_id = str(child["parent_run_id"])
            parent = await (await connection.execute(
                "SELECT * FROM assistant.runs WHERE run_id=%s FOR UPDATE",
                (parent_id,),
            )).fetchone()
            if parent is None:
                await connection.commit()
                return None
            pending = await (await connection.execute(
                """
                SELECT count(*) AS count FROM assistant.runs
                WHERE parent_run_id=%s AND request_json->>'interaction_kind'='child_task'
                  AND status IN ('queued','running','reconciling')
                """,
                (parent_id,),
            )).fetchone()
            failures = list((parent.get("request_json") or {}).get("child_failures") or [])
            if run_id not in failures:
                failures.append(run_id)
            accepted = dict((parent.get("request_json") or {}).get("accepted_documents") or {})
            request_ref = dict(
                ((child.get("request_json") or {}).get("delegation") or {}).get(
                    "request_ref"
                ) or {}
            )
            if request_ref:
                accepted["active_delegations"] = [
                    item for item in accepted.get("active_delegations") or []
                    if item != request_ref
                ]
            next_status = "queued" if int(pending["count"] or 0) == 0 else "waiting_children"
            updated = await (await connection.execute(
                """
                UPDATE assistant.runs
                SET status=%s,available_at=CASE WHEN %s='queued' THEN now() ELSE available_at END,
                    request_json=COALESCE(request_json,'{}'::jsonb) || %s,
                    updated_at=now(),ended_at=NULL
                WHERE run_id=%s AND status='waiting_children' RETURNING *
                """,
                (
                    next_status, next_status,
                    Jsonb({
                        "child_failures": failures,
                        "accepted_documents": accepted,
                    }), parent_id,
                ),
            )).fetchone()
            await connection.commit()
            return updated

    async def revoke_project_grant_from_claim(
        self, *, source_run_id: str, worker_instance_id: str,
        claim_token: str, grant_id: str,
    ) -> dict[str, Any]:
        """Revoke a Project grant from a leased user-triggered control Run."""
        async with self.database.connection() as connection:
            source = await (await connection.execute(
                """
                SELECT r.project_id,m.direction,m.message_type,m.user_id,m.message_id
                FROM assistant.runs r
                JOIN assistant.messages m ON m.message_id=r.trigger_message_id
                WHERE r.run_id=%s AND r.worker_instance_id=%s AND r.claim_token=%s
                  AND r.status='running' AND r.cancel_requested_at IS NULL
                FOR UPDATE OF r
                """,
                (source_run_id, worker_instance_id, claim_token),
            )).fetchone()
            if (
                source is None or source["direction"] != "inbound"
                or source["message_type"] not in {"text", "answer"}
                or not source["user_id"]
            ):
                raise PermissionError("grant revocation requires a current user request")
            grant = await (await connection.execute(
                """
                UPDATE assistant.project_grants
                SET status='revoked',revoked_at=now()
                WHERE grant_id=%s AND project_id=%s AND status='active'
                RETURNING grant_id,project_id,resource_key,grant_kind
                """,
                (grant_id, source["project_id"]),
            )).fetchone()
            if grant is None:
                raise ValueError("active Project grant was not found")
            affected = list(await (await connection.execute(
                """
                UPDATE assistant.runs SET
                    status=CASE WHEN status IN ('queued','waiting_user','waiting_supervisor','waiting_children','blocked')
                        THEN 'cancelled' ELSE status END,
                    cancel_requested_at=COALESCE(cancel_requested_at,now()),
                    ended_at=CASE WHEN status IN ('queued','waiting_user','waiting_supervisor','waiting_children','blocked')
                        THEN now() ELSE ended_at END,
                    updated_at=now()
                WHERE project_id=%s AND run_id<>%s
                  AND status IN ('queued','running','waiting_user','waiting_supervisor','waiting_children','blocked')
                  AND COALESCE(executor_json->'active_file_grant_ids','[]'::jsonb) ? %s
                RETURNING run_id,status
                """,
                (source["project_id"], source_run_id, grant_id),
            )).fetchall())
            await connection.commit()
            return {
                "grant_id": str(grant["grant_id"]),
                "project_id": grant["project_id"],
                "resource_key": grant["resource_key"],
                "grant_kind": grant["grant_kind"],
                "status": "revoked",
                "affected_run_ids": [str(item["run_id"]) for item in affected],
            }

    async def handoff_from_claim(
        self, *, source_run_id: str, worker_instance_id: str, claim_token: str,
        target_project_id: str, handoff_summary: str,
        target_agent_key: str | None = None,
        target_channel: str | None = None,
        target_chat_id: str | None = None,
        target_user_id: str | None = None,
    ) -> dict[str, Any]:
        if not handoff_summary.strip():
            raise ValueError("handoff_summary is required")
        target_agent_key = target_agent_key or (
            "system-butler" if target_project_id == "system"
            else f"project:{target_project_id}:manager"
        )
        message_id = str(uuid.uuid4())
        run_id = str(uuid.uuid4())
        async with self.database.connection() as connection:
            source = await (
                await connection.execute(
                    """
                    SELECT r.*,m.channel,m.chat_id,m.user_id,m.message_id AS source_message_id,
                           m.content_json AS source_content
                    FROM assistant.runs r
                    JOIN assistant.messages m ON m.message_id=r.trigger_message_id
                    WHERE r.run_id=%s AND r.worker_instance_id=%s AND r.claim_token=%s
                      AND r.status='running' AND r.cancel_requested_at IS NULL
                    FOR UPDATE OF r
                    """,
                    (source_run_id, worker_instance_id, claim_token),
                )
            ).fetchone()
            if source is None:
                raise PermissionError("the scoped source run is no longer leased")
            destination_channel = target_channel or str(source["channel"])
            destination_chat_id = target_chat_id or str(source["chat_id"])
            destination_user_id = (
                target_user_id if target_user_id is not None else source["user_id"]
            )
            key = f"handoff:{source_run_id}:{target_project_id}:{target_agent_key}"
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (
                    f"route:{destination_channel}:{destination_chat_id}:"
                    f"{destination_user_id or ''}",
                ),
            )
            route_content = {
                "schema_version": "route-binding.v1",
                "status": "active",
                "project_id": target_project_id,
                "agent_key": target_agent_key,
                "source_run_id": source_run_id,
                "source_message_id": str(source["source_message_id"]),
                "reason": (
                    "user_requested_agent_handoff"
                    if str(source["project_id"]) == target_project_id
                    else "user_requested_project_handoff"
                ),
            }
            route_binding = await (
                await connection.execute(
                    """
                    INSERT INTO assistant.messages(
                        message_id,project_id,channel,chat_id,user_id,direction,message_type,
                        content_json,delivery_status,idempotency_key
                    ) VALUES (%s,%s,%s,%s,%s,'system','route_binding',%s,'received',%s)
                    ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL
                    DO UPDATE SET idempotency_key=EXCLUDED.idempotency_key RETURNING *
                    """,
                    (
                        str(uuid.uuid4()), target_project_id, destination_channel,
                        destination_chat_id, destination_user_id, Jsonb(route_content),
                        f"route:{key}",
                    ),
                )
            ).fetchone()
            if (route_binding["content_json"] or {}) != route_content:
                raise ValueError("handoff route idempotency key was reused with different content")
            message = await (
                await connection.execute(
                    """
                    INSERT INTO assistant.messages(
                        message_id,project_id,channel,chat_id,user_id,direction,message_type,
                        external_event_id,reply_to_message_id,content_json,delivery_status,
                        idempotency_key
                    ) VALUES (%s,%s,%s,%s,%s,'inbound','handoff',NULL,NULL,%s,'received',%s)
                    ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL
                    DO UPDATE SET idempotency_key=EXCLUDED.idempotency_key RETURNING *
                    """,
                    (
                        message_id, target_project_id, destination_channel,
                        destination_chat_id, destination_user_id,
                        Jsonb({
                            "text": handoff_summary.strip(),
                            "source_project_id": str(source["project_id"]),
                            "source_run_id": source_run_id,
                            "source_message_id": str(source["source_message_id"]),
                            "source_content": json_compatible(source["source_content"]),
                        }),
                        key,
                    ),
                )
            ).fetchone()
            target_run = await (
                await connection.execute(
                    """
                    INSERT INTO assistant.runs(
                        run_id,project_id,agent_key,trigger_message_id,parent_run_id,
                        run_type,status,conversation_key,request_json,executor_json,idempotency_key
                    ) VALUES (%s,%s,%s,%s,%s,'chat','queued',%s,%s,'{}'::jsonb,%s)
                    ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL
                    DO UPDATE SET idempotency_key=EXCLUDED.idempotency_key RETURNING *
                    """,
                    (
                        run_id, target_project_id,
                        target_agent_key, message["message_id"],
                        source_run_id,
                        conversation_key(
                            target_project_id, target_agent_key,
                            destination_channel, destination_chat_id,
                        ),
                        Jsonb({"handoff_source_run_id": source_run_id}),
                        f"run:{key}",
                    ),
                )
            ).fetchone()
            await connection.execute(
                "UPDATE assistant.messages SET run_id=%s WHERE message_id=%s",
                (target_run["run_id"], message["message_id"]),
            )
            await connection.commit()
            return {"message": message, "run": target_run, "route_binding": route_binding}

    async def claimed_run_source(
        self, *, run_id: str, worker_instance_id: str, claim_token: str,
    ) -> dict[str, Any]:
        """Return the immutable trigger context for one currently leased Run."""
        async with self.database.connection() as connection:
            row = await (
                await connection.execute(
                    """
                    SELECT r.run_id,r.project_id,m.message_id,m.channel,m.chat_id,m.user_id,
                           m.content_json
                    FROM assistant.runs r
                    JOIN assistant.messages m ON m.message_id=r.trigger_message_id
                    WHERE r.run_id=%s AND r.worker_instance_id=%s AND r.claim_token=%s
                      AND r.status='running' AND r.cancel_requested_at IS NULL
                    """,
                    (run_id, worker_instance_id, claim_token),
                )
            ).fetchone()
        if row is None:
            raise PermissionError("the scoped source run is no longer leased")
        return row

    async def claim_run(
        self, worker_id: str, worker_instance_id: str, lease_seconds: int
    ) -> dict[str, Any] | None:
        claim_token = str(uuid.uuid4())
        try:
            async with self.database.connection() as connection:
                cursor = await connection.execute(
                """
                WITH candidate AS (
                    SELECT r.run_id FROM assistant.runs r
                    WHERE r.status='queued' AND r.available_at<=now()
                      AND NOT EXISTS (
                          SELECT 1 FROM assistant.runs active
                          WHERE active.conversation_key=r.conversation_key
                            AND active.status IN ('running','reconciling')
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM assistant.runs parent
                          WHERE parent.run_id=r.parent_run_id
                            AND parent.status IN ('running','reconciling')
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM assistant.runs project_active
                          WHERE project_active.project_id=r.project_id
                            AND project_active.status IN ('running','reconciling')
                            AND assistant.run_lane(
                                  project_active.run_type,
                                  project_active.agent_key,
                                  project_active.executor_json->>'scheduling_lane'
                                )='execution'
                            AND assistant.run_lane(
                                  r.run_type, r.agent_key,
                                  r.executor_json->>'scheduling_lane'
                                )='execution'
                      )
                    ORDER BY
                        assistant.run_lane(
                            r.run_type, r.agent_key,
                            r.executor_json->>'scheduling_lane'
                        )='conversation' DESC,
                        r.created_at
                    FOR UPDATE SKIP LOCKED LIMIT 1
                )
                UPDATE assistant.runs r
                SET status='running',attempt=attempt+1,worker_id=%s,worker_instance_id=%s,
                    claim_token=%s,process_pid=NULL,process_start_ticks=NULL,
                    lease_until=now()+(%s*interval '1 second'),
                    started_at=COALESCE(started_at,now()),updated_at=now()
                FROM candidate c WHERE r.run_id=c.run_id RETURNING r.*
                """,
                (worker_id, worker_instance_id, claim_token, lease_seconds),
            )
                row = await cursor.fetchone()
                await connection.commit()
                return row
        except UniqueViolation:
            # Another worker won the Project/conversation active-run race.
            return None

    async def active_execution_blocker(self, project_id: str) -> dict[str, Any] | None:
        """Return the oldest active execution-lane Run without blocking PM chat."""
        async with self.database.connection() as connection:
            return await (
                await connection.execute(
                    """
                    SELECT * FROM assistant.runs r
                    WHERE r.project_id=%s AND r.status IN ('running','reconciling')
                      AND assistant.run_lane(
                            r.run_type, r.agent_key,
                            r.executor_json->>'scheduling_lane'
                          )='execution'
                    ORDER BY r.created_at,r.run_id LIMIT 1
                    """,
                    (project_id,),
                )
            ).fetchone()

    async def current_project_task(self, project_id: str) -> dict[str, Any] | None:
        """Return the single current task, including queued and human-waiting states."""
        async with self.database.connection() as connection:
            return await (
                await connection.execute(
                    """
                    SELECT * FROM assistant.runs r
                    WHERE r.project_id=%s
                      AND (
                        (
                          COALESCE(r.request_json->>'task_protocol','') IN ('task.v1','single-agent.v2')
                          AND r.status IN ('queued','running','waiting_user','waiting_supervisor','waiting_children','blocked','reconciling')
                        ) OR (
                          r.status IN ('queued','running','reconciling')
                          AND assistant.run_lane(
                                r.run_type, r.agent_key,
                                r.executor_json->>'scheduling_lane'
                              )='execution'
                        )
                      )
                    ORDER BY
                      CASE r.status
                        WHEN 'reconciling' THEN 0 WHEN 'running' THEN 1
                        WHEN 'waiting_user' THEN 2
                        WHEN 'waiting_supervisor' THEN 3
                        WHEN 'waiting_children' THEN 4
                        WHEN 'blocked' THEN 5 ELSE 6
                      END,
                      r.created_at,r.run_id
                    LIMIT 1
                    """,
                    (project_id,),
                )
            ).fetchone()

    async def validate_current_waiting_message(
        self, *, message_id: str, project_id: str, channel: str, chat_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        """Fence a user interaction before any external/file-side effect."""
        async with self.database.connection() as connection:
            row = await (
                await connection.execute(
                    """
                    SELECT m.*,r.status AS run_status,r.run_type,
                           r.request_json,r.waiting_question_message_id,
                           r.interaction_seq AS run_interaction_seq,r.result_json AS run_result_json
                    FROM assistant.messages m
                    JOIN assistant.runs r ON r.run_id=m.run_id
                    WHERE m.message_id=%s AND m.project_id=%s AND m.channel=%s
                      AND m.chat_id=%s AND m.user_id IS NOT DISTINCT FROM %s
                      AND m.direction='outbound' AND m.message_type IN ('question','approval')
                      AND r.status='waiting_user'
                    FOR UPDATE OF r
                    """,
                    (message_id, project_id, channel, chat_id, user_id),
                )
            ).fetchone()
            if row is None:
                raise PermissionError("interaction is no longer waiting or belongs to another user")
            if self._retired_stage(project_id, row):
                raise PermissionError("旧阶段交互已退役")
            current_ids = [
                str(item) for item in (row.get("run_result_json") or {}).get("question_message_ids") or []
            ]
            current = (
                message_id in current_ids
                if current_ids else str(row.get("waiting_question_message_id")) == message_id
            )
            if not current:
                raise PermissionError("interaction is no longer current")
            reset = await (
                await connection.execute(
                    """
                    SELECT 1 FROM assistant.messages
                    WHERE project_id=%s AND channel=%s AND chat_id=%s
                      AND direction='system' AND message_type='conversation_reset'
                      AND created_at>%s LIMIT 1
                    """,
                    (project_id, channel, chat_id, row["created_at"]),
                )
            ).fetchone()
            if reset is not None:
                raise PermissionError("interaction expired after /new")
            await connection.commit()
            return row

    async def quarantine_expired_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        """Move abandoned executions to an explicit reconciliation state.

        Unknown work is never replayed automatically because the old process may
        already have changed files or an external system.
        """
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                """
                WITH expired AS (
                    SELECT run_id FROM assistant.runs
                    WHERE status='running' AND lease_until<now()
                    ORDER BY lease_until FOR UPDATE SKIP LOCKED LIMIT %s
                )
                UPDATE assistant.runs r
                SET status='reconciling',worker_id=NULL,worker_instance_id=NULL,
                    claim_token=NULL,lease_until=NULL,updated_at=now(),
                    error_json=COALESCE(error_json,'{}'::jsonb) ||
                        '{"type":"LeaseExpired","message":"execution outcome requires reconciliation"}'::jsonb
                FROM expired e WHERE r.run_id=e.run_id RETURNING r.*
                """,
                (min(max(limit, 1), 100),),
            )
            rows = list(await cursor.fetchall())
            for row in rows:
                await self._update_publish_parent(
                    connection, row, status="blocked",
                    phase="publication_reconciling",
                    publication_state="reconciling",
                )
            await connection.commit()
            return rows

    async def heartbeat_run(
        self, run_id: str, worker_instance_id: str, claim_token: str, lease_seconds: int
    ) -> str:
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                """
                UPDATE assistant.runs SET lease_until=now()+(%s*interval '1 second'),updated_at=now()
                WHERE run_id=%s AND worker_instance_id=%s AND claim_token=%s AND status='running'
                RETURNING cancel_requested_at
                """,
                (lease_seconds, run_id, worker_instance_id, claim_token),
            )
            row = await cursor.fetchone()
            await connection.commit()
            if row is None:
                return "lease_lost"
            return "cancel_requested" if row["cancel_requested_at"] else "active"

    async def record_process_identity(
        self, run_id: str, worker_instance_id: str, claim_token: str,
        pid: int, start_ticks: int | None,
    ) -> bool:
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                """
                UPDATE assistant.runs SET process_pid=%s,process_start_ticks=%s,updated_at=now()
                WHERE run_id=%s AND worker_instance_id=%s AND claim_token=%s
                  AND status='running' RETURNING run_id
                """,
                (pid, start_ticks, run_id, worker_instance_id, claim_token),
            )
            updated = await cursor.fetchone() is not None
            await connection.commit()
            return updated

    async def run_claim_is_active(
        self, run_id: str, worker_instance_id: str, claim_token: str,
    ) -> bool:
        async with self.database.connection() as connection:
            row = await (await connection.execute(
                """
                SELECT 1 FROM assistant.runs
                WHERE run_id=%s AND worker_instance_id=%s AND claim_token=%s
                  AND status='running' AND cancel_requested_at IS NULL
                  AND lease_until>now()
                """,
                (run_id, worker_instance_id, claim_token),
            )).fetchone()
        return row is not None

    async def bind_run_execution(
        self, run_id: str, worker_instance_id: str, claim_token: str,
        session_id: str, trigger_message_id: str, executor_patch: dict[str, Any],
    ) -> bool:
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                """
                UPDATE assistant.runs
                SET session_id=%s,executor_json=executor_json || %s,
                    harness=%s,provider=%s,model=%s,reasoning_effort=%s,auth_mode=%s,
                    updated_at=now()
                WHERE run_id=%s AND worker_instance_id=%s AND claim_token=%s
                  AND status='running' AND cancel_requested_at IS NULL RETURNING run_id
                """,
                (
                    session_id, Jsonb(json_compatible(executor_patch)),
                    executor_patch.get("harness"), executor_patch.get("provider"),
                    executor_patch.get("model"), executor_patch.get("reasoning_effort"),
                    executor_patch.get("auth_mode"), run_id,
                    worker_instance_id, claim_token,
                ),
            )
            if await cursor.fetchone() is None:
                await connection.rollback()
                return False
            await connection.execute(
                "UPDATE assistant.messages SET session_id=%s WHERE message_id=%s",
                (session_id, trigger_message_id),
            )
            await connection.execute(
                """
                UPDATE assistant.sessions
                SET harness=%s,provider=%s,model=%s,reasoning_effort=%s,auth_mode=%s,
                    updated_at=now()
                WHERE session_id=%s
                """,
                (
                    executor_patch.get("harness"), executor_patch.get("provider"),
                    executor_patch.get("model"), executor_patch.get("reasoning_effort"),
                    executor_patch.get("auth_mode"), session_id,
                ),
            )
            await connection.commit()
            return True

    async def start_model_invocation(
        self, *, invocation_id: str, project_id: str, run_id: str | None,
        session_id: str | None, actor_role: str, harness: str, provider: str,
        model: str, reasoning_effort: str | None, auth_mode: str | None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        async with self.database.connection() as connection:
            await connection.execute(
                """
                INSERT INTO assistant.model_invocations(
                    invocation_id,project_id,run_id,session_id,actor_role,harness,
                    provider,model,reasoning_effort,auth_mode,status,metadata_json
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'running',%s)
                ON CONFLICT (invocation_id) DO NOTHING
                """,
                (
                    invocation_id, project_id, run_id, session_id, actor_role, harness,
                    provider, model, reasoning_effort, auth_mode,
                    Jsonb(json_compatible(metadata or {})),
                ),
            )
            await connection.commit()

    async def finish_model_invocation(
        self, *, invocation_id: str, status: str,
        native_session_id: str | None = None, input_tokens: int = 0,
        cached_input_tokens: int = 0, output_tokens: int = 0,
        reasoning_tokens: int = 0, trace_uri: str | None = None,
        trace_sha256: str | None = None, error_type: str | None = None,
        error_message: str | None = None, metadata: dict[str, Any] | None = None,
    ) -> None:
        if status not in {"succeeded", "failed", "cancelled"}:
            raise ValueError("unsupported model invocation terminal status")
        async with self.database.connection() as connection:
            await connection.execute(
                """
                UPDATE assistant.model_invocations
                SET status=%s,native_session_id=COALESCE(%s,native_session_id),
                    input_tokens=%s,cached_input_tokens=%s,output_tokens=%s,
                    reasoning_tokens=%s,trace_uri=COALESCE(%s,trace_uri),
                    trace_sha256=COALESCE(%s,trace_sha256),error_type=%s,error_message=%s,
                    metadata_json=metadata_json || %s,ended_at=now(),updated_at=now()
                WHERE invocation_id=%s
                """,
                (
                    status, native_session_id, max(input_tokens, 0),
                    max(cached_input_tokens, 0), max(output_tokens, 0),
                    max(reasoning_tokens, 0), trace_uri, trace_sha256, error_type,
                    (error_message or "")[:1000] or None,
                    Jsonb(json_compatible(metadata or {})), invocation_id,
                ),
            )
            await connection.commit()

    async def record_turn_usage(
        self, *, run_id: str, worker_instance_id: str, claim_token: str,
        session_id: str, native_session_id: str, input_tokens: int,
        context_window_tokens: int, archive_uri: str | None,
        archive_sha256: str | None, usage_snapshot: dict[str, Any],
    ) -> bool:
        """Persist one completed executor invocation before result validation."""
        async with self.database.connection() as connection:
            run = await (
                await connection.execute(
                    """
                    SELECT executor_json FROM assistant.runs
                    WHERE run_id=%s AND worker_instance_id=%s AND claim_token=%s
                      AND status='running' AND cancel_requested_at IS NULL FOR UPDATE
                    """,
                    (run_id, worker_instance_id, claim_token),
                )
            ).fetchone()
            if run is None:
                await connection.rollback()
                return False
            executor = dict(run["executor_json"] or {})
            history = list(executor.get("usage_history") or [])
            invocation_id = str(usage_snapshot.get("invocation_id") or "")
            if not invocation_id:
                raise ValueError("usage snapshot requires invocation_id")
            if not any(
                str(item.get("invocation_id") or "") == invocation_id for item in history
            ):
                history.append(json_compatible(usage_snapshot))
            executor["usage_history"] = history
            usage_known = usage_snapshot.get("context_usage_known") is not False
            session = await (
                await connection.execute(
                    """
                    UPDATE assistant.sessions
                    SET native_session_id=%s,input_tokens=%s,context_window_tokens=%s,
                        usage_known=%s,usage_source=%s,last_invocation_id=%s,
                        archive_uri=COALESCE(%s,archive_uri),
                        archive_sha256=COALESCE(%s,archive_sha256),updated_at=now()
                    WHERE session_id=%s AND status='active' RETURNING session_id
                    """,
                    (
                        native_session_id, max(input_tokens, 0), context_window_tokens,
                        usage_known, usage_snapshot.get("usage_source"), invocation_id,
                        archive_uri, archive_sha256, session_id,
                    ),
                )
            ).fetchone()
            if session is None:
                raise RuntimeError("active session was not found while recording usage")
            updated = await (
                await connection.execute(
                    """
                    UPDATE assistant.runs SET executor_json=%s,updated_at=now()
                    WHERE run_id=%s AND worker_instance_id=%s AND claim_token=%s
                      AND status='running' RETURNING run_id
                    """,
                    (
                        Jsonb(json_compatible(executor)), run_id,
                        worker_instance_id, claim_token,
                    ),
                )
            ).fetchone()
            if updated is None:
                raise RuntimeError("run lease was lost while recording usage")
            await connection.commit()
            return True

    async def finalize_agent_completed(
        self, *, run_id: str, worker_instance_id: str, claim_token: str,
        session_id: str, native_session_id: str, input_tokens: int,
        context_window_tokens: int, archive_uri: str | None,
        archive_sha256: str | None, project_id: str, channel: str, chat_id: str,
        user_id: str | None, source_message_id: str, summary: str,
        result: dict[str, Any], artifact_refs: list[Any],
        usage_snapshot: dict[str, Any], advance_stage: bool = True,
    ) -> dict[str, Any]:
        """Commit session progress, reply, and run success under one lease fence."""
        async with self.database.connection() as connection:
            locked = await (
                await connection.execute(
                    """
                    SELECT * FROM assistant.runs
                    WHERE run_id=%s AND worker_instance_id=%s AND claim_token=%s
                      AND status='running' AND cancel_requested_at IS NULL FOR UPDATE
                    """,
                    (run_id, worker_instance_id, claim_token),
                )
            ).fetchone()
            if locked is None:
                raise RuntimeError("run lease was lost before completion")
            session = await (
                await connection.execute(
                    """
                    UPDATE assistant.sessions
                    SET native_session_id=%s,input_tokens=%s,context_window_tokens=%s,
                        archive_uri=COALESCE(%s,archive_uri),
                        archive_sha256=COALESCE(%s,archive_sha256),updated_at=now()
                    WHERE session_id=%s AND status='active' RETURNING *
                    """,
                    (
                        native_session_id, max(input_tokens, 0), context_window_tokens,
                        archive_uri, archive_sha256, session_id,
                    ),
                )
            ).fetchone()
            if session is None:
                raise RuntimeError("active session was not found")
            reply_id = str(uuid.uuid4())
            idempotency_key = f"reply:{run_id}:{source_message_id}"
            prior_reply = await (
                await connection.execute(
                    """
                    SELECT message_id FROM assistant.messages
                    WHERE project_id=%s AND channel=%s AND chat_id=%s
                      AND direction='outbound' AND message_type='reply'
                      AND reply_to_message_id=%s
                    ORDER BY created_at DESC,message_id DESC LIMIT 1
                    """,
                    (project_id, channel, chat_id, source_message_id),
                )
            ).fetchone()
            reply_target_id = (
                str(prior_reply["message_id"]) if prior_reply else source_message_id
            )
            reply = await (
                await connection.execute(
                    """
                    INSERT INTO assistant.messages(
                        message_id,project_id,session_id,run_id,channel,chat_id,user_id,
                        direction,message_type,reply_to_message_id,content_json,
                        delivery_status,idempotency_key
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,'outbound','reply',%s,%s,'pending',%s)
                    ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL
                    DO UPDATE SET idempotency_key=EXCLUDED.idempotency_key RETURNING *
                    """,
                    (
                        reply_id, project_id, session_id, run_id, channel, chat_id,
                        user_id, reply_target_id, Jsonb({"text": summary}), idempotency_key,
                    ),
                )
            ).fetchone()
            if (reply["content_json"] or {}) != {"text": summary}:
                raise ValueError("the persisted reply does not match this run result")
            next_run = None
            locked_request = dict(locked["request_json"] or {})
            stage_plan = [str(item) for item in locked_request.get("stage_plan") or []]
            stage_index = int(locked_request.get("stage_index") or 0)
            final_result = dict(result)
            if advance_stage and locked["run_type"] == "stage" and stage_index + 1 < len(stage_plan):
                next_stage = stage_plan[stage_index + 1]
                next_run_id = str(uuid.uuid4())
                root_id = str(locked_request.get("task_root_run_id") or run_id)
                next_request = {
                    **locked_request,
                    "stage_index": stage_index + 1,
                    "task_root_run_id": root_id,
                    "prior_artifacts": json_compatible(
                        list(locked_request.get("prior_artifacts") or []) + artifact_refs
                    ),
                }
                next_run = await (
                    await connection.execute(
                        """
                        INSERT INTO assistant.runs(
                            run_id,project_id,agent_key,trigger_message_id,parent_run_id,
                            run_type,stage,status,conversation_key,request_json,
                            executor_json,idempotency_key
                        ) VALUES (%s,%s,%s,%s,%s,'stage',%s,'queued',%s,%s,%s,%s)
                        ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL
                        DO UPDATE SET idempotency_key=EXCLUDED.idempotency_key RETURNING *
                        """,
                        (
                            next_run_id, project_id, f"project:{project_id}:{next_stage}",
                            reply["message_id"], run_id, next_stage,
                            conversation_key(
                                project_id, f"project:{project_id}:{next_stage}", channel, chat_id
                            ),
                            Jsonb(next_request),
                            Jsonb(stage_executor(next_request, next_stage)),
                            f"task-stage:{root_id}:{stage_index + 1}:{next_stage}",
                        ),
                    )
                ).fetchone()
                final_result["next_run_id"] = str(next_run["run_id"])
            updated = await (
                await connection.execute(
                    """
                    UPDATE assistant.runs
                    SET status='succeeded',result_json=%s,artifact_refs=%s,
                        error_json=NULL,
                        worker_id=NULL,worker_instance_id=NULL,claim_token=NULL,
                        lease_until=NULL,process_pid=NULL,process_start_ticks=NULL,
                        updated_at=now(),ended_at=now()
                    WHERE run_id=%s AND worker_instance_id=%s AND claim_token=%s
                      AND status='running' RETURNING run_id
                    """,
                    (
                        Jsonb(json_compatible(final_result)), Jsonb(json_compatible(artifact_refs)),
                        run_id, worker_instance_id, claim_token,
                    ),
                )
            ).fetchone()
            if updated is None:
                raise RuntimeError("run lease was lost before completion")
            await connection.commit()
            return {"reply": reply, "next_run": next_run}

    async def finalize_review_request_changes(
        self, *, run_id: str, worker_instance_id: str, claim_token: str,
        project_id: str, source_message_id: str, summary: str,
        issues: list[dict[str, Any]], artifact_refs: list[Any],
    ) -> dict[str, Any]:
        """Finish one read-only Review and re-run the preceding pinned stage."""
        async with self.database.connection() as connection:
            locked = await (
                await connection.execute(
                    """
                    SELECT r.*,m.channel,m.chat_id,m.user_id
                    FROM assistant.runs r
                    JOIN assistant.messages m ON m.message_id=r.trigger_message_id
                    WHERE r.run_id=%s AND r.worker_instance_id=%s AND r.claim_token=%s
                      AND r.status='running' AND r.cancel_requested_at IS NULL
                      AND r.stage='review'
                    FOR UPDATE OF r
                    """,
                    (run_id, worker_instance_id, claim_token),
                )
            ).fetchone()
            if locked is None:
                raise RuntimeError("Review Run lease was lost before requesting changes")
            request = dict(locked["request_json"] or {})
            stage_plan = [str(item) for item in request.get("stage_plan") or []]
            target_stage = review_rework_stage(stage_plan)
            root_id = str(request.get("task_root_run_id") or run_id)
            round_number = int(request.get("stage_round") or 1) + 1
            reply = await (
                await connection.execute(
                    """
                    INSERT INTO assistant.messages(
                        message_id,project_id,run_id,channel,chat_id,user_id,direction,
                        message_type,reply_to_message_id,content_json,delivery_status,idempotency_key
                    ) VALUES (%s,%s,%s,%s,%s,%s,'outbound','reply',%s,%s,'pending',%s)
                    ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL
                    DO UPDATE SET idempotency_key=EXCLUDED.idempotency_key RETURNING *
                    """,
                    (
                        str(uuid.uuid4()), project_id, run_id, locked["channel"],
                        locked["chat_id"], locked["user_id"], source_message_id,
                        Jsonb({"text": summary, "issues": json_compatible(issues)}),
                        f"review-request-changes:{run_id}:{round_number}",
                    ),
                )
            ).fetchone()
            updated = await (
                await connection.execute(
                    """
                    UPDATE assistant.runs SET status='succeeded',result_json=%s,artifact_refs=%s,
                        worker_id=NULL,worker_instance_id=NULL,claim_token=NULL,lease_until=NULL,
                        process_pid=NULL,process_start_ticks=NULL,updated_at=now(),ended_at=now()
                    WHERE run_id=%s AND worker_instance_id=%s AND claim_token=%s
                      AND status='running' RETURNING run_id
                    """,
                    (
                        Jsonb({"outcome": "request_changes", "summary": summary,
                               "issues": json_compatible(issues)}),
                        Jsonb(json_compatible(artifact_refs)), run_id,
                        worker_instance_id, claim_token,
                    ),
                )
            ).fetchone()
            if updated is None:
                raise RuntimeError("Review Run lease was lost")
            next_request = {
                **request,
                "stage_index": stage_plan.index(target_stage),
                "stage_round": round_number,
                "review_feedback": {"source_review_run_id": run_id, "issues": issues},
                "prior_artifacts": json_compatible(
                    list(request.get("prior_artifacts") or []) + artifact_refs
                ),
            }
            next_run = await (
                await connection.execute(
                    """
                    INSERT INTO assistant.runs(
                        run_id,project_id,agent_key,trigger_message_id,parent_run_id,
                        run_type,stage,status,conversation_key,request_json,
                        executor_json,idempotency_key
                    ) VALUES (%s,%s,%s,%s,%s,'stage',%s,'queued',%s,%s,%s,%s)
                    ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL
                    DO UPDATE SET idempotency_key=EXCLUDED.idempotency_key RETURNING *
                    """,
                    (
                        str(uuid.uuid4()), project_id, f"project:{project_id}:{target_stage}",
                        reply["message_id"], run_id, target_stage,
                        conversation_key(
                            project_id, f"project:{project_id}:{target_stage}",
                            str(locked["channel"]), str(locked["chat_id"]),
                        ),
                        Jsonb(json_compatible(next_request)),
                        Jsonb(stage_executor(next_request, target_stage)),
                        f"task-rework:{root_id}:{round_number}:{target_stage}",
                    ),
                )
            ).fetchone()
            await connection.execute(
                "UPDATE assistant.messages SET run_id=%s WHERE message_id=%s",
                (next_run["run_id"], reply["message_id"]),
            )
            await connection.commit()
            return {
                "outcome": "request_changes",
                "reply_message_id": str(reply["message_id"]),
                "next_run_id": str(next_run["run_id"]),
                "stage_round": round_number,
            }

    async def finalize_implementation_validation_rework(
        self, *, run_id: str, worker_instance_id: str, claim_token: str,
        summary: str, validation_artifact: dict[str, Any],
        artifact_refs: list[Any], max_reworks: int = 2,
    ) -> dict[str, Any]:
        """Finish an Implementation attempt and create a bounded fresh rework Run."""
        async with self.database.connection() as connection:
            locked = await (
                await connection.execute(
                    """
                    SELECT r.*,m.channel,m.chat_id,m.user_id
                    FROM assistant.runs r
                    JOIN assistant.messages m ON m.message_id=r.trigger_message_id
                    WHERE r.run_id=%s AND r.worker_instance_id=%s AND r.claim_token=%s
                      AND r.status='running' AND r.cancel_requested_at IS NULL
                      AND r.run_type='stage'
                      AND r.stage='implementation' FOR UPDATE OF r
                    """,
                    (run_id, worker_instance_id, claim_token),
                )
            ).fetchone()
            if locked is None:
                raise RuntimeError("Implementation Run lease was lost before validation rework")
            request = dict(locked["request_json"] or {})
            rework_count = int(request.get("validation_rework_count") or 0)
            report_status = str(
                validation_artifact.get("release_gate")
                or validation_artifact.get("status")
                or "failed"
            )
            result = {
                "outcome": "validation_failed",
                "summary": summary,
                "validation_status": report_status,
                "validation_artifact": json_compatible(validation_artifact),
                "validation_rework_count": rework_count,
            }
            if rework_count >= max_reworks:
                reply = await (
                    await connection.execute(
                        """
                        INSERT INTO assistant.messages(
                            message_id,project_id,run_id,channel,chat_id,user_id,direction,
                            message_type,reply_to_message_id,content_json,delivery_status,
                            idempotency_key
                        ) VALUES (%s,%s,%s,%s,%s,%s,'outbound','reply',%s,%s,'pending',%s)
                        ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL
                        DO UPDATE SET idempotency_key=EXCLUDED.idempotency_key RETURNING *
                        """,
                        (
                            str(uuid.uuid4()), locked["project_id"], run_id,
                            locked["channel"], locked["chat_id"], locked["user_id"],
                            locked["trigger_message_id"],
                            Jsonb({
                                "text": summary,
                                "outcome": "blocked",
                                "reason": "validation_rework_limit",
                            }),
                            f"validation-blocked-reply:{run_id}",
                        ),
                    )
                ).fetchone()
                result["outcome"] = "blocked"
                result["reason"] = "validation_rework_limit"
                await connection.execute(
                    """
                    UPDATE assistant.runs
                    SET status='blocked',result_json=%s,artifact_refs=%s,error_json=NULL,
                        worker_id=NULL,worker_instance_id=NULL,claim_token=NULL,
                        lease_until=NULL,process_pid=NULL,process_start_ticks=NULL,
                        updated_at=now(),ended_at=NULL
                    WHERE run_id=%s
                    """,
                    (
                        Jsonb(result), Jsonb(json_compatible(artifact_refs)), run_id,
                    ),
                )
                await connection.commit()
                return {
                    "outcome": "blocked",
                    "status": "blocked",
                    "reply": reply,
                    "next_run": None,
                }

            next_count = rework_count + 1
            root_id = str(request.get("task_root_run_id") or run_id)
            handoff_key = f"validation-rework:{root_id}:{next_count}"
            handoff = await (
                await connection.execute(
                    """
                    INSERT INTO assistant.messages(
                        message_id,project_id,channel,chat_id,user_id,direction,message_type,
                        content_json,delivery_status,idempotency_key
                    ) VALUES (%s,%s,%s,%s,%s,'system','validation_rework',%s,'received',%s)
                    ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL
                    DO UPDATE SET idempotency_key=EXCLUDED.idempotency_key RETURNING *
                    """,
                    (
                        str(uuid.uuid4()), locked["project_id"], locked["channel"],
                        locked["chat_id"], locked["user_id"],
                        Jsonb({
                            "source_run_id": run_id,
                            "validation_artifact": json_compatible(validation_artifact),
                            "summary": summary,
                        }),
                        handoff_key,
                    ),
                )
            ).fetchone()
            next_request = {
                **request,
                "stage_index": int(request.get("stage_index") or 0),
                "task_root_run_id": root_id,
                "validation_rework_count": next_count,
                "validation_issue": json_compatible(validation_artifact),
                "prior_artifacts": json_compatible(
                    list(request.get("prior_artifacts") or []) + artifact_refs
                ),
            }
            next_run = await (
                await connection.execute(
                    """
                    INSERT INTO assistant.runs(
                        run_id,project_id,agent_key,trigger_message_id,parent_run_id,
                        run_type,stage,status,conversation_key,request_json,
                        executor_json,idempotency_key
                    ) VALUES (%s,%s,%s,%s,%s,'stage','implementation','queued',%s,%s,%s,%s)
                    ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL
                    DO UPDATE SET idempotency_key=EXCLUDED.idempotency_key RETURNING *
                    """,
                    (
                        str(uuid.uuid4()), locked["project_id"], locked["agent_key"],
                        handoff["message_id"], run_id,
                        conversation_key(
                            str(locked["project_id"]), str(locked["agent_key"]),
                            str(locked["channel"]), str(locked["chat_id"]),
                        ),
                        Jsonb(next_request),
                        Jsonb(stage_executor(next_request, "implementation")),
                        f"task-validation-rework:{root_id}:{next_count}:implementation",
                    ),
                )
            ).fetchone()
            result["next_run_id"] = str(next_run["run_id"])
            result["validation_rework_count"] = next_count
            await connection.execute(
                "UPDATE assistant.messages SET run_id=%s WHERE message_id=%s",
                (next_run["run_id"], handoff["message_id"]),
            )
            updated = await (
                await connection.execute(
                    """
                    UPDATE assistant.runs
                    SET status='succeeded',result_json=%s,artifact_refs=%s,error_json=NULL,
                        worker_id=NULL,worker_instance_id=NULL,claim_token=NULL,
                        lease_until=NULL,process_pid=NULL,process_start_ticks=NULL,
                        updated_at=now(),ended_at=now()
                    WHERE run_id=%s AND worker_instance_id=%s AND claim_token=%s
                      AND status='running' RETURNING run_id
                    """,
                    (
                        Jsonb(result), Jsonb(json_compatible(artifact_refs)),
                        run_id, worker_instance_id, claim_token,
                    ),
                )
            ).fetchone()
            if updated is None:
                raise RuntimeError("Implementation Run lease was lost during validation rework")
            await connection.commit()
            return {
                "outcome": "validation_rework",
                "status": "succeeded",
                "reply": None,
                "next_run": next_run,
            }

    async def finalize_agent_waiting(
        self, *, run_id: str, worker_instance_id: str, claim_token: str,
        session_id: str, native_session_id: str, input_tokens: int,
        context_window_tokens: int, archive_uri: str | None,
        archive_sha256: str | None, project_id: str, channel: str, chat_id: str,
        user_id: str | None, source_message_id: str, question: dict[str, Any],
        message_type: str = "question", artifact_refs: list[Any] | None = None,
        usage_snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist one interaction round and release the worker atomically.

        Every free-text question is its own outbound message. A Feishu user can
        therefore use the native Reply action and the external message ID resolves
        one exact Run/Session/question tuple, even when several questions are open.
        """
        if message_type not in {"question", "approval"}:
            raise ValueError("message_type must be question or approval")
        async with self.database.connection() as connection:
            run = await (
                await connection.execute(
                    """
                    SELECT interaction_seq FROM assistant.runs
                    WHERE run_id=%s AND worker_instance_id=%s AND claim_token=%s
                      AND status='running' AND cancel_requested_at IS NULL FOR UPDATE
                    """,
                    (run_id, worker_instance_id, claim_token),
                )
            ).fetchone()
            if run is None:
                raise RuntimeError("run lease was lost before waiting for user")
            session_update = await connection.execute(
                """
                UPDATE assistant.sessions
                SET native_session_id=%s,input_tokens=%s,context_window_tokens=%s,
                    archive_uri=COALESCE(%s,archive_uri),
                    archive_sha256=COALESCE(%s,archive_sha256),
                    updated_at=now()
                WHERE session_id=%s AND status='active' RETURNING session_id
                """,
                (
                    native_session_id, max(input_tokens, 0), context_window_tokens,
                    archive_uri, archive_sha256, session_id,
                ),
            )
            if await session_update.fetchone() is None:
                raise RuntimeError("active session was not found while waiting for user")
            sequence = int(run["interaction_seq"]) + 1
            stage = str(question.get("stage") or "PM")
            correlation = {
                "project_id": project_id,
                "session_id": session_id,
                "run_id": run_id,
                "interaction_seq": sequence,
                "chat_id": chat_id,
                "user_id": user_id,
            }
            payloads: list[dict[str, Any]] = []
            if message_type == "question":
                questions = [dict(item) for item in question.get("questions") or []]
                question_ids = [str(item.get("question_id") or "").strip() for item in questions]
                if not questions or any(not item for item in question_ids):
                    raise ValueError("a question interaction requires stable question IDs")
                if len(set(question_ids)) != len(question_ids):
                    raise ValueError("question_id must be unique inside one interaction")
                for ordinal, (item, question_id) in enumerate(
                    zip(questions, question_ids, strict=True), start=1
                ):
                    question_text = str(item.get("question_text") or "").strip()
                    if not question_text:
                        raise ValueError("every question requires its complete original text")
                    summary = str(question.get("summary") or "").strip()
                    options = [
                        dict(option) for option in item.get("options") or []
                        if isinstance(option, dict)
                    ]
                    option_text = "\n".join(
                        f"{index}. {str(option.get('label') or '')}"
                        + (f" — {option['description']}" if option.get("description") else "")
                        for index, option in enumerate(options, start=1)
                    )
                    label = "\n\n".join(
                        part for part in (
                            summary if ordinal == 1 else "",
                            question_text,
                            option_text,
                            "可点选卡片，也可回复本消息补充自己的答案。" if options else "",
                            (
                                "有多个待答问题，请回复对应消息。"
                                if len(questions) > 1 else ""
                            ),
                        ) if part
                    )
                    payloads.append({
                        "text": label,
                        "summary": question.get("summary", ""),
                        "question_id": question_id,
                        "question_text": question_text,
                        "options": options,
                        "required": bool(item.get("required", True)),
                        "input_schema": item.get("input_schema"),
                        "ordinal": ordinal,
                        "interaction_seq": sequence,
                        "correlation": {**correlation, "question_id": question_id},
                    })
            else:
                payload = dict(question)
                payload["correlation"] = correlation
                payload["interaction_seq"] = sequence
                payloads.append(payload)

            messages: list[dict[str, Any]] = []
            for ordinal, payload in enumerate(payloads, start=1):
                message_id = str(uuid.uuid4())
                identity = payload.get("question_id") or ordinal
                key = f"{message_type}:{run_id}:{sequence}:{canonical_sha256(identity)}"
                message = await (
                    await connection.execute(
                        """
                        INSERT INTO assistant.messages(
                            message_id,project_id,session_id,run_id,channel,chat_id,user_id,
                            direction,message_type,reply_to_message_id,content_json,
                            delivery_status,idempotency_key
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,'outbound',%s,%s,%s,'pending',%s)
                        RETURNING *
                        """,
                        (
                            message_id, project_id, session_id, run_id, channel, chat_id,
                            user_id, message_type, source_message_id,
                            Jsonb(json_compatible(payload)), key,
                        ),
                    )
                ).fetchone()
                messages.append(message)
            message = messages[0]
            question_ids = [
                str(payload["question_id"]) for payload in payloads
                if payload.get("question_id")
            ]
            required_question_ids = [
                str(payload["question_id"]) for payload in payloads
                if payload.get("question_id") and payload.get("required", True)
            ]
            result_payload = {
                "outcome": "needs_approval" if message_type == "approval" else "needs_input",
                "interaction_seq": sequence,
                "artifact_refs": json_compatible(artifact_refs or []),
                "question_ids": question_ids,
                "required_question_ids": required_question_ids,
                "question_message_ids": [str(item["message_id"]) for item in messages],
                "token_usage": json_compatible(usage_snapshot or {}),
            }
            updated = await (
                await connection.execute(
                    """
                    UPDATE assistant.runs
                    SET status='waiting_user',waiting_question_message_id=%s,
                        interaction_seq=%s,result_json=%s,artifact_refs=%s,
                        error_json=NULL,
                        worker_id=NULL,worker_instance_id=NULL,claim_token=NULL,
                        lease_until=NULL,process_pid=NULL,process_start_ticks=NULL,
                        updated_at=now()
                    WHERE run_id=%s AND worker_instance_id=%s AND claim_token=%s
                      AND status='running' RETURNING run_id
                    """,
                    (
                        message["message_id"], sequence, Jsonb(result_payload),
                        Jsonb(json_compatible(artifact_refs or [])), run_id,
                        worker_instance_id, claim_token,
                    ),
                )
            ).fetchone()
            if updated is None:
                raise RuntimeError("run lease was lost before waiting for user")
            await connection.commit()
            result = dict(message)
            result["question_message_ids"] = result_payload["question_message_ids"]
            return result

    async def finish_run(
        self, run_id: str, worker_instance_id: str, claim_token: str, *, status: str,
        result: dict[str, Any] | None = None, error: dict[str, Any] | None = None,
        artifact_refs: list[Any] | None = None,
    ) -> bool:
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                """
                UPDATE assistant.runs SET status=%s,result_json=%s,error_json=%s,
                    artifact_refs=COALESCE(%s,artifact_refs),worker_id=NULL,
                    worker_instance_id=NULL,claim_token=NULL,lease_until=NULL,
                    process_pid=NULL,process_start_ticks=NULL,updated_at=now(),
                    ended_at=CASE WHEN %s IN ('succeeded','failed','cancelled','dead_letter')
                                  THEN now() ELSE ended_at END
                WHERE run_id=%s AND worker_instance_id=%s AND claim_token=%s
                  AND status='running'
                RETURNING run_id,project_id,status,request_json
                """,
                (
                    status,
                    Jsonb(json_compatible(result)) if result is not None else None,
                    Jsonb(json_compatible(error)) if error is not None else None,
                    Jsonb(json_compatible(artifact_refs)) if artifact_refs is not None else None,
                    status, run_id, worker_instance_id, claim_token,
                ),
            )
            row = await cursor.fetchone()
            updated = row is not None
            if (
                row is not None
                and status in {"succeeded", "failed", "cancelled", "dead_letter"}
                and (row.get("request_json") or {}).get("task_protocol")
                        == "single-agent.v2"
            ):
                await self._advance_supervisor_context_epoch_locked(
                    connection, task=row, reason="task_terminal",
                    source_id=f"terminal:{run_id}:{status}",
                )
            await connection.commit()
            return updated

    async def finish_cancelled_run(
        self, run_id: str, worker_instance_id: str, claim_token: str,
    ) -> str:
        """A cancelled write-capable process needs reconciliation, not replay."""
        async with self.database.connection() as connection:
            row = await (await connection.execute(
                """
                UPDATE assistant.runs SET
                    status=CASE WHEN run_type IN ('python','publish')
                        OR COALESCE((executor_json->>'may_write_shared')::boolean,
                            COALESCE(executor_json->>'sandbox','')<>'read-only')
                        THEN 'reconciling' ELSE 'cancelled' END,
                    error_json=jsonb_build_object('type','Cancelled',
                        'message','process stopped; inspect possible file effects'),
                    worker_id=NULL,worker_instance_id=NULL,claim_token=NULL,
                    lease_until=NULL,process_pid=NULL,process_start_ticks=NULL,
                    updated_at=now(),
                    ended_at=CASE WHEN run_type IN ('python','publish')
                        OR COALESCE((executor_json->>'may_write_shared')::boolean,
                            COALESCE(executor_json->>'sandbox','')<>'read-only')
                        THEN NULL ELSE now() END
                WHERE run_id=%s AND worker_instance_id=%s AND claim_token=%s
                  AND status='running'
                RETURNING run_id,project_id,status,run_type,parent_run_id,request_json
                """,
                (run_id, worker_instance_id, claim_token),
            )).fetchone()
            if row is not None and row["status"] == "reconciling":
                await self._update_publish_parent(
                    connection, row, status="blocked",
                    phase="publication_reconciling",
                    publication_state="reconciling",
                )
            elif (
                row is not None
                and row["status"] == "cancelled"
                and (row.get("request_json") or {}).get("task_protocol")
                == "single-agent.v2"
            ):
                await self._advance_supervisor_context_epoch_locked(
                    connection, task=row, reason="task_terminal",
                    source_id=f"terminal:{run_id}:cancelled",
                )
            await connection.commit()
            return str(row["status"]) if row else "lease_lost"

    async def finalize_agent_blocked(
        self, *, run_id: str, worker_instance_id: str, claim_token: str,
        summary: str, artifact_refs: list[Any],
        permission_request: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist a business blocker without treating it as success or retryable failure."""
        async with self.database.connection() as connection:
            locked = await (
                await connection.execute(
                    """
                    SELECT r.*,m.channel,m.chat_id,m.user_id
                    FROM assistant.runs r
                    JOIN assistant.messages m ON m.message_id=r.trigger_message_id
                    WHERE r.run_id=%s AND r.worker_instance_id=%s AND r.claim_token=%s
                      AND r.status='running' AND r.cancel_requested_at IS NULL
                    FOR UPDATE OF r
                    """,
                    (run_id, worker_instance_id, claim_token),
                )
            ).fetchone()
            if locked is None:
                raise RuntimeError("Run lease was lost before recording blocker")
            reply = await (
                await connection.execute(
                    """
                    INSERT INTO assistant.messages(
                        message_id,project_id,run_id,channel,chat_id,user_id,direction,
                        message_type,reply_to_message_id,content_json,delivery_status,idempotency_key
                    ) VALUES (%s,%s,%s,%s,%s,%s,'outbound','reply',%s,%s,'pending',%s)
                    ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL
                    DO UPDATE SET idempotency_key=EXCLUDED.idempotency_key RETURNING *
                    """,
                    (
                        str(uuid.uuid4()), locked["project_id"], run_id,
                        locked["channel"], locked["chat_id"], locked["user_id"],
                        locked["trigger_message_id"],
                        Jsonb({
                            "text": summary, "outcome": "blocked",
                            "permission_request": json_compatible(permission_request),
                        }),
                        f"blocked-reply:{run_id}",
                    ),
                )
            ).fetchone()
            updated = await (
                await connection.execute(
                    """
                    UPDATE assistant.runs SET status='blocked',result_json=%s,artifact_refs=%s,
                        worker_id=NULL,worker_instance_id=NULL,claim_token=NULL,lease_until=NULL,
                        process_pid=NULL,process_start_ticks=NULL,error_json=NULL,
                        updated_at=now(),ended_at=NULL
                    WHERE run_id=%s AND worker_instance_id=%s AND claim_token=%s
                      AND status='running' RETURNING run_id
                    """,
                    (
                        Jsonb({
                            "outcome": "blocked", "summary": summary,
                            "artifact_refs": json_compatible(artifact_refs),
                            "permission_request": json_compatible(permission_request),
                        }),
                        Jsonb(json_compatible(artifact_refs)), run_id,
                        worker_instance_id, claim_token,
                    ),
                )
            ).fetchone()
            if updated is None:
                raise RuntimeError("Run lease was lost before recording blocker")
            await connection.commit()
            return {"run_id": run_id, "status": "blocked", "reply": reply}

    async def resume_blocked_stage_with_permission(
        self, *, blocked_run_id: str, permission_run_id: str,
        permission_receipt_uri: str, permission_receipt_sha256: str,
        approval_message_id: str | None,
    ) -> dict[str, Any]:
        key = f"permission-resume:{blocked_run_id}:{permission_receipt_sha256}"
        async with self.database.connection() as connection:
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (key,),
            )
            existing = await (
                await connection.execute(
                    "SELECT * FROM assistant.runs WHERE idempotency_key=%s", (key,)
                )
            ).fetchone()
            if existing is not None:
                await connection.commit()
                return {**existing, "idempotent": True}
            blocked = await (
                await connection.execute(
                    """
                    SELECT r.*,m.channel,m.chat_id,m.user_id
                    FROM assistant.runs r
                    JOIN assistant.messages m ON m.message_id=r.trigger_message_id
                    WHERE r.run_id=%s AND r.status='blocked' AND r.run_type='stage'
                    FOR UPDATE OF r
                    """,
                    (blocked_run_id,),
                )
            ).fetchone()
            if blocked is None:
                raise ValueError("permission resume target is not a blocked Stage Run")
            request = dict(blocked["request_json"] or {})
            request["permission_resume"] = {
                "blocked_run_id": blocked_run_id,
                "permission_run_id": permission_run_id,
                "permission_receipt_uri": permission_receipt_uri,
                "permission_receipt_sha256": permission_receipt_sha256,
                "approval_message_id": approval_message_id,
            }
            stage = str(blocked.get("stage") or "")
            if stage not in {"requirements", "implementation", "review"}:
                raise ValueError("permission resume target has an unsupported Stage")
            message_id = str(uuid.uuid4())
            new_run_id = str(uuid.uuid4())
            trigger = await (
                await connection.execute(
                    """
                    INSERT INTO assistant.messages(
                        message_id,project_id,channel,chat_id,user_id,direction,message_type,
                        content_json,delivery_status,idempotency_key
                    ) VALUES (%s,%s,%s,%s,%s,'system','task',%s,'received',%s)
                    RETURNING *
                    """,
                    (
                        message_id, blocked["project_id"], blocked["channel"],
                        blocked["chat_id"], blocked["user_id"],
                        Jsonb({
                            "schema_version": "permission-resume.v1",
                            "blocked_run_id": blocked_run_id,
                            "permission_run_id": permission_run_id,
                            "permission_receipt_uri": permission_receipt_uri,
                            "permission_receipt_sha256": permission_receipt_sha256,
                            "goal": str(request.get("goal") or ""),
                        }),
                        f"permission-resume-trigger:{blocked_run_id}:{permission_receipt_sha256}",
                    ),
                )
            ).fetchone()
            resumed = await (
                await connection.execute(
                    """
                    INSERT INTO assistant.runs(
                        run_id,project_id,agent_key,trigger_message_id,parent_run_id,
                        run_type,stage,status,conversation_key,request_json,executor_json,
                        idempotency_key
                    ) VALUES (%s,%s,%s,%s,%s,'stage',%s,'queued',%s,%s,%s,%s)
                    RETURNING *
                    """,
                    (
                        new_run_id, blocked["project_id"], blocked["agent_key"],
                        trigger["message_id"], blocked_run_id, stage,
                        blocked["conversation_key"], Jsonb(json_compatible(request)),
                        Jsonb(stage_executor(request, stage)), key,
                    ),
                )
            ).fetchone()
            await connection.execute(
                "UPDATE assistant.messages SET run_id=%s WHERE message_id=%s",
                (resumed["run_id"], trigger["message_id"]),
            )
            await connection.execute(
                """
                UPDATE assistant.runs
                SET status='cancelled',ended_at=now(),updated_at=now(),
                    result_json=COALESCE(result_json,'{}'::jsonb) || %s
                WHERE run_id=%s AND status='blocked'
                """,
                (
                    Jsonb({
                        "superseded_by_run_id": str(resumed["run_id"]),
                        "permission_receipt_uri": permission_receipt_uri,
                        "permission_receipt_sha256": permission_receipt_sha256,
                    }),
                    blocked_run_id,
                ),
            )
            await connection.commit()
            return {**resumed, "idempotent": False}

    async def unblock_run(self, run_id: str, note: str) -> str:
        if not note.strip():
            raise ValueError("unblock note is required")
        async with self.database.connection() as connection:
            locked = await (
                await connection.execute(
                    "SELECT request_json FROM assistant.runs WHERE run_id=%s AND status='blocked' FOR UPDATE",
                    (run_id,),
                )
            ).fetchone()
            if locked is None:
                raise ValueError("run is not blocked")
            request = dict(locked["request_json"] or {})
            history = list(request.get("unblock_history") or [])
            history.append({"note": note.strip(), "recorded_at": datetime.utcnow().isoformat() + "Z"})
            request["unblock_history"] = history
            request["unblock_note"] = note.strip()
            row = await (
                await connection.execute(
                    """
                    UPDATE assistant.runs SET status='queued',request_json=%s,result_json=NULL,
                        error_json=NULL,available_at=now(),updated_at=now(),ended_at=NULL
                    WHERE run_id=%s AND status='blocked' RETURNING status
                    """,
                    (Jsonb(json_compatible(request)), run_id),
                )
            ).fetchone()
            await connection.commit()
            return str(row["status"])

    async def finish_publish_run(
        self, run_id: str, worker_instance_id: str, claim_token: str, *,
        result: dict[str, Any], artifact_refs: list[Any],
    ) -> dict[str, Any]:
        """Commit a deterministic publication and its user-visible receipt atomically."""
        async with self.database.connection() as connection:
            locked = await (
                await connection.execute(
                    """
                    SELECT r.*,m.channel,m.chat_id,m.user_id
                    FROM assistant.runs r
                    JOIN assistant.messages m ON m.message_id=r.trigger_message_id
                    WHERE r.run_id=%s AND r.worker_instance_id=%s AND r.claim_token=%s
                      AND r.status='running' AND r.run_type='publish'
                    FOR UPDATE OF r
                    """,
                    (run_id, worker_instance_id, claim_token),
                )
            ).fetchone()
            if locked is None:
                raise RuntimeError("Publish Run lease was lost")
            if result.get("target_skill"):
                summary = (
                    f"Skill {result.get('target_skill')} 已发布；"
                    f"revision={result.get('published_revision')}。"
                )
            else:
                summary = f"正式候选文件已发布；files={len(result.get('files') or [])}。"
            reply = await (
                await connection.execute(
                    """
                    INSERT INTO assistant.messages(
                        message_id,project_id,run_id,channel,chat_id,user_id,direction,
                        message_type,reply_to_message_id,content_json,delivery_status,idempotency_key
                    ) VALUES (%s,%s,%s,%s,%s,%s,'outbound','reply',%s,%s,'pending',%s)
                    ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL
                    DO UPDATE SET idempotency_key=EXCLUDED.idempotency_key RETURNING *
                    """,
                    (
                        str(uuid.uuid4()), locked["project_id"], run_id,
                        locked["channel"], locked["chat_id"], locked["user_id"],
                        locked["trigger_message_id"], Jsonb({"text": summary, **result}),
                        f"publish-receipt:{run_id}",
                    ),
                )
            ).fetchone()
            updated = await (
                await connection.execute(
                    """
                    UPDATE assistant.runs SET status='succeeded',result_json=%s,artifact_refs=%s,
                        worker_id=NULL,worker_instance_id=NULL,claim_token=NULL,lease_until=NULL,
                        process_pid=NULL,process_start_ticks=NULL,error_json=NULL,
                        updated_at=now(),ended_at=now()
                    WHERE run_id=%s AND worker_instance_id=%s AND claim_token=%s
                      AND status='running' RETURNING run_id
                    """,
                    (
                        Jsonb(json_compatible(result)), Jsonb(json_compatible(artifact_refs)),
                        run_id, worker_instance_id, claim_token,
                    ),
                )
            ).fetchone()
            if updated is None:
                raise RuntimeError("Publish Run lease was lost before completion")
            if (locked["request_json"] or {}).get("task_protocol") == "single-agent.v2":
                parent = await (
                    await connection.execute(
                        """
                        UPDATE assistant.runs SET status='succeeded',
                            request_json=request_json || %s,
                            result_json=COALESCE(result_json,'{}'::jsonb) || %s,
                            ended_at=now(),updated_at=now()
                        WHERE run_id=%s AND status IN ('waiting_user','blocked')
                          AND request_json->>'publish_run_id'=%s
                        RETURNING run_id
                        """,
                        (
                            Jsonb({"task_phase": "published"}),
                            Jsonb({
                                "publication_state": "published",
                                "publish_run_id": run_id,
                                "publication_receipt": result,
                            }),
                            locked["parent_run_id"], run_id,
                        ),
                    )
                ).fetchone()
                if parent is None:
                    raise RuntimeError("approved Task is not awaiting this Publisher Run")
            await connection.commit()
            return {"run_id": run_id, "reply_message_id": str(reply["message_id"])}

    @staticmethod
    async def _update_publish_parent(
        connection: Any, publish: dict[str, Any], *,
        status: str, phase: str, publication_state: str,
    ) -> None:
        if (
            publish.get("run_type") != "publish"
            or (publish.get("request_json") or {}).get("task_protocol") != "single-agent.v2"
        ):
            return
        await connection.execute(
            """
            UPDATE assistant.runs SET status=%s,
                request_json=request_json || %s,
                result_json=COALESCE(result_json,'{}'::jsonb) || %s,
                ended_at=CASE WHEN %s IN ('failed','cancelled') THEN now() ELSE NULL END,
                updated_at=now()
            WHERE run_id=%s AND status IN ('waiting_user','blocked')
              AND request_json->>'publish_run_id'=%s
            """,
            (
                status, Jsonb({"task_phase": phase}),
                Jsonb({
                    "publication_state": publication_state,
                    "publish_run_id": str(publish["run_id"]),
                }),
                status, publish["parent_run_id"], str(publish["run_id"]),
            ),
        )

    async def retry_run(
        self, run_id: str, worker_instance_id: str, claim_token: str,
        error: dict[str, Any]
    ) -> str:
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                """
                UPDATE assistant.runs
                SET status=CASE
                      WHEN cancel_requested_at IS NOT NULL AND (
                        run_type IN ('python','publish') OR (process_pid IS NOT NULL AND
                        COALESCE((executor_json->>'may_write_shared')::boolean,
                            COALESCE(executor_json->>'sandbox','')<>'read-only'))
                      ) THEN 'reconciling'
                      WHEN cancel_requested_at IS NOT NULL THEN 'cancelled'
                      WHEN run_type='publish' OR (process_pid IS NOT NULL AND (
                        run_type='python' OR COALESCE((executor_json->>'may_write_shared')::boolean,
                            COALESCE(executor_json->>'sandbox','')<>'read-only')
                      )) THEN 'reconciling'
                      WHEN attempt>=max_attempts THEN 'dead_letter'
                      ELSE 'queued' END,
                    available_at=CASE
                      WHEN cancel_requested_at IS NOT NULL THEN available_at
                      WHEN run_type='publish' OR (process_pid IS NOT NULL AND (
                        run_type='python' OR COALESCE((executor_json->>'may_write_shared')::boolean,
                            COALESCE(executor_json->>'sandbox','')<>'read-only')
                      )) THEN available_at
                      WHEN attempt>=max_attempts THEN available_at
                      ELSE now()+(LEAST(attempt,6)*interval '5 seconds') END,
                    error_json=%s,worker_id=NULL,worker_instance_id=NULL,claim_token=NULL,
                    lease_until=NULL,
                    process_pid=CASE WHEN cancel_requested_at IS NULL AND process_pid IS NOT NULL AND (
                        run_type='python' OR COALESCE((executor_json->>'may_write_shared')::boolean,
                            COALESCE(executor_json->>'sandbox','')<>'read-only')
                      ) THEN process_pid ELSE NULL END,
                    process_start_ticks=CASE WHEN cancel_requested_at IS NULL AND process_pid IS NOT NULL AND (
                        run_type='python' OR COALESCE((executor_json->>'may_write_shared')::boolean,
                            COALESCE(executor_json->>'sandbox','')<>'read-only')
                      ) THEN process_start_ticks ELSE NULL END,
                    ended_at=CASE WHEN cancel_requested_at IS NOT NULL THEN now()
                      WHEN attempt>=max_attempts AND NOT (
                        run_type='publish' OR (process_pid IS NOT NULL AND (
                          run_type='python' OR COALESCE((executor_json->>'may_write_shared')::boolean,
                              COALESCE(executor_json->>'sandbox','')<>'read-only')
                        ))
                      ) THEN now() ELSE NULL END,
                    updated_at=now()
                WHERE run_id=%s AND worker_instance_id=%s AND claim_token=%s
                  AND status='running'
                RETURNING run_id,status,run_type,parent_run_id,request_json
                """,
                (Jsonb(json_compatible(error)), run_id, worker_instance_id, claim_token),
            )
            row = await cursor.fetchone()
            if row is not None and row["status"] == "reconciling":
                await self._update_publish_parent(
                    connection, row, status="blocked",
                    phase="publication_reconciling",
                    publication_state="reconciling",
                )
            await connection.commit()
            return str(row["status"]) if row else "lease_lost"

    async def reconcile_run(self, run_id: str, action: str, note: str) -> str:
        if action not in {"retry", "fail", "cancel"}:
            raise ValueError("action must be retry, fail, or cancel")
        target = {"retry": "queued", "fail": "failed", "cancel": "cancelled"}[action]
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                """
                UPDATE assistant.runs
                SET status=%s,available_at=CASE WHEN %s='queued' THEN now() ELSE available_at END,
                    ended_at=CASE WHEN %s IN ('failed','cancelled') THEN now() ELSE NULL END,
                    cancel_requested_at=CASE WHEN %s='queued' THEN NULL ELSE cancel_requested_at END,
                    error_json=COALESCE(error_json,'{}'::jsonb) || %s,
                    process_pid=NULL,process_start_ticks=NULL,updated_at=now()
                WHERE run_id=%s AND status='reconciling'
                RETURNING run_id,status,run_type,parent_run_id,request_json
                """,
                (target, target, target, target, Jsonb({"reconciliation_note": note}), run_id),
            )
            row = await cursor.fetchone()
            if row is not None:
                phase = {
                    "retry": "publishing", "fail": "publication_failed",
                    "cancel": "publication_cancelled",
                }[action]
                parent_status = {
                    "retry": "waiting_user", "fail": "failed", "cancel": "cancelled",
                }[action]
                await self._update_publish_parent(
                    connection, row, status=parent_status,
                    phase=phase, publication_state=target,
                )
            await connection.commit()
            if row is None:
                raise ValueError("run is not waiting for reconciliation")
            return str(row["status"])

    async def list_reconciling_runs_due(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return Discord reconciliation Runs that need an initial card or one reminder."""
        async with self.database.connection() as connection:
            rows = list(await (await connection.execute(
                """
                SELECT r.*,m.channel,m.chat_id,m.user_id,m.message_id AS source_message_id,
                       m.content_json AS source_content
                FROM assistant.runs r JOIN assistant.messages m
                  ON m.message_id=r.trigger_message_id
                WHERE r.status='reconciling' AND m.channel='discord'
                  AND COALESCE(r.executor_json#>>'{reconciliation,card_message_id}','')=''
                  AND (
                    COALESCE(r.executor_json#>>'{reconciliation,resolved_action}','')=''
                    OR (
                      r.executor_json#>>'{reconciliation,resolved_action}'='wait'
                      AND COALESCE(r.executor_json#>>'{reconciliation,next_reminder_at}','')<>''
                      AND (r.executor_json#>>'{reconciliation,next_reminder_at}')::timestamptz<=now()
                    )
                  )
                ORDER BY r.updated_at,r.run_id LIMIT %s
                """,
                (min(max(limit, 1), 100),),
            )).fetchall())
        return rows

    async def list_reconciling_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        """All reconciling Runs not yet resolved by a card decision."""
        async with self.database.connection() as connection:
            rows = list(await (await connection.execute(
                """
                SELECT * FROM assistant.runs
                WHERE status='reconciling'
                  AND COALESCE(executor_json#>>'{reconciliation,resolved_action}','')=''
                ORDER BY updated_at LIMIT %s
                """,
                (min(max(limit, 1), 100),),
            )).fetchall())
        return rows

    async def auto_requeue_reconciled(
        self, *, run_id: str, reason: str,
    ) -> str:
        """v6.1 tiered recovery: requeue a reconciling Run with bounded backoff.

        Mirrors retry_run's queued/dead_letter branches but operates on the
        reconciling state (the lease is already cleared). The caller must
        only send Runs whose side effects are provably absent; publish and
        python Runs always stay on the manual card path.
        """
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                """
                UPDATE assistant.runs
                SET status=CASE WHEN attempt>=max_attempts THEN 'dead_letter'
                                ELSE 'queued' END,
                    available_at=CASE
                      WHEN attempt>=max_attempts THEN available_at
                      ELSE now()+(LEAST(attempt,6)*interval '5 seconds') END,
                    ended_at=CASE WHEN attempt>=max_attempts THEN now()
                                  ELSE ended_at END,
                    error_json=COALESCE(error_json,'{}'::jsonb) || %s,
                    updated_at=now()
                WHERE run_id=%s AND status='reconciling'
                RETURNING run_id,status
                """,
                (
                    Jsonb(json_compatible({
                        "type": "AutoRequeued", "message": reason[:1000],
                    })),
                    run_id,
                ),
            )
            row = await cursor.fetchone()
            await connection.commit()
            return str(row["status"]) if row else "not_reconciling"

    async def reconciliation_health(self) -> dict[str, Any]:
        async with self.database.connection() as connection:
            row = await (await connection.execute(
                """
                SELECT count(*) AS reconciling_count,
                       COALESCE(EXTRACT(EPOCH FROM (now()-min(updated_at))),0)::bigint
                         AS oldest_reconciling_seconds,
                       count(*) FILTER (WHERE
                         COALESCE(executor_json#>>'{reconciliation,card_message_id}','')=''
                       ) AS reconciliation_cards_pending
                FROM assistant.runs WHERE status='reconciling'
                """
            )).fetchone()
        return {
            "reconciling_count": int(row.get("reconciling_count") or 0),
            "oldest_reconciling_seconds": int(row.get("oldest_reconciling_seconds") or 0),
            "reconciliation_cards_pending": int(row.get("reconciliation_cards_pending") or 0),
        }

    async def list_messages_since(
        self, project_id: str, channels: list[str], *,
        since: str | None, limit: int = 30, chat_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Semantic messages after a watermark timestamp for Supervisor deltas."""
        bounded_limit = min(max(limit, 1), 60)
        clauses = [
            "m.project_id=%s", "m.channel=ANY(%s)",
            "m.direction='inbound'", "m.message_type IN ('text','answer')",
        ]
        parameters: list[Any] = [project_id, list(channels)]
        if chat_id:
            clauses.append("m.chat_id=%s")
            parameters.append(str(chat_id))
        if since:
            clauses.append("m.created_at > %s::timestamptz")
            parameters.append(since)
        parameters.append(bounded_limit)
        async with self.database.connection() as connection:
            rows = list(await (await connection.execute(
                f"""
                SELECT m.message_id,m.run_id,m.channel,m.chat_id,m.direction,
                       m.message_type,m.content_json,m.created_at,r.agent_key
                FROM assistant.messages m
                LEFT JOIN assistant.runs r ON r.run_id=m.run_id
                WHERE {' AND '.join(clauses)}
                ORDER BY m.created_at LIMIT %s
                """,
                tuple(parameters),
            )).fetchall())
        return [
            {
                "message_id": str(row["message_id"]),
                "run_id": str(row["run_id"]) if row.get("run_id") else None,
                "agent_key": str(row["agent_key"]) if row.get("agent_key") else None,
                "interface_role": (
                    "supervisor" if str(row.get("agent_key") or "").endswith(":supervisor")
                    else "operator" if str(row.get("agent_key") or "").endswith(":operator")
                    else "pm" if str(row.get("agent_key") or "").endswith(":manager")
                    else None
                ),
                "channel": str(row["channel"]),
                "chat_id": str(row["chat_id"]),
                "direction": str(row["direction"]),
                "message_type": str(row["message_type"]),
                "text": str((row["content_json"] or {}).get("text") or "")[:400],
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]

    async def project_usage_rollup(self, project_id: str) -> dict[str, Any]:
        """Token/call counts per actor role for the last 24 hours (no prices)."""
        async with self.database.connection() as connection:
            rows = list(await (await connection.execute(
                """
                SELECT actor_role, count(*) AS calls,
                       COALESCE(sum(input_tokens),0)::bigint AS input_tokens,
                       COALESCE(sum(output_tokens),0)::bigint AS output_tokens
                FROM assistant.model_invocations
                WHERE project_id=%s AND started_at > now() - interval '24 hours'
                GROUP BY actor_role ORDER BY actor_role
                """,
                (project_id,),
            )).fetchall())
        return {
            "window": "24h",
            "by_actor_role": [
                {
                    "actor_role": str(row["actor_role"]),
                    "calls": int(row["calls"]),
                    "input_tokens": int(row["input_tokens"]),
                    "output_tokens": int(row["output_tokens"]),
                }
                for row in rows
            ],
        }

    async def _supersede_open_supervisor_cards_locked(
        self, connection: Any, *, task_id: str, reason: str,
        context_epoch: int,
    ) -> int:
        cards = list(await (await connection.execute(
            """
            SELECT message_id,project_id,run_id,channel,chat_id,user_id,
                   external_message_id,delivery_status,content_json
            FROM assistant.messages
            WHERE run_id=%s AND direction='outbound'
              AND content_json->>'interaction_kind'='supervisor_status'
              AND content_json->>'action_policy'='supervisor-card-actions.v2'
              AND content_json->>'action_status'='open'
            ORDER BY created_at,message_id
            FOR UPDATE
            """,
            (task_id,),
        )).fetchall())
        for card in cards:
            updated = _superseded_supervisor_card_content(
                dict(card.get("content_json") or {}),
                reason=reason, context_epoch=context_epoch,
            )
            changed = await (await connection.execute(
                """
                UPDATE assistant.messages SET content_json=%s,updated_at=now()
                WHERE message_id=%s
                  AND content_json->>'action_status'='open'
                RETURNING message_id
                """,
                (Jsonb(updated), card["message_id"]),
            )).fetchone()
            if (
                changed is not None
                and card.get("delivery_status") == "sent"
                and card.get("external_message_id")
            ):
                projection = {
                    **updated,
                    "context_visibility": "ui_only",
                    "ui_update_only": True,
                    "update_external_message_id": str(card["external_message_id"]),
                }
                await connection.execute(
                    """
                    INSERT INTO assistant.messages(
                        message_id,project_id,run_id,channel,chat_id,user_id,
                        direction,message_type,content_json,delivery_status,
                        idempotency_key
                    ) VALUES (%s,%s,%s,%s,%s,%s,'outbound','kernel_notice',
                              %s,'pending',%s)
                    ON CONFLICT (idempotency_key)
                        WHERE idempotency_key IS NOT NULL DO NOTHING
                    """,
                    (
                        str(uuid.uuid4()), card["project_id"], card.get("run_id"),
                        card["channel"], card["chat_id"], card.get("user_id"),
                        Jsonb(json_compatible(projection)),
                        (
                            f"supervisor-card-disable:{card['message_id']}:"
                            f"{int(context_epoch)}:{canonical_sha256(str(reason))[:12]}"
                        ),
                    ),
                )
        return len(cards)

    async def _advance_supervisor_context_epoch_locked(
        self, connection: Any, *, task: dict[str, Any], reason: str,
        source_id: str | None,
    ) -> int:
        """Advance one locked Task head, exactly once for a durable source id."""
        task_id = str(task["run_id"])
        request = dict(task.get("request_json") or {})
        current_epoch = int(request.get("supervisor_context_epoch") or 0)
        event_key = None
        if source_id:
            event_key = (
                f"supervisor-context-event:{task_id}:"
                f"{canonical_sha256(str(source_id))}"
            )
            existing = await (await connection.execute(
                """
                SELECT content_json FROM assistant.messages
                WHERE idempotency_key=%s
                """,
                (event_key,),
            )).fetchone()
            if existing is not None:
                body = dict(existing.get("content_json") or {})
                if (
                    str(body.get("task_id") or "") != task_id
                    or str(body.get("source_id") or "") != str(source_id)
                    or str(body.get("reason") or "") != str(reason)
                ):
                    raise ValueError("Supervisor context source id was reused")
                return int(body.get("context_epoch") or 0)

        next_epoch = current_epoch + 1
        if event_key:
            inserted = await (await connection.execute(
                """
                INSERT INTO assistant.messages(
                    message_id,project_id,run_id,channel,chat_id,direction,
                    message_type,content_json,delivery_status,idempotency_key
                ) VALUES (%s,%s,%s,'kernel',%s,'system','action',%s,'received',%s)
                ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL
                DO NOTHING RETURNING message_id
                """,
                (
                    str(uuid.uuid4()), task["project_id"], task_id, task_id,
                    Jsonb({
                        "schema_version": "supervisor-context-event.v1",
                        "context_visibility": "audit_only",
                        "task_id": task_id,
                        "source_id": str(source_id),
                        "reason": str(reason),
                        "context_epoch": next_epoch,
                    }),
                    event_key,
                ),
            )).fetchone()
            if inserted is None:
                # A competing transaction can only reach this branch after its
                # Task lock commits. Read and validate the authoritative event.
                existing = await (await connection.execute(
                    "SELECT content_json FROM assistant.messages WHERE idempotency_key=%s",
                    (event_key,),
                )).fetchone()
                if existing is None:
                    raise RuntimeError("Supervisor context event disappeared")
                body = dict(existing.get("content_json") or {})
                if (
                    str(body.get("task_id") or "") != task_id
                    or str(body.get("source_id") or "") != str(source_id)
                    or str(body.get("reason") or "") != str(reason)
                ):
                    raise ValueError("Supervisor context source id was reused")
                return int(body.get("context_epoch") or 0)

        request["supervisor_context_epoch"] = next_epoch
        await connection.execute(
            """
            UPDATE assistant.runs
            SET request_json=%s,updated_at=now()
            WHERE run_id=%s
            """,
            (Jsonb(json_compatible(request)), task_id),
        )
        await self._supersede_open_supervisor_cards_locked(
            connection, task_id=task_id, reason=reason,
            context_epoch=next_epoch,
        )
        return next_epoch

    async def advance_supervisor_context_epoch(
        self, task_id: str, *, reason: str, source_id: str | None = None,
    ) -> int:
        """Advance a Task's Supervisor head and close all prior open cards.

        A caller-supplied source id is recorded as an authoritative system
        event, making retries exactly idempotent without an unbounded id list
        in the Task JSON.
        """
        if not str(reason).strip():
            raise ValueError("Supervisor context reason is required")
        async with self.database.connection() as connection:
            task = await (await connection.execute(
                "SELECT * FROM assistant.runs WHERE run_id=%s FOR UPDATE",
                (task_id,),
            )).fetchone()
            if task is None:
                raise ValueError("Supervisor context Task is unavailable")
            epoch = await self._advance_supervisor_context_epoch_locked(
                connection, task=task, reason=str(reason), source_id=source_id,
            )
            await connection.commit()
            return epoch

    async def supervisor_context_is_current(
        self, task_id: str, source_context_epoch: int,
    ) -> bool:
        async with self.database.connection() as connection:
            row = await (await connection.execute(
                """
                SELECT 1 FROM assistant.runs
                WHERE run_id=%s
                  AND COALESCE((request_json->>'supervisor_context_epoch')::bigint,0)=%s
                """,
                (task_id, int(source_context_epoch)),
            )).fetchone()
        return row is not None

    async def queue_reconciliation_card(
        self, *, run_id: str, diagnostic: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Atomically queue one current reconciliation card through the normal Outbox."""
        async with self.database.connection() as connection:
            row = await (await connection.execute(
                """
                SELECT r.*,m.channel,m.chat_id,m.user_id,m.message_id AS source_message_id
                FROM assistant.runs r JOIN assistant.messages m
                  ON m.message_id=r.trigger_message_id
                WHERE r.run_id=%s AND r.status='reconciling' AND m.channel='discord'
                  AND COALESCE(r.executor_json#>>'{reconciliation,card_message_id}','')=''
                  AND (
                    COALESCE(r.executor_json#>>'{reconciliation,resolved_action}','')=''
                    OR (
                      r.executor_json#>>'{reconciliation,resolved_action}'='wait'
                      AND COALESCE(r.executor_json#>>'{reconciliation,next_reminder_at}','')<>''
                      AND (r.executor_json#>>'{reconciliation,next_reminder_at}')::timestamptz<=now()
                    )
                  )
                FOR UPDATE OF r
                """,
                (run_id,),
            )).fetchone()
            if row is None:
                await connection.commit()
                return None
            executor = dict(row.get("executor_json") or {})
            prior = dict(executor.get("reconciliation") or {})
            generation = int(prior.get("generation") or 0) + 1
            reminder_count = int(prior.get("reminder_count") or 0)
            if prior.get("card_message_id") and not prior.get("resolved_at"):
                reminder_count += 1
            elif prior.get("resolved_action") == "wait":
                reminder_count = 0
            parent = await (await connection.execute(
                """
                SELECT message_id FROM assistant.messages
                WHERE project_id=%s AND run_id=%s AND channel=%s AND chat_id=%s
                  AND direction='outbound' AND message_type IN ('reply','reconciliation')
                ORDER BY created_at DESC,message_id DESC LIMIT 1
                """,
                (row["project_id"],run_id,row["channel"],row["chat_id"]),
            )).fetchone()
            reply_target = str(parent["message_id"]) if parent else str(row["source_message_id"])
            content = {
                **json_compatible(diagnostic),
                "origin": "kernel",
                "context_visibility": "ui_only",
                "interaction_kind": "reconcile_run",
                "reconciliation_run_id": run_id,
                "reconciliation_generation": generation,
            }
            message_id = str(uuid.uuid4())
            message = await (await connection.execute(
                """
                INSERT INTO assistant.messages(
                    message_id,project_id,run_id,channel,chat_id,user_id,direction,
                    message_type,reply_to_message_id,content_json,delivery_status,idempotency_key
                ) VALUES (%s,%s,%s,%s,%s,%s,'outbound','reconciliation',%s,%s,'pending',%s)
                ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL
                DO UPDATE SET idempotency_key=EXCLUDED.idempotency_key RETURNING *
                """,
                (
                    message_id,row["project_id"],run_id,row["channel"],row["chat_id"],
                    row["user_id"],reply_target,Jsonb(content),
                    f"reconciliation-card:{run_id}:{generation}",
                ),
            )).fetchone()
            reconciliation = {
                **prior,
                **{key: value for key, value in diagnostic.items() if key != "text"},
                "generation": generation,
                "card_message_id": str(message["message_id"]),
                "notified_at": datetime.utcnow().isoformat() + "Z",
                "next_reminder_at": (
                    datetime.utcnow() + timedelta(minutes=10)
                ).isoformat() + "Z",
                "reminder_count": reminder_count,
                "resolved_at": None,
                "resolved_by": None,
                "resolved_action": None,
            }
            executor["reconciliation"] = reconciliation
            await connection.execute(
                "UPDATE assistant.runs SET executor_json=%s,updated_at=now() WHERE run_id=%s",
                (Jsonb(json_compatible(executor)), run_id),
            )
            await connection.commit()
            return message

    async def resolve_reconciliation_card(
        self, *, card_message_id: str, external_event_id: str,
        actor_user_id: str, action: str, recovered_summary: str | None = None,
    ) -> dict[str, Any]:
        """Apply one idempotent Discord reconciliation decision without an LLM Run."""
        if action not in {"recover", "retry", "resume", "fail", "cancel", "wait"}:
            raise ValueError("unsupported reconciliation action")
        async with self.database.connection() as connection:
            duplicate = await (await connection.execute(
                "SELECT run_id,content_json FROM assistant.messages "
                "WHERE channel='discord' AND external_event_id=%s",
                (external_event_id,),
            )).fetchone()
            if duplicate is not None:
                current = await (await connection.execute(
                    "SELECT status FROM assistant.runs WHERE run_id=%s",
                    (duplicate["run_id"],),
                )).fetchone()
                await connection.commit()
                return {
                    "run_id": str(duplicate["run_id"]),
                    "status": str(current["status"]) if current else "unknown",
                    "action": str((duplicate.get("content_json") or {}).get("action") or action),
                    "duplicate": True,
                }
            card = await (await connection.execute(
                """
                SELECT m.*,r.status AS run_status,r.run_type,r.executor_json,r.error_json,
                       r.parent_run_id,r.request_json AS run_request_json,
                       r.project_id AS run_project_id
                FROM assistant.messages m JOIN assistant.runs r ON r.run_id=m.run_id
                WHERE m.message_id=%s AND m.channel='discord'
                  AND m.message_type='reconciliation' FOR UPDATE OF r
                """,
                (card_message_id,),
            )).fetchone()
            if card is None:
                raise PermissionError("reconciliation card is unknown")
            content = dict(card.get("content_json") or {})
            executor = dict(card.get("executor_json") or {})
            reconciliation = dict(executor.get("reconciliation") or {})
            generation = int(content.get("reconciliation_generation") or 0)
            if (
                card["run_status"] != "reconciling"
                or str(reconciliation.get("card_message_id") or "") != card_message_id
                or int(reconciliation.get("generation") or 0) != generation
                or reconciliation.get("resolved_at")
            ):
                raise PermissionError("reconciliation card is stale")
            allowed = {
                str(item.get("id") or "") for item in content.get("actions") or []
                if isinstance(item, dict)
            }
            if action not in allowed:
                raise PermissionError("reconciliation action is not allowed by this card")
            incoming_id = str(uuid.uuid4())
            incoming = await (await connection.execute(
                """
                INSERT INTO assistant.messages(
                    message_id,project_id,run_id,channel,chat_id,user_id,direction,
                    message_type,external_event_id,reply_to_message_id,content_json,
                    delivery_status,idempotency_key
                ) VALUES (%s,%s,%s,'discord',%s,%s,'inbound','action',%s,%s,%s,'received',%s)
                ON CONFLICT (channel,external_event_id)
                    WHERE external_event_id IS NOT NULL DO NOTHING RETURNING *
                """,
                (
                    incoming_id,card["project_id"],card["run_id"],card["chat_id"],
                    actor_user_id,external_event_id,card_message_id,
                    Jsonb({
                        "schema_version": "reconciliation-action.v1",
                        "action": action,"generation": generation,
                    }),f"reconciliation-action:{card['run_id']}:{generation}:{action}",
                ),
            )).fetchone()
            if incoming is None:
                existing = await (await connection.execute(
                    "SELECT * FROM assistant.messages WHERE channel='discord' AND external_event_id=%s",
                    (external_event_id,),
                )).fetchone()
                await connection.commit()
                return {
                    "run_id": str(card["run_id"]),
                    "status": str(card["run_status"]),
                    "action": str((existing.get("content_json") or {}).get("action") or action),
                    "duplicate": True,
                }
            now = datetime.utcnow().isoformat() + "Z"
            reconciliation.update({
                "resolved_at": now,"resolved_by": actor_user_id,
                "resolved_action": action,"next_reminder_at": None,
            })
            executor["reconciliation"] = reconciliation
            if action == "wait":
                reconciliation.update({
                    "card_message_id": None,"next_reminder_at": (
                        datetime.utcnow() + timedelta(minutes=10)
                    ).isoformat() + "Z","reminder_count": 0,
                })
                target_status = "reconciling"
            elif action == "recover":
                if card["run_type"] != "chat" or not recovered_summary:
                    raise ValueError("this Run has no recoverable completed chat result")
                target_status = "succeeded"
            else:
                target_status = {
                    "retry": "queued", "resume": "queued",
                    "fail": "failed", "cancel": "cancelled",
                }[action]
            result_json = None
            if action == "recover":
                result_json = Jsonb({
                    "outcome": "completed","summary": recovered_summary,
                    "recovered_by_reconciliation": True,
                })
            await connection.execute(
                """
                UPDATE assistant.runs
                SET status=%s,executor_json=%s,
                    result_json=CASE WHEN %s::jsonb IS NULL THEN result_json ELSE %s::jsonb END,
                    available_at=CASE WHEN %s='queued' THEN now() ELSE available_at END,
                    ended_at=CASE WHEN %s IN ('succeeded','failed','cancelled') THEN now() ELSE NULL END,
                    cancel_requested_at=CASE WHEN %s='queued' THEN NULL ELSE cancel_requested_at END,
                    worker_id=NULL,worker_instance_id=NULL,claim_token=NULL,lease_until=NULL,
                    process_pid=NULL,process_start_ticks=NULL,
                    error_json=COALESCE(error_json,'{}'::jsonb) || %s,updated_at=now()
                WHERE run_id=%s AND status='reconciling'
                """,
                (
                    target_status,Jsonb(json_compatible(executor)),result_json,result_json,
                    target_status,target_status,target_status,
                    Jsonb({"reconciliation_note": f"Discord user selected {action}"}),
                    card["run_id"],
                ),
            )
            if action in {"retry", "resume", "fail", "cancel"}:
                parent_status = {
                    "retry": "waiting_user", "resume": "waiting_user",
                    "fail": "failed", "cancel": "cancelled",
                }[action]
                phase = {
                    "retry": "publishing", "resume": "publishing",
                    "fail": "publication_failed", "cancel": "publication_cancelled",
                }[action]
                await self._update_publish_parent(
                    connection,
                    {
                        "run_type": card["run_type"],
                        "request_json": card["run_request_json"],
                        "parent_run_id": card["parent_run_id"],
                        "run_id": card["run_id"],
                    },
                    status=parent_status, phase=phase,
                    publication_state=target_status,
                )
            public_text = {
                "recover": "已恢复已完成结果。",
                "retry": "已安排重新执行。",
                "resume": "已安排从现有候选继续完成。",
                "fail": "已标记任务失败。",
                "cancel": "已取消任务。",
                "wait": "将继续等待，并在稍后再次提醒。",
            }[action]
            outbound_id = str(uuid.uuid4())
            outbound = await (await connection.execute(
                """
                INSERT INTO assistant.messages(
                    message_id,project_id,run_id,channel,chat_id,user_id,direction,
                    message_type,reply_to_message_id,content_json,delivery_status,idempotency_key
                ) VALUES (%s,%s,%s,'discord',%s,%s,'outbound','reply',%s,%s,'pending',%s)
                ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL
                DO UPDATE SET idempotency_key=EXCLUDED.idempotency_key RETURNING *
                """,
                (
                    outbound_id,card["project_id"],card["run_id"],card["chat_id"],
                    card["user_id"],card_message_id,
                    Jsonb({
                        "text": f"{public_text}\nRun ID：{card['run_id']}",
                        "origin": "kernel",
                        "context_visibility": "ui_only",
                    }),
                    f"reconciliation-receipt:{card['run_id']}:{generation}:{action}",
                ),
            )).fetchone()
            await connection.commit()
            return {
                "run_id": str(card["run_id"]),"status": target_status,
                "action": action,"duplicate": False,
                "reply_message_id": str(outbound["message_id"]),
            }

    async def request_cancel(self, run_id: str) -> bool:
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                """
                UPDATE assistant.runs SET cancel_requested_at=COALESCE(cancel_requested_at,now()),
                    status=CASE WHEN status IN ('queued','waiting_user','waiting_supervisor','waiting_children') THEN 'cancelled' ELSE status END,
                    ended_at=CASE WHEN status IN ('queued','waiting_user','waiting_supervisor','waiting_children') THEN now() ELSE ended_at END,
                    updated_at=now()
                WHERE run_id=%s AND status IN ('queued','running','waiting_user','waiting_supervisor','waiting_children')
                  AND NOT (
                    COALESCE(request_json->>'task_protocol','')='single-agent.v2'
                    AND COALESCE(request_json->>'task_phase','') IN
                        ('publishing','publication_reconciling')
                  )
                RETURNING run_id
                """,
                (run_id,),
            )
            updated = await cursor.fetchone() is not None
            await connection.commit()
            return updated

    async def current_session(
        self, project_id: str, agent_key: str, channel: str, chat_id: str
    ) -> dict[str, Any] | None:
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT * FROM assistant.sessions
                WHERE project_id=%s AND agent_key=%s AND channel=%s AND chat_id=%s
                  AND status IN ('active','rotation_required')
                ORDER BY updated_at DESC LIMIT 1
                """,
                (project_id, agent_key, channel, chat_id),
            )
            return await cursor.fetchone()

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "SELECT * FROM assistant.sessions WHERE session_id=%s", (session_id,)
            )
            return await cursor.fetchone()

    async def list_runs(self, project_id: str, limit: int = 100) -> list[dict[str, Any]]:
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                """SELECT * FROM assistant.runs WHERE project_id=%s
                ORDER BY created_at DESC LIMIT %s""",
                (project_id, min(max(limit, 1), 500)),
            )
            return list(await cursor.fetchall())

    async def ensure_session(
        self, project_id: str, agent_key: str, channel: str, chat_id: str,
        *, rotation_checkpoint: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        session_id = str(uuid.uuid4())
        async with self.database.connection() as connection:
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (f"session:{project_id}:{agent_key}:{channel}:{chat_id}",),
            )
            cursor = await connection.execute(
                """
                SELECT * FROM assistant.sessions
                WHERE project_id=%s AND agent_key=%s AND channel=%s AND chat_id=%s
                  AND status IN ('active','rotation_required')
                ORDER BY updated_at DESC LIMIT 1 FOR UPDATE
                """,
                (project_id, agent_key, channel, chat_id),
            )
            current = await cursor.fetchone()
            stored_window = int(
                (current or {}).get("context_window_tokens")
                or self.settings.default_context_window_tokens
            )
            stored_input = int((current or {}).get("input_tokens") or 0)
            stored_ratio = stored_input / stored_window if stored_window > 0 else 0.0
            threshold_rotation = stored_ratio > self.settings.context_soft_ratio
            rotation_needed = bool(
                current and (
                    current["status"] == "rotation_required"
                    or current.get("usage_known") is False
                    or threshold_rotation
                    or bool(rotation_checkpoint)
                )
            )
            if current and not rotation_needed:
                await connection.commit()
                return current
            parent_id = str(current["session_id"]) if current else None
            checkpoint = dict(rotation_checkpoint or {})
            if rotation_needed:
                if not checkpoint:
                    raise RuntimeError("a validated handoff is required before session rotation")
                lineage = dict(checkpoint.get("lineage") or {})
                if str(lineage.get("from_session_id") or "") != parent_id:
                    raise ValueError("rotation checkpoint belongs to another session")
                expected_hash = str(checkpoint.get("content_sha256") or "")
                # ``document_ref`` is the immutable storage receipt for the
                # handoff payload.  It is attached after the payload hash is
                # computed and therefore is not part of the signed material.
                unsigned = {
                    key: value for key, value in checkpoint.items()
                    if key not in {"content_sha256", "document_ref"}
                }
                if not expected_hash or canonical_sha256(unsigned) != expected_hash:
                    raise ValueError("rotation checkpoint hash is invalid")
            logical_session_id = (
                str(current["logical_session_id"]) if current
                else session_id
            )
            checkpoint_hash = str(checkpoint.get("content_sha256") or "") or None
            coverage = dict(checkpoint.get("coverage") or {})
            cutoff_message_id = coverage.get("to_message_id")
            checkpoint_created_at = checkpoint.get("created_at")
            if current:
                await connection.execute(
                    """UPDATE assistant.sessions
                    SET status='sealed',handoff_checkpoint_json=%s,
                        handoff_checkpoint_sha256=%s,handoff_cutoff_message_id=%s,
                        handoff_created_at=%s,ended_at=now(),updated_at=now()
                    WHERE session_id=%s""",
                    (
                        Jsonb(json_compatible(checkpoint)), checkpoint_hash,
                        cutoff_message_id, checkpoint_created_at, current["session_id"],
                    ),
                )
            cursor = await connection.execute(
                """
                INSERT INTO assistant.sessions(
                    session_id,logical_session_id,project_id,agent_key,channel,chat_id,
                    parent_session_id,status,handoff_checkpoint_json,
                    handoff_checkpoint_sha256,handoff_cutoff_message_id,handoff_created_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,'active',%s,%s,%s,%s) RETURNING *
                """,
                (
                    session_id, logical_session_id, project_id, agent_key, channel,
                    chat_id, parent_id, Jsonb(json_compatible(checkpoint)),
                    checkpoint_hash, cutoff_message_id, checkpoint_created_at,
                ),
            )
            created = await cursor.fetchone()
            await connection.commit()
            return created

    async def record_session_turn(
        self, session_id: str, native_session_id: str, input_tokens: int,
        context_window_tokens: int, archive_uri: str | None, archive_sha256: str | None,
    ) -> dict[str, Any]:
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                """
                UPDATE assistant.sessions
                SET native_session_id=%s,input_tokens=%s,context_window_tokens=%s,
                    archive_uri=COALESCE(%s,archive_uri),archive_sha256=COALESCE(%s,archive_sha256),
                    updated_at=now()
                WHERE session_id=%s AND status='active' RETURNING *
                """,
                (
                    native_session_id, max(input_tokens, 0), context_window_tokens,
                    archive_uri, archive_sha256, session_id,
                ),
            )
            row = await cursor.fetchone()
            await connection.commit()
            if row is None:
                raise RuntimeError("active session was not found")
            return row

    async def create_outbound(
        self, *, project_id: str, session_id: str | None, run_id: str | None,
        channel: str, chat_id: str, user_id: str | None, message_type: str,
        content: dict[str, Any], reply_to_message_id: str | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        message_id = str(uuid.uuid4())
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                """
                INSERT INTO assistant.messages(
                    message_id,project_id,session_id,run_id,channel,chat_id,user_id,direction,
                    message_type,reply_to_message_id,content_json,delivery_status,idempotency_key
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,'outbound',%s,%s,%s,'pending',%s)
                ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL
                DO UPDATE SET idempotency_key=EXCLUDED.idempotency_key RETURNING *
                """,
                (
                    message_id, project_id, session_id, run_id, channel, chat_id, user_id,
                    message_type, reply_to_message_id, Jsonb(json_compatible(content)),
                    idempotency_key,
                ),
            )
            row = await cursor.fetchone()
            if (
                str(row["project_id"]) != project_id
                or str(row["channel"]) != channel
                or str(row["chat_id"]) != chat_id
                or str(row["message_type"]) != message_type
                or (row["content_json"] or {}) != json_compatible(content)
            ):
                raise ValueError("idempotency_key was reused for different outbound content")
            await connection.commit()
            return row

    async def queue_supervisor_status(
        self, *, project_id: str, run_id: str, channel: str, chat_id: str,
        user_id: str | None, text: str, turn_number: int,
        decision: str, prompt_sha256: str | None,
        card_revision: int = 0,
        delivery_route: str = "trigger_channel",
        decision_id: str | None = None,
        decision_ref: str | None = None,
        action_plan_ref: dict[str, Any] | None = None,
        recommended_prompt: str | None = None,
        confirmation_class: str = "user_required",
        source_context_epoch: int | None = None,
        source_turn_number: int | None = None,
        source_supervisor_run_id: str | None = None,
    ) -> dict[str, Any]:
        """Queue one independent v2 Supervisor card at the current Task head."""
        async with self.database.connection() as connection:
            task = await (await connection.execute(
                "SELECT * FROM assistant.runs WHERE run_id=%s FOR UPDATE",
                (run_id,),
            )).fetchone()
            if task is None or str(task["project_id"]) != project_id:
                raise PermissionError("Supervisor status Task is unavailable")
            request = dict(task.get("request_json") or {})
            current_epoch = int(request.get("supervisor_context_epoch") or 0)
            effective_epoch = (
                current_epoch if source_context_epoch is None
                else int(source_context_epoch)
            )
            if effective_epoch != current_epoch:
                raise PermissionError("Supervisor result belongs to a stale Task context")
            current_turn = int(request.get("task_turn_count") or 0)
            requested_turn = int(source_turn_number or 0)
            effective_turn = requested_turn if requested_turn > 0 else current_turn
            if current_turn > 0 and effective_turn != current_turn:
                raise PermissionError("Supervisor result belongs to a stale Task turn")
            idempotency_key = f"supervisor-status:{run_id}:{turn_number}:{card_revision}"
            existing = await (await connection.execute(
                "SELECT * FROM assistant.messages WHERE idempotency_key=%s",
                (idempotency_key,),
            )).fetchone()
            if existing is not None:
                body = dict(existing.get("content_json") or {})
                if (
                    str(existing["project_id"]) != project_id
                    or str(body.get("task_id") or "") != run_id
                    or int(body.get("card_revision") or 0) != int(card_revision)
                    or str(body.get("decision_id") or "") != str(decision_id or "")
                    or str(body.get("recommended_prompt") or "")
                       != str(recommended_prompt or "")
                    or dict(body.get("action_plan_ref") or {})
                       != dict(action_plan_ref or {})
                    or int(body.get("source_context_epoch") or 0) != effective_epoch
                ):
                    raise ValueError("Supervisor status idempotency key was reused")
                await connection.commit()
                return existing

            latest_revision = await (await connection.execute(
                """
                SELECT max(COALESCE((content_json->>'card_revision')::integer,0)) AS revision
                FROM assistant.messages
                WHERE project_id=%s
                  AND content_json->>'interaction_kind'='supervisor_status'
                  AND content_json->>'task_id'=%s
                """,
                (project_id, run_id),
            )).fetchone()
            prior_revision = int((latest_revision or {}).get("revision") or 0)
            if prior_revision and int(card_revision) <= prior_revision:
                raise ValueError("Supervisor card revision must increase monotonically")
            await self._supersede_open_supervisor_cards_locked(
                connection, task_id=run_id,
                reason="new_supervisor_decision", context_epoch=current_epoch,
            )
            accepted_plan = dict(
                (request.get("accepted_documents") or {}).get(
                    "current_action_plan"
                ) or {}
            )
            can_adopt = bool(
                decision_id and action_plan_ref
                and str(task["status"]) == "waiting_user"
                and dict(action_plan_ref) == accepted_plan
            )
            actions: list[dict[str, Any]] = []
            if decision_id and confirmation_class == "user_required":
                if can_adopt:
                    actions.append({
                        "id": "adopt", "label": "采纳计划", "style": "success",
                    })
                actions.extend([
                    {"id": "dismiss", "label": "取消计划", "style": "secondary"},
                    {"id": "revise", "label": "需要修改", "style": "primary"},
                ])
            content = {
                "text": text, "origin": "supervisor",
                "context_visibility": "ui_only",
                "interaction_kind": "supervisor_status",
                "task_id": run_id, "turn_number": turn_number,
                "decision": decision, "prompt_sha256": prompt_sha256,
                "card_revision": int(card_revision),
                "delivery_route": str(delivery_route),
                "decision_id": decision_id,
                "decision_ref": decision_ref,
                "action_plan_ref": json_compatible(action_plan_ref),
                "recommended_prompt": recommended_prompt,
                "confirmation_class": confirmation_class,
                "action_policy": "supervisor-card-actions.v2",
                "action_status": (
                    "auto_applied" if confirmation_class == "safe"
                    else "invalid" if confirmation_class == "invalid"
                    else "open"
                ),
                "source_context_epoch": effective_epoch,
                "source_turn_number": effective_turn,
                "source_supervisor_run_id": source_supervisor_run_id,
                "resolved_at": None,
                "resolved_by_user_id": None,
                "resolution_action": None,
                "superseded_reason": None,
                "actions": actions,
            }
            message = await (await connection.execute(
                """
                INSERT INTO assistant.messages(
                    message_id,project_id,run_id,channel,chat_id,user_id,direction,
                    message_type,content_json,delivery_status,idempotency_key
                ) VALUES (%s,%s,%s,%s,%s,%s,'outbound','kernel_notice',%s,'pending',%s)
                ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL
                DO UPDATE SET idempotency_key=EXCLUDED.idempotency_key RETURNING *
                """,
                (
                    str(uuid.uuid4()), project_id, run_id, channel, chat_id, user_id,
                    Jsonb(json_compatible(content)), idempotency_key,
                ),
            )).fetchone()
            await connection.commit()
            return message

    async def record_supervisor_action(
        self, *, card_message_id: str, external_event_id: str,
        actor_user_id: str, action: str,
    ) -> dict[str, Any]:
        """Record one explicit user decision on a Supervisor status card.

        PostgreSQL is authoritative.  Vault decision/memory files are derived
        projections written by the channel service after this transaction.
        """
        if action in {"continue", "reject"}:
            raise PermissionError("legacy Supervisor card actions are no longer valid")
        if action not in {"adopt", "dismiss", "revise"}:
            raise ValueError("unsupported Supervisor action")
        if not actor_user_id:
            raise PermissionError("Supervisor action requires an identified user")
        async with self.database.connection() as connection:
            duplicate = await (await connection.execute(
                """
                SELECT * FROM assistant.messages
                WHERE channel='discord' AND external_event_id=%s
                """,
                (external_event_id,),
            )).fetchone()
            if duplicate is not None:
                prior = dict(duplicate.get("content_json") or {})
                if (
                    str(duplicate.get("reply_to_message_id") or "") != card_message_id
                    or str(duplicate.get("user_id") or "") != actor_user_id
                    or prior.get("action") != action
                    or prior.get("schema_version") not in {
                        "decision-record.v1", "decision-record.v2"
                    }
                ):
                    raise ValueError("Supervisor action event was reused")
                await connection.commit()
                return {**prior, "duplicate": True}

            card_head = await (await connection.execute(
                """
                SELECT * FROM assistant.messages
                WHERE message_id=%s AND direction='outbound'
                  AND content_json->>'interaction_kind'='supervisor_status'
                """,
                (card_message_id,),
            )).fetchone()
            if card_head is None:
                raise PermissionError("Supervisor status card is unknown")
            head_content = dict(card_head.get("content_json") or {})
            task_id = str(head_content.get("task_id") or card_head.get("run_id") or "")
            task = await (await connection.execute(
                "SELECT * FROM assistant.runs WHERE run_id=%s FOR UPDATE",
                (task_id,),
            )).fetchone()
            card = await (await connection.execute(
                """
                SELECT * FROM assistant.messages
                WHERE message_id=%s AND direction='outbound'
                  AND content_json->>'interaction_kind'='supervisor_status'
                FOR UPDATE
                """,
                (card_message_id,),
            )).fetchone()
            if card is None or task is None:
                raise PermissionError("Supervisor Decision Task is unavailable")
            if card.get("user_id") is not None and str(card["user_id"]) != actor_user_id:
                raise PermissionError("Supervisor status card belongs to another user")
            content = dict(card["content_json"] or {})
            if content.get("action_policy") != "supervisor-card-actions.v2":
                raise PermissionError("legacy Supervisor card is no longer actionable")
            if content.get("action_status") != "open":
                raise PermissionError("Supervisor status card is no longer open")
            if str(task["project_id"]) != str(card["project_id"]):
                raise PermissionError("Supervisor Decision belongs to another Project")
            decision_id = str(content.get("decision_id") or "")
            action_plan_ref = dict(content.get("action_plan_ref") or {})
            revision = int(content.get("card_revision") or 0)
            if not task_id or not decision_id:
                raise ValueError("Supervisor card has no actionable Decision")
            allowed = {
                str(item.get("id") or "") for item in content.get("actions") or []
                if isinstance(item, dict) and not item.get("disabled")
            }
            if action not in allowed:
                raise PermissionError("Supervisor action is not available on this card")
            task_request = dict(task.get("request_json") or {})
            current_epoch = int(task_request.get("supervisor_context_epoch") or 0)
            source_epoch = int(content.get("source_context_epoch") or 0)
            if source_epoch != current_epoch:
                raise PermissionError("Supervisor status card belongs to a stale context")
            current_turn = int(
                task_request.get("task_turn_count")
                or content.get("source_turn_number") or 0
            )
            if int(content.get("source_turn_number") or 0) != current_turn:
                raise PermissionError("Supervisor status card belongs to a stale Task turn")
            if str(task["status"]) in {"succeeded", "failed", "cancelled", "dead_letter"}:
                raise PermissionError("Supervisor status card belongs to a terminal Task")
            newer = await (await connection.execute(
                """
                SELECT 1 FROM assistant.messages
                WHERE project_id=%s
                  AND content_json->>'interaction_kind'='supervisor_status'
                  AND content_json->>'task_id'=%s
                  AND COALESCE((content_json->>'card_revision')::integer,0)>%s
                LIMIT 1
                """,
                (card["project_id"], task_id, revision),
            )).fetchone()
            if newer is not None:
                raise PermissionError("Supervisor status card was superseded")

            if action == "adopt":
                if str(task["status"]) != "waiting_user":
                    raise PermissionError("PM Task is not at an Action Plan boundary")
                accepted_plan = dict(
                    (task_request.get("accepted_documents") or {}).get(
                        "current_action_plan"
                    ) or {}
                )
                if not action_plan_ref or action_plan_ref != accepted_plan:
                    raise ValueError("Supervisor card Action Plan is not current")

            record_id = f"dec-{uuid.uuid4().hex[:12]}"
            decided_at = datetime.now(timezone.utc).isoformat()
            status = {
                "adopt": "adopted", "dismiss": "dismissed",
                "revise": "revision_requested",
            }[action]
            decision_payload = {
                "schema_version": "decision-record.v2",
                "decision_record_id": record_id,
                "decision_id": record_id,
                "kind": {
                    "adopt": "action_plan_adoption",
                    "dismiss": "action_plan_cancellation",
                    "revise": "action_plan_revision_request",
                }[action],
                "action": action,
                "status": status,
                "task_id": task_id,
                "supervisor_decision_id": decision_id,
                "target_ref": action_plan_ref or content.get("decision_ref"),
                "card_revision": revision,
                "source_context_epoch": source_epoch,
                "actor_user_id": actor_user_id,
                "source_message_ids": [card_message_id],
                "scope": {
                    "task_id": task_id,
                    "source_context_epoch": source_epoch,
                    "source_turn_number": current_turn,
                },
                "effective_at": decided_at,
                "expires_at": None,
                "supersedes_ref": None,
                "revokes_ref": None,
                "source_message_ids": [card_message_id],
                "decided_at": decided_at,
            }
            incoming = await (await connection.execute(
                """
                INSERT INTO assistant.messages(
                    message_id,project_id,run_id,channel,chat_id,user_id,direction,
                    message_type,external_event_id,reply_to_message_id,content_json,
                    delivery_status,idempotency_key
                ) VALUES (%s,%s,%s,'discord',%s,%s,'inbound','action',%s,%s,%s,
                          'received',%s)
                RETURNING *
                """,
                (
                    str(uuid.uuid4()), card["project_id"], task_id, card["chat_id"],
                    actor_user_id, external_event_id, card_message_id,
                    Jsonb(json_compatible(decision_payload)),
                    f"supervisor-action:{card_message_id}:{action}",
                ),
            )).fetchone()

            next_epoch = current_epoch + 1
            resolved_content = {
                **content,
                "action_status": status,
                "resolved_at": decided_at,
                "resolved_by_user_id": actor_user_id,
                "resolution_action": action,
                "resolved_context_epoch": next_epoch,
                "actions": [
                    {**dict(item), "disabled": True}
                    for item in content.get("actions") or []
                    if isinstance(item, dict)
                ],
            }
            updated = await (await connection.execute(
                """
                UPDATE assistant.messages SET content_json=%s,updated_at=now()
                WHERE message_id=%s AND content_json->>'action_status'='open'
                RETURNING message_id
                """,
                (Jsonb(json_compatible(resolved_content)), card_message_id),
            )).fetchone()
            if updated is None:
                raise PermissionError("Supervisor status card was already resolved")

            task_patch: dict[str, Any] = {
                "supervisor_context_epoch": next_epoch,
                "supervisor_action_status": {
                    "adopt": "user_confirmed",
                    "dismiss": "cancelled",
                    "revise": "revision_requested",
                }[action],
            }
            if action == "adopt":
                pending_validation_turn = int(
                    task_request.get("pending_validation_supervisor_turn") or 0
                )
                if pending_validation_turn == current_turn:
                    task_patch.update({
                        "validation_supervisor_approved_turn": pending_validation_turn,
                        "task_phase": "working",
                    })
                await connection.execute(
                    """
                    UPDATE assistant.runs
                    SET request_json=request_json || %s,
                        status='queued',available_at=now(),
                        waiting_question_message_id=NULL,ended_at=NULL,
                        updated_at=now()
                    WHERE run_id=%s AND status='waiting_user'
                    """,
                    (Jsonb(json_compatible(task_patch)), task_id),
                )
                await connection.execute(
                    """
                    INSERT INTO assistant.messages(
                        message_id,project_id,run_id,channel,chat_id,user_id,direction,
                        message_type,content_json,delivery_status,idempotency_key
                    )
                    SELECT %s,r.project_id,r.run_id,t.channel,t.chat_id,%s,'system',
                           'action',%s,'received',%s
                    FROM assistant.runs r
                    JOIN assistant.messages t ON t.message_id=r.trigger_message_id
                    WHERE r.run_id=%s
                    ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL
                    DO NOTHING
                    """,
                    (
                        str(uuid.uuid4()), actor_user_id,
                        Jsonb({
                            **json_compatible(decision_payload),
                            "context_visibility": "semantic",
                            "routed_to": "pm",
                        }),
                        f"supervisor-pm-handoff:{record_id}", task_id,
                    ),
                )
            else:
                await connection.execute(
                    """
                    UPDATE assistant.runs
                    SET request_json=request_json || %s,updated_at=now()
                    WHERE run_id=%s
                    """,
                    (Jsonb(json_compatible(task_patch)), task_id),
                )
            await self._supersede_open_supervisor_cards_locked(
                connection, task_id=task_id, reason=f"action_{action}",
                context_epoch=next_epoch,
            )
            await connection.commit()
            return {
                **decision_payload,
                "message_id": str(incoming["message_id"]),
                "duplicate": False,
            }

    async def wait_for_user(
        self, run_id: str, worker_instance_id: str, question: dict[str, Any],
        *, project_id: str, session_id: str, channel: str, chat_id: str,
        user_id: str | None, reply_to_message_id: str | None,
    ) -> dict[str, Any]:
        message_id = str(uuid.uuid4())
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                """
                INSERT INTO assistant.messages(
                    message_id,project_id,session_id,run_id,channel,chat_id,user_id,direction,
                    message_type,reply_to_message_id,content_json,delivery_status,idempotency_key
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,'outbound','question',%s,%s,'pending',%s)
                ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL
                DO UPDATE SET idempotency_key=EXCLUDED.idempotency_key RETURNING *
                """,
                (
                    message_id, project_id, session_id, run_id, channel, chat_id, user_id,
                    reply_to_message_id, Jsonb(json_compatible(question)), f"question:{run_id}",
                ),
            )
            message = await cursor.fetchone()
            if (message["content_json"] or {}) != json_compatible(question):
                raise ValueError("the persisted question does not match this run result")
            cursor = await connection.execute(
                """
                UPDATE assistant.runs
                SET status='waiting_user',waiting_question_message_id=%s,result_json=%s,
                    worker_id=NULL,worker_instance_id=NULL,lease_until=NULL,updated_at=now()
                WHERE run_id=%s AND worker_instance_id=%s AND status='running' RETURNING run_id
                """,
                (message["message_id"], Jsonb({"outcome": "needs_input"}), run_id, worker_instance_id),
            )
            if await cursor.fetchone() is None:
                raise RuntimeError("run lease was lost before waiting for user")
            await connection.commit()
            return message

    async def claim_delivery(
        self, worker_id: str, worker_instance_id: str, lease_seconds: int,
        channel: str | None = None,
    ) -> dict[str, Any] | None:
        async with self.database.connection() as connection:
            await connection.execute(
                """
                UPDATE assistant.messages SET delivery_status='failed',lease_until=NULL,
                    error_text='delivery lease expired after retry limit',updated_at=now()
                WHERE direction='outbound' AND delivery_status='delivering'
                  AND lease_until<now() AND retry_count>=max_retries
                """
            )
            await connection.execute(
                """
                UPDATE assistant.messages SET delivery_status='pending',worker_id=NULL,
                    worker_instance_id=NULL,lease_until=NULL,available_at=now(),updated_at=now()
                WHERE direction='outbound' AND delivery_status='delivering'
                  AND lease_until<now() AND retry_count<max_retries
                """
            )
            cursor = await connection.execute(
                """
                WITH candidate AS (
                    SELECT message_id FROM assistant.messages
                    WHERE direction='outbound' AND delivery_status='pending' AND available_at<=now()
                      AND (%s::text IS NULL OR channel=%s)
                    ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1
                )
                UPDATE assistant.messages m
                SET delivery_status='delivering',retry_count=retry_count+1,
                    worker_id=%s,worker_instance_id=%s,
                    lease_until=now()+(%s*interval '1 second'),updated_at=now()
                FROM candidate c WHERE m.message_id=c.message_id RETURNING m.*
                """,
                (channel, channel, worker_id, worker_instance_id, lease_seconds),
            )
            row = await cursor.fetchone()
            await connection.commit()
            return row

    async def delivery_backlog(self, channel: str) -> dict[str, int]:
        async with self.database.connection() as connection:
            row = await (
                await connection.execute(
                    """
                    SELECT
                      count(*) FILTER (WHERE delivery_status='pending') AS pending,
                      count(*) FILTER (WHERE delivery_status='delivering') AS delivering,
                      count(*) FILTER (WHERE delivery_status='failed') AS failed
                    FROM assistant.messages
                    WHERE channel=%s AND direction='outbound'
                    """,
                    (channel,),
                )
            ).fetchone()
            return {key: int(row.get(key) or 0) for key in ("pending", "delivering", "failed")}

    async def delivery_health(self, channel: str) -> dict[str, int]:
        async with self.database.connection() as connection:
            row = await (
                await connection.execute(
                    """
                    SELECT
                      count(*) FILTER (WHERE delivery_status='pending') AS pending,
                      count(*) FILTER (WHERE delivery_status='delivering') AS delivering,
                      count(*) FILTER (
                        WHERE delivery_status='failed'
                          AND updated_at>=now()-interval '15 minutes'
                      ) AS failed_recent_15m,
                      COALESCE(max(EXTRACT(EPOCH FROM (now()-created_at))) FILTER (
                        WHERE delivery_status='pending'
                      ),0)::bigint AS oldest_pending_delivery_seconds
                    FROM assistant.messages
                    WHERE direction='outbound' AND channel=%s
                    """,
                    (channel,),
                )
            ).fetchone()
        return {
            key: int(row.get(key) or 0)
            for key in (
                "pending", "delivering", "failed_recent_15m",
                "oldest_pending_delivery_seconds",
            )
        }

    async def execution_health(self) -> dict[str, int]:
        async with self.database.connection() as connection:
            row = await (
                await connection.execute(
                    """
                    SELECT
                      count(*) FILTER (WHERE status='queued') AS queued,
                      count(*) FILTER (WHERE status='running') AS running,
                      COALESCE(max(EXTRACT(EPOCH FROM (now()-created_at))) FILTER (
                        WHERE status='queued'
                      ),0)::bigint AS oldest_queued_seconds
                    FROM assistant.runs
                    """
                )
            ).fetchone()
        return {
            key: int(row.get(key) or 0)
            for key in ("queued", "running", "oldest_queued_seconds")
        }

    async def list_active_discord_component_messages(
        self, limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Return sent Discord messages whose persistent components may still act."""
        async with self.database.connection() as connection:
            rows = list(await (
                await connection.execute(
                    """
                    SELECT m.*
                    FROM assistant.messages m
                    LEFT JOIN assistant.runs r ON r.run_id=m.run_id
                    WHERE m.channel='discord' AND m.direction='outbound'
                      AND m.delivery_status='sent' AND m.external_message_id IS NOT NULL
                      AND (
                        (m.message_type IN ('question','approval') AND r.status='waiting_user')
                        OR (
                          m.message_type='reconciliation'
                          AND m.content_json->>'interaction_kind'='reconcile_run'
                          AND r.status='reconciling'
                          AND r.executor_json#>>'{reconciliation,card_message_id}'=m.message_id::text
                        )
                        OR (
                          m.content_json->>'interaction_kind'='resume_select'
                          AND COALESCE(
                            (m.content_json->>'component_expires_at')::timestamptz,
                            m.created_at + interval '7 days'
                          ) > now()
                          AND EXISTS (
                            SELECT 1
                            FROM jsonb_array_elements(
                              COALESCE(m.content_json->'resume_options','[]'::jsonb)
                            ) option
                            JOIN assistant.runs pending_run
                              ON pending_run.run_id=(option->>'id')::uuid
                            WHERE pending_run.status='waiting_user'
                          )
                        )
                      )
                    ORDER BY m.created_at DESC,m.message_id DESC LIMIT %s
                    """,
                    (min(max(limit, 1), 2000),),
                )
            ).fetchall())
        return rows

    async def confirm_delivery_current(
        self, message_id: str, worker_instance_id: str,
    ) -> bool:
        """Fence foreground replies that were overtaken by a /new reset."""
        async with self.database.connection() as connection:
            row = await (
                await connection.execute(
                    """
                    SELECT m.message_id,r.run_type,r.stage,trigger.created_at AS trigger_created_at,
                           (
                             SELECT max(reset.created_at)
                             FROM assistant.messages reset
                             WHERE reset.project_id=m.project_id
                               AND reset.channel=m.channel AND reset.chat_id=m.chat_id
                               AND reset.direction='system'
                               AND reset.message_type='conversation_reset'
                               AND reset.content_json->>'schema_version'='conversation-reset.v1'
                           ) AS reset_created_at
                    FROM assistant.messages m
                    LEFT JOIN assistant.runs r ON r.run_id=m.run_id
                    LEFT JOIN assistant.messages trigger ON trigger.message_id=r.trigger_message_id
                    WHERE m.message_id=%s AND m.worker_instance_id=%s
                      AND m.delivery_status='delivering'
                    FOR UPDATE OF m
                    """,
                    (message_id, worker_instance_id),
                )
            ).fetchone()
            if row is None:
                await connection.rollback()
                return False
            foreground = row.get("run_type") == "chat" or (
                row.get("run_type") == "stage" and row.get("stage") == "requirements"
            )
            stale = bool(
                foreground
                and row.get("reset_created_at") is not None
                and row.get("trigger_created_at") is not None
                and row["trigger_created_at"] < row["reset_created_at"]
            )
            if stale:
                await connection.execute(
                    """
                    UPDATE assistant.messages
                    SET delivery_status='cancelled',worker_id=NULL,worker_instance_id=NULL,
                        lease_until=NULL,error_text='suppressed by conversation reset',updated_at=now()
                    WHERE message_id=%s AND worker_instance_id=%s
                      AND delivery_status='delivering'
                    """,
                    (message_id, worker_instance_id),
                )
            await connection.commit()
            return not stale

    async def acknowledge_delivery(
        self, message_id: str, worker_instance_id: str, external_message_id: str,
        delivery: dict[str, Any] | None = None,
    ) -> bool:
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                """
                UPDATE assistant.messages SET delivery_status='sent',external_message_id=%s,
                    content_json=content_json || %s,worker_id=NULL,worker_instance_id=NULL,
                    lease_until=NULL,sent_at=now(),updated_at=now(),error_text=NULL
                WHERE message_id=%s AND worker_instance_id=%s AND delivery_status='delivering'
                RETURNING message_id
                """,
                (
                    external_message_id, Jsonb({"delivery": json_compatible(delivery or {})}),
                    message_id, worker_instance_id,
                ),
            )
            updated = await cursor.fetchone() is not None
            await connection.commit()
            return updated

    async def fail_delivery(self, message_id: str, worker_instance_id: str, error: str) -> str:
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                """
                UPDATE assistant.messages
                SET delivery_status=CASE WHEN retry_count>=max_retries THEN 'failed' ELSE 'pending' END,
                    available_at=CASE WHEN retry_count>=max_retries THEN available_at
                      ELSE now()+(LEAST(retry_count,6)*interval '10 seconds') END,
                    worker_id=NULL,worker_instance_id=NULL,lease_until=NULL,
                    error_text=%s,updated_at=now()
                WHERE message_id=%s AND worker_instance_id=%s AND delivery_status='delivering'
                RETURNING delivery_status
                """,
                (error[:2000], message_id, worker_instance_id),
            )
            row = await cursor.fetchone()
            await connection.commit()
            return str(row["delivery_status"]) if row else "lease_lost"
