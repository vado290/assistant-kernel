from __future__ import annotations

import base64
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb
from assistant_kernel.services.run_scheduling import run_scheduling
import yaml

from assistant_kernel.config import KernelSettings
from assistant_kernel.repositories.database import AssistantDatabase
from assistant_kernel.storage import ProjectRuntimeManager
from assistant_kernel.services.questionnaire import (
    advance_questionnaire, is_questionnaire, make_questionnaire, questionnaire_eligible,
)


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








class CoreRepository:
    """Persistence boundary for the four-table first-release core."""

    def __init__(self, settings: KernelSettings):
        self.settings = settings
        self.database = AssistantDatabase(settings)

    def _single_agent_project(self, project_id: str) -> bool:
        from assistant_kernel.services.published_agents import PublishedAgentService
        project=PublishedAgentService(self.settings).project_policy(project_id)
        return project.get("task_protocol") == "single-agent.v2"

    def _retired_stage(self, project_id: str, run: dict[str, Any]) -> bool:
        return str(run.get("run_type") or "") == "stage"


    async def _require_active_run(self, connection, run_id):
        from assistant_kernel.services.kernel_management import require_active_identity
        row = await (await connection.execute(
            'SELECT project_id,agent_key FROM assistant.runs WHERE run_id=%s', (run_id,))).fetchone()
        if row:
            require_active_identity(row['project_id'], row['agent_key'])

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
        from assistant_kernel.services.kernel_management import require_active_identity
        require_active_identity(project_id, agent_key)
        if agent_key.endswith((":child", ":operator")):
            raise ValueError("child Run and Operator Agent are retired")
        message_id = str(uuid.uuid4())
        run_id = str(uuid.uuid4())
        superseded_run_ids: list[str] = []
        single_agent_project = self._single_agent_project(project_id)
        async with self.database.connection() as connection:
            if single_agent_project and agent_key.endswith((":manager", ":supervisor")):
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                    (f"project-mainline:{project_id}",),
                )
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
                      AND COALESCE(request_json->>'task_activation','active')='active'
                      AND status IN ('queued','running','waiting_user','waiting_supervisor','blocked','reconciling')
                    ORDER BY created_at,run_id LIMIT 1
                    FOR UPDATE
                    """,
                    (project_id,),
                )).fetchone()
                if task is not None:
                    if self._five_stage_task(task):
                        content = {**content, "task_id": str(task["run_id"]), "task_input_status": "pending"}
                        await connection.execute(
                            "UPDATE assistant.messages SET content_json=%s WHERE message_id=%s",
                            (Jsonb(json_compatible(content)), message_id),
                        )
                        message["content_json"] = content
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
            deferred_approval_reply = False
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
                if current_interaction and question["message_type"] == "approval" and await (await connection.execute("SELECT 1 FROM assistant.messages WHERE run_id=%s AND content_json->>'task_input_status'='pending' LIMIT 1",(question["run_id"],))).fetchone():
                    run = await self._requeue_pending_inputs(connection, {"run_id":question["run_id"]})
                    if (question["run_request_json"] or {}).get('protocol_revision') == '1.2':
                        # The new semantic input belongs to this Task. An old
                        # approval must neither replay PM nor create new work.
                        await connection.execute('UPDATE assistant.messages SET run_id=%s WHERE message_id=%s',
                            (question['run_id'], message_id))
                        run = await self._supervise_answer_locked(connection, run, message_id)
                        deferred_approval_reply = True
                    current_interaction = False
                    stale_reply = True
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
                            and current_request.get("approval_kind") in {"requirements", "design"}
                        )
                        if single_requirements:
                            is_design = current_request.get("approval_kind") == "design"
                            task_contract_ref = dict(
                                current_request.get("solution_design_ref" if is_design else "task_contract_ref") or {}
                            )
                            if not task_contract_ref:
                                raise RuntimeError("Requirements approval has no Task Contract")
                            from assistant_kernel.services.document_runtime import (
                                TaskDocumentService,
                            )
                            contract = TaskDocumentService(self.settings).read(
                                project_id, task_contract_ref
                            )
                            expected = str(current_request.get("solution_design_sha256" if is_design else "task_contract_sha256") or "")
                            if (
                                str(task_contract_ref.get("sha256") or "") != expected
                                or (not is_design and not list((contract.get("payload") or {}).get("requirements") or []))
                            ):
                                raise RuntimeError(
                                    "Task Contract changed after the approval card was created"
                                )
                            if is_design and (
                                not current_request.get("requirements_approval")
                                or (contract.get("payload") or {}).get("task_contract_sha256")
                                   != (current_request.get("task_contract_ref") or {}).get("sha256")
                            ):
                                raise ValueError("Design approval does not match the current approved requirements")
                            next_mode = "build" if is_design or not self._five_stage_project(project_id) else "design"
                            approval_patch = {
                                "current_stage": next_mode, "work_mode": next_mode, "task_phase": "working",
                                "execution_plan_variant": next_mode, "approval_kind": None,
                                "answer_message_id": message_id,
                                ("design_sha256" if is_design else "requirements_sha256"): expected,
                                ("design_approval" if is_design else "requirements_approval"): {
                                    "message_id": message_id, "approved_document_sha256": expected,
                                    ("solution_design_ref" if is_design else "task_contract_ref"): task_contract_ref,
                                },
                            }
                            if not is_design:
                                approval_patch["design_approval"] = None
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
                                    Jsonb(approval_patch),
                                    Jsonb({
                                        "approval": "approved",
                                        "approval_message_id": message_id,
                                        "transition": f"{'design' if is_design else 'requirements'}_to_{next_mode}",
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
                            run = await self._supervise_answer_locked(connection, run, message_id)
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
                                        "text": f"{'Design' if is_design else 'Requirements'}已批准；进入{next_mode}前完成Supervisor评估。",
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
                        if current_request.get("task_protocol") == "single-agent.v2" and current_request.get("approval_kind") == "operation":
                            prepared = dict(current_request.get("operation_approval_request") or {})
                            card_request = dict((question.get("content_json") or {}).get("operation_approval_request") or {})
                            for key in ("action_id", "action_definition_sha256", "baseline_sha256", "input_sha256"):
                                if not prepared.get(key) or prepared.get(key) != card_request.get(key):
                                    raise ValueError("Operation approval no longer matches this card")
                            approved_actions = dict(current_request.get("approved_actions") or {})
                            approved_actions[str(prepared["action_id"])] = {
                                "approval_message_id": message_id,
                                **{key: prepared[key] for key in ("action_definition_sha256", "baseline_sha256", "input_sha256")},
                            }
                            run = await (await connection.execute(
                                """UPDATE assistant.runs SET status='queued',available_at=now(),
                                    waiting_question_message_id=NULL,request_json=request_json || %s,
                                    updated_at=now() WHERE run_id=%s AND status='waiting_user' RETURNING *""",
                                (Jsonb({"approved_actions": approved_actions, "approval_kind": None,
                                        "operation_approval_request": None, "answer_message_id": message_id}),
                                 question["run_id"]),
                            )).fetchone()
                            if run is None:
                                raise RuntimeError("Operation is no longer waiting")
                            await connection.execute("UPDATE assistant.messages SET run_id=%s WHERE message_id=%s", (run["run_id"], message_id))
                            run = await self._supervise_answer_locked(connection, run, message_id)
                            await connection.commit()
                            return {"message": message, "run": run, "duplicate": False, "resumed": True,
                                    "stale_reply": False, "approval_decision": "approve", "decision_required": False,
                                    "partial_answer": False, "correction_run": False, "superseded_run_ids": []}
                        if current_request.get('protocol_revision')=='1.2' and current_request.get('approval_kind')=='release':
                            from assistant_kernel.services.prompt_protocol import PromptProtocol
                            root=await (await connection.execute('SELECT * FROM assistant.runs WHERE run_id=%s FOR UPDATE',(question['run_id'],))).fetchone()
                            compiler=PromptProtocol(self.settings,self)
                            if (question['content_json'].get('publication_bundle_ref')!=current_request.get('sealed_candidate_bundle')
                                    or question['content_json'].get('review_ref')!=(current_request.get('accepted_documents') or {}).get('review_report')
                                    or await compiler.input_head_locked(connection,root)!=current_request.get('publication_input_head_ref')):
                                raise PermissionError('Specific publication approval is stale; sealed bytes, Review or source inputs changed')
                            publication=await self._queue_approved_publication_locked(connection,root,message_id,authorization_kind='final')
                            await connection.execute("UPDATE assistant.runs SET waiting_question_message_id=NULL,request_json=request_json || %s WHERE run_id=%s",
                                (Jsonb({'approval_kind':None}),root['run_id']))
                            await connection.execute('UPDATE assistant.messages SET run_id=%s WHERE message_id=%s',(root['run_id'],message_id))
                            await connection.commit()
                            return {'message':message,'run':publication['publication_run'],'duplicate':False,'resumed':True,
                                'stale_reply':False,'approval_decision':'approve','decision_required':False,'partial_answer':False,
                                'correction_run':False,'superseded_run_ids':[]}
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
                    elif decision == 'revise' and (question['run_request_json'] or {}).get('protocol_revision') == '1.2':
                        run = await (await connection.execute('SELECT * FROM assistant.runs WHERE run_id=%s',
                            (question['run_id'],))).fetchone()
                        await connection.execute('UPDATE assistant.messages SET run_id=%s WHERE message_id=%s',
                            (question['run_id'], message_id))
                        run = await self._supervise_answer_locked(connection, run, message_id)
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
                                run = await self._supervise_answer_locked(connection, run, message_id)
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
                and not retired_stage_reply and not closed_release_reply and not deferred_approval_reply
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
                if (
                    message_type == "text"
                    and agent_key == f"project:{project_id}:manager"
                    and single_agent_project
                ):
                    # Only PM-manager texts can open a second Task; supervisor
                    # and operator conversation messages always flow through.
                    active_task = await (
                        await connection.execute(
                            """
                            SELECT * FROM assistant.runs
                            WHERE project_id=%s AND agent_key=%s AND run_type='chat'
                              AND mutex_key=%s
                              AND COALESCE(request_json->>'task_activation','active')='active'
                              AND status IN ('queued','running','reconciling','waiting_user','waiting_supervisor','blocked')
                            ORDER BY created_at,run_id LIMIT 1
                            FOR UPDATE
                            """,
                            (project_id,agent_key,f"engineering:{project_id}"),
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
                                    "task_input_status": "pending",
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
                                            "补充消息已绑定当前Task，将在后续Turn处理；审批和恢复条件保持有效。\n"
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
                            }),
                            Jsonb(json_compatible({
                                **dict(executor or {}),
                            })),
                            f"run:message:{message_id}",
                        ),
                    )
                    run = await cursor.fetchone()
                    await connection.execute(
                        "UPDATE assistant.messages SET run_id=%s WHERE message_id=%s",
                        (run_id, message_id),
                    )
            if run is not None:
                run = await self._persist_scheduling(connection, run)
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
            await self._require_active_run(connection, run_id)
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
            if resumed:
                current = await (await connection.execute("SELECT * FROM assistant.runs WHERE run_id=%s", (run_id,))).fetchone()
                if current:
                    await self._supervise_answer_locked(connection, current, str(incoming["message_id"]))
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
            row = await cursor.fetchone()
            if row:
                from assistant_kernel.services.question_groups import QuestionGroupService, projection_ref, REFRESH_SCHEMA
                if projection_ref(row['content_json'] or {}) or (row['content_json'] or {}).get('schema_version')==REFRESH_SCHEMA:
                    row = await QuestionGroupService(self.settings,self).hydrate(connection,row)
            return row

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

    async def record_questionnaire_action(
        self, *, message_id: str, project_id: str, channel: str, chat_id: str,
        user_id: str, external_event_id: str, revision: int, page: int,
        action: str, value: str | None = None,
    ) -> dict[str, Any]:
        """Persist drafts; atomically accept a final answer and resume once."""
        if not user_id:
            raise PermissionError("questionnaire requires an identified user")
        from assistant_kernel.services.question_groups import QuestionGroupService, projection_ref
        async with self.database.connection() as lookup:
            projection = await (await lookup.execute('SELECT content_json,run_id FROM assistant.messages WHERE message_id=%s',
                (message_id,))).fetchone()
            if projection and projection['run_id']:
                await self._require_active_run(lookup, str(projection['run_id']))
        if projection and projection_ref(projection['content_json'] or {}):
            return await QuestionGroupService(self.settings,self).record_action(message_id=message_id,
                project_id=project_id,channel=channel,chat_id=chat_id,user_id=user_id,
                external_event_id=external_event_id,revision=revision,page=page,action=action,value=value)
        event_request = {
            "message_id": message_id, "revision": revision, "page": page,
            "action": action, "value": value,
        }
        event_key = f"questionnaire:{channel}:{external_event_id}"
        async with self.database.connection() as connection:
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (f"conversation-command:{project_id}:{channel}:{chat_id}",),
            )
            row = await (await connection.execute(
                """
                SELECT m.*,r.status AS run_status,r.run_type,r.request_json AS run_request_json,
                       r.result_json AS run_result_json,r.interaction_seq AS run_interaction_seq,
                       r.waiting_question_message_id
                FROM assistant.messages m JOIN assistant.runs r ON r.run_id=m.run_id
                WHERE m.message_id=%s AND m.project_id=%s AND m.channel=%s AND m.chat_id=%s
                  AND m.user_id IS NOT DISTINCT FROM %s
                  AND m.direction='outbound' AND m.message_type='question'
                FOR UPDATE OF r,m
                """,
                (message_id, project_id, channel, chat_id, user_id),
            )).fetchone()
            if row is None:
                raise PermissionError("questionnaire belongs to another user or conversation")
            body = dict(row["content_json"] or {})
            if not is_questionnaire(body):
                raise ValueError("message is not a questionnaire")
            existing = await (await connection.execute(
                "SELECT * FROM assistant.messages WHERE idempotency_key=%s", (event_key,),
            )).fetchone()
            if existing:
                if (
                    str(existing["user_id"]) != user_id
                    or (existing["content_json"] or {}).get("event_request") != event_request
                ):
                    raise ValueError("questionnaire event ID was reused with different content")
                await connection.commit()
                return {"message": dict(row), "duplicate": True, "resumed": False}
            current_ids = {
                str(item) for item in (row["run_result_json"] or {}).get("question_message_ids") or []
            }
            if not current_ids:
                current_ids = {str(row["waiting_question_message_id"])}
            reset = await (await connection.execute(
                """
                SELECT 1 FROM assistant.messages
                WHERE project_id=%s AND channel=%s AND chat_id=%s
                  AND direction='system' AND message_type='conversation_reset'
                  AND created_at>%s LIMIT 1
                """, (project_id, channel, chat_id, row["created_at"]),
            )).fetchone()
            if (
                row["run_status"] != "waiting_user" or message_id not in current_ids
                or int(body["interaction_seq"]) != int(row["run_interaction_seq"])
                or reset or self._retired_stage(project_id, {
                    "run_type": row["run_type"], "request_json": row["run_request_json"],
                })
            ):
                raise PermissionError("questionnaire is no longer current")
            if action != "reply" and int(body["questionnaire"]["revision"]) != revision:
                raise ValueError("页面已更新，请刷新卡片后重试。")
            if action == "reply":
                page = int(body["questionnaire"]["cursor"])
            effective_action = (
                "additional" if page == len(body["questions"]) else "text"
            ) if action == "reply" else action
            updated = advance_questionnaire(body, action=effective_action, page=page, value=value)
            submitted = action == "submit"
            state = updated["questionnaire"]
            answer_payload = {
                "answers": dict(state["answers"]), "questions": body["questions"],
                "choice_ids": dict(state["choices"]),
                "additional_text": state["additional_text"],
            }
            incoming_id = str(uuid.uuid4())
            event_content = {
                "schema_version": "questionnaire-answer.v1" if submitted else "questionnaire-action.v1",
                "context_visibility": "semantic" if submitted else "ui_only",
                "event_request": event_request,
                **(answer_payload if submitted else {}),
            }
            await connection.execute(
                """
                INSERT INTO assistant.messages(
                    message_id,project_id,session_id,run_id,channel,chat_id,user_id,
                    external_event_id,direction,message_type,reply_to_message_id,
                    content_json,delivery_status,idempotency_key
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'inbound',%s,%s,%s,'received',%s)
                """,
                (incoming_id, project_id, row["session_id"], row["run_id"], channel,
                 chat_id, user_id, external_event_id, "answer" if submitted else "action",
                 message_id, Jsonb(json_compatible(event_content)), event_key),
            )
            await connection.execute(
                "UPDATE assistant.messages SET content_json=%s,updated_at=now() WHERE message_id=%s",
                (Jsonb(json_compatible(updated)), message_id),
            )
            if submitted:
                resumed_run = await (await connection.execute(
                    """
                    UPDATE assistant.runs
                    SET status='queued',available_at=now(),waiting_question_message_id=NULL,
                        request_json=jsonb_set(
                          COALESCE(request_json,'{}'::jsonb),'{answer_history}',
                          COALESCE(request_json->'answer_history','[]'::jsonb) || %s
                        ) || %s,updated_at=now()
                    WHERE run_id=%s AND status='waiting_user' RETURNING *
                    """,
                    (Jsonb([{"answer_message_id": incoming_id, "answer": answer_payload}]),
                     Jsonb({"answer_message_id": incoming_id, "answer": answer_payload,
                            "answer_state": state["answers"]}), row["run_id"]),
                )).fetchone()
                if resumed_run is not None:
                    await self._supervise_answer_locked(connection, resumed_run, incoming_id)
            await connection.commit()
            return {
                "message": {**dict(row), "content_json": updated},
                "duplicate": False, "resumed": submitted,
            }

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

    def _filter_revoked_context(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        from assistant_kernel.services.public_background import PublicBackgroundService
        revoked = PublicBackgroundService(self.settings.data_root,self.settings).revoked_source_message_ids()
        return [row for row in rows if str(row["message_id"]) not in revoked
                and str(row.get("reply_to_message_id") or "") not in revoked]

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
        # Advance over the raw page even when every visible source was revoked.
        page = rows[:limit]
        selected = self._filter_revoked_context(page)
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
        if has_more and page:
            last = page[-1]
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
            return self._filter_revoked_context(list(await cursor.fetchall()))

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
                      AND COALESCE(r.request_json->>'protocol_revision','')<>'1.2'
                      AND (r.run_type='chat' OR (r.run_type='stage' AND r.stage='requirements'))
                    FOR UPDATE OF r
                    """,
                    (project_id, channel, chat_id),
                )
            ).fetchall())
            from assistant_kernel.services.question_groups import QuestionGroupService
            owners=await (await connection.execute("""SELECT DISTINCT run_id FROM assistant.messages
                WHERE project_id=%s AND channel=%s AND chat_id=%s AND direction='outbound'
                AND content_json->>'schema_version'='question-group-projection.v1' AND run_id IS NOT NULL""",
                (project_id,channel,chat_id))).fetchall()
            for owner in owners:
                await QuestionGroupService(self.settings,self).withdraw_views_locked(connection,owner_id=str(owner['run_id']),
                    source_message_id=source_message_id,channel=channel,chat_id=chat_id)
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
                      AND r.status IN ('waiting_user','waiting_supervisor','blocked')
                      AND (trigger.user_id IS NULL OR trigger.user_id IS NOT DISTINCT FROM %s)
                    ORDER BY r.updated_at DESC,r.run_id DESC LIMIT %s
                    """,
                    (project_id, channel, chat_id, user_id, min(max(limit, 1), 25)),
                )
            ).fetchall())
            from assistant_kernel.services.question_groups import QuestionGroupService
            projections=await (await connection.execute("""SELECT DISTINCT ON(g.message_id) r.run_id,r.project_id,r.agent_key,
                r.run_type,r.stage,r.status,r.created_at,r.updated_at,p.message_id AS waiting_question_message_id,
                p.message_type AS question_message_type,g.content_json->>'task_id' AS logical_task_id
                FROM assistant.messages p JOIN assistant.runs r ON r.run_id=p.run_id
                JOIN assistant.messages g ON g.message_id::text=p.content_json#>>'{question_projection,group_ref,id}'
                WHERE p.project_id=%s AND p.channel=%s AND p.chat_id=%s AND p.user_id IS NOT DISTINCT FROM %s
                AND p.direction='outbound' AND p.message_type='question' AND p.content_json->>'schema_version'='question-group-projection.v1'
                AND g.content_json#>>'{group,status}'='open' AND r.status NOT IN ('succeeded','failed','cancelled','dead_letter')
                ORDER BY g.message_id,p.created_at DESC LIMIT %s""",(project_id,channel,chat_id,user_id,min(max(limit,1),25)))).fetchall()
            logical={row['logical_task_id'] for row in projections if row['logical_task_id']}
            replaced={row['run_id'] for row in projections}
            rows=[row for row in rows if row['run_id'] not in replaced and not (str(row['run_id']) in logical and not row.get('question_content'))]
            for row in projections:
                message=await (await connection.execute('SELECT * FROM assistant.messages WHERE message_id=%s',(row['waiting_question_message_id'],))).fetchone()
                hydrated=await QuestionGroupService(self.settings,self).hydrate(connection,message)
                rows.append({**row,'question_content':hydrated['content_json']})
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
                        'queued','running','waiting_user','waiting_supervisor','blocked','reconciling'
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
            await self._require_active_run(connection, run_id)
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
            projection=await (await connection.execute("""SELECT p.* FROM assistant.messages p
                JOIN assistant.messages g ON g.message_id=(p.content_json#>>'{question_projection,group_ref,id}')::uuid
                WHERE p.run_id=%s AND p.project_id=%s AND p.channel=%s AND p.chat_id=%s
                AND p.user_id IS NOT DISTINCT FROM %s AND p.direction='outbound' AND p.message_type='question'
                AND p.content_json->>'schema_version'='question-group-projection.v1'
                AND g.content_json#>>'{group,status}'='open' ORDER BY p.created_at DESC LIMIT 1""",
                (run_id,project_id,channel,chat_id,user_id))).fetchone()
            if projection:
                if run.get('cancel_requested_at') or run['status'] in {'succeeded','failed','cancelled','dead_letter'}:
                    raise PermissionError('Question owner is no longer active')
                from assistant_kernel.services.question_groups import QuestionGroupService
                restored=await QuestionGroupService(self.settings,self).restore_view_locked(connection,owner=run,projection=projection,
                    source_message_id=source_message_id,key=idempotency_key)
                await connection.commit()
                return {'run':run,'message':restored,'blocked':False,'duplicate':False}
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
            return self._filter_revoked_context(list(await cursor.fetchall()))

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
        from assistant_kernel.services.kernel_management import require_active_identity
        require_active_identity(project_id, agent_key)
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
        from assistant_kernel.services.kernel_management import require_active_identity
        require_active_identity(project_id, agent_key)
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
        max_attempts: int | None = None,
    ) -> dict[str, Any]:
        async with self.database.connection() as connection:
            row = await self.create_run_locked(connection, project_id=project_id, agent_key=agent_key,
                run_type=run_type, request=request, stage=stage, parent_run_id=parent_run_id,
                trigger_message_id=trigger_message_id, executor=executor, idempotency_key=idempotency_key,
                max_attempts=max_attempts)
            await connection.commit()
            return row

    async def create_run_locked(
        self, connection, *, project_id: str, agent_key: str, run_type: str,
        request: dict[str, Any], stage: str | None = None,
        parent_run_id: str | None = None, trigger_message_id: str | None = None,
        executor: dict[str, Any] | None = None, idempotency_key: str | None = None,
        max_attempts: int | None = None,
    ) -> dict[str, Any]:
        from assistant_kernel.services.kernel_management import require_active_identity
        require_active_identity(project_id, agent_key)
        if max_attempts is not None and (isinstance(max_attempts,bool) or not isinstance(max_attempts,int) or max_attempts<1):
            raise ValueError('max_attempts must be a positive fixed integer')
        if request.get("interaction_kind") == "child_task" or agent_key.endswith((":child", ":operator")):
            raise ValueError("child Run and Operator Agent are retired")
        if run_type == "stage":
            raise PermissionError("Stage execution is retired; use single-agent.v2")
        if run_type == "python":
            request = {"shared_write": False, **request}
        if parent_run_id:
            parent_run_id = str(uuid.UUID(str(parent_run_id)))
        if trigger_message_id:
            trigger_message_id = str(uuid.UUID(str(trigger_message_id)))
        run_scheduling(project_id,agent_key,run_type,request)
        if run_type not in {"chat", "python"}:
            raise ValueError("run_type must be chat, stage, or python")
        if run_type != "python" and not trigger_message_id:
            raise ValueError("chat and stage runs require trigger_message_id")
        run_id = str(uuid.uuid4())
        key = idempotency_key or f"run:{run_id}"
        resolved_executor = {
            **dict(executor or {}),
        }
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
        run_conversation_key = f"python:{project_id}:{run_id}" if run_type == "python" else (
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
            or str(row.get("parent_run_id") or "") != str(parent_run_id or "")
            or str(row.get("trigger_message_id") or "") != str(trigger_message_id or "")
            or {k:v for k,v in (row["request_json"] or {}).items()
                if k not in {"capacity_reservation", "capacity_wait"}} != json_compatible(request)
        ):
            raise ValueError("idempotency_key was reused for a different run")
        if max_attempts is not None and row['max_attempts']!=max_attempts:
            if row['status']!='queued' or row['attempt']!=0:raise ValueError('Retry policy differs from an already attempted binding')
            row=await (await connection.execute('UPDATE assistant.runs SET max_attempts=%s WHERE run_id=%s RETURNING *',
                (max_attempts,row['run_id']))).fetchone()
        row = await self._persist_scheduling(connection, row)
        return row

    async def start_task_from_claim(
        self, *, source_run_id: str, worker_instance_id: str, claim_token: str,
        goal: str, stages: list[str], change_class: str = "major",
        executor: dict[str, Any] | None = None,
        task_metadata: dict[str, Any] | None = None,
        supersede_run_id: str | None = None,
        supersede_reason: str | None = None,
    ) -> dict[str, Any]:
        raise PermissionError("Stage execution is retired; use single-agent.v2")

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
            row = await self._persist_scheduling(connection, row)
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
                    SELECT * FROM assistant.runs
                    WHERE run_id=%s AND worker_instance_id=%s AND claim_token=%s
                      AND status='running' AND cancel_requested_at IS NULL
                    FOR UPDATE
                    """,
                    (run_id, worker_instance_id, claim_token),
                )
            ).fetchone()
            if locked is None:
                raise RuntimeError("Task lease was lost before continuation")
            pending_inputs = await self._settle_turn_inputs(connection, locked)
            if pending_inputs:
                request_patch = {**request_patch,'task_phase':'working','pending_validation_supervisor_turn':None,'validation_supervisor_approved_turn':None,'audit_complete':False}
            prior_reply = await (await connection.execute(
                "SELECT 1 FROM assistant.messages WHERE direction='outbound' AND message_type='reply' AND reply_to_message_id=%s LIMIT 1",
                (source_message_id,),
            )).fetchone()
            reply_parent = None if prior_reply else source_message_id
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
                        user_id, reply_parent,
                        Jsonb({
                            "text": summary,
                            "source_message_id": source_message_id,
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
            updated = await self._persist_scheduling(connection, updated)
            if wait_for_supervisor and self._five_stage_task(updated):
                updated = await (await connection.execute(
                    "UPDATE assistant.runs SET request_json=request_json || %s WHERE run_id=%s RETURNING *",
                    (Jsonb({"pending_supervision_turn": int((updated.get("request_json") or {}).get("task_turn_count") or 0),
                            "pending_supervision_source_id": str(reply["message_id"])}), run_id),
                )).fetchone()
                await self._queue_supervision_locked(connection, updated, str(reply["message_id"]), reason="after_agent_turn")
            await connection.commit()
            return {"run": updated, "reply": reply}

    async def resume_supervision_degraded(
        self, *, task_id: str, turn_number: int, reason: str,
    ) -> dict[str, Any] | None:
        """Read compatibility for pre-migration Tasks; modern Tasks never fail open."""
        async with self.database.connection() as connection:
            task = await (await connection.execute(
                "SELECT * FROM assistant.runs WHERE run_id=%s FOR UPDATE", (task_id,),
            )).fetchone()
            if task and self._five_stage_task(task):
                raise PermissionError("five-stage Tasks require successful supervision")
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
        if any(d.get("executor") != "native_subagent" for d in delegations):
            raise ValueError("Kernel child Run is retired; only native_subagent delegation is supported")
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
            if task["status"] in {"running", "reconciling", "succeeded", "failed", "dead_letter", "cancelled"} or task.get("cancel_requested_at"):
                raise PermissionError("Supervisor can only change a Task at a settled safe boundary")
            request = dict(task.get("request_json") or {})
            if request.get('protocol_revision')=='1.2':
                raise PermissionError('v1.2 requires the typed Prompt compiler and specific confirmation; legacy Action Plans cannot dispatch')
            if request.get("task_phase") in {"publishing", "publication_reconciling"}:
                raise PermissionError("Publication must settle before a mainline change")
            if request.get("task_activation", "active") != "active":
                raise PermissionError("Supervisor cannot activate a suspended Task")
            if int(request.get("supervisor_context_epoch") or 0) != int(source_context_epoch):
                raise PermissionError("Supervisor Action Plan belongs to a stale context")
            current_turn = int(request.get("task_turn_count") or 0)
            if source_turn_number and current_turn != int(source_turn_number):
                raise PermissionError("Supervisor Action Plan belongs to a stale Turn")
            accepted = dict(request.get("accepted_documents") or {})
            accepted["current_action_plan"] = json_compatible(action_ref)
            brief_ref = result.get("brief_ref")
            if isinstance(brief_ref, dict):
                from assistant_kernel.services.document_runtime import TaskDocumentService
                brief = TaskDocumentService(self.settings).read(str(task["project_id"]), brief_ref)
                if brief.get("schema_version") != "supervisor-brief.v1" or brief.get("task_id") != task_id:
                    raise PermissionError("Supervisor Brief belongs to another Task or schema")
                accepted["supervisor_brief"] = json_compatible(brief_ref)
            active_delegations = list(accepted.get("active_delegations") or [])
            for delegation in delegations:
                ref = delegation.get("request_ref")
                if ref and ref not in active_delegations:
                    active_delegations.append(json_compatible(ref))
            accepted["active_delegations"] = active_delegations
            state_revision = int(request.get("state_revision") or 0) + 1
            task_status = str(task["status"])
            if task_status == "waiting_supervisor":
                task_status = "waiting_user" if confirmation_class == "user_required" else "queued"
            request_patch = {
                "accepted_documents": accepted,
                "state_revision": state_revision,
                "degraded_supervision": False,
                "pending_supervision_turn": None,
                "supervision_failure": None,
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
            from assistant_kernel.services.agent_results import normalize_phase
            current_mode = normalize_phase(current_mode)
            requested_mode = normalize_phase(requested_mode)
            allowed_modes = {"requirements", "design", "build", "audit", "operate"}
            if requested_mode not in allowed_modes:
                raise ValueError("Supervisor selected an unknown Work Mode")
            if requested_mode == "build" and not request.get("requirements_approval"):
                requested_mode = current_mode
            if requested_mode == "design" and not request.get("requirements_approval"):
                requested_mode = "requirements"
            if requested_mode == "build" and self._five_stage_task(task) and not request.get("design_approval"):
                requested_mode = "design" if request.get("requirements_approval") else "requirements"
            if requested_mode == "audit" and not request.get("validation_artifact"):
                requested_mode = current_mode
            if requested_mode == "audit" and request.get("design_required") and request.get("validation_design_sha256") != (request.get("design_approval") or {}).get("approved_document_sha256"):
                requested_mode = "build"
            if requested_mode == "operate" and not (
                request.get("publication_state") == "published"
                or request.get("task_phase") == "published"
            ):
                requested_mode = current_mode
            request_patch.update({
                "work_mode": requested_mode,
                "current_stage": requested_mode,
                "execution_plan_variant": requested_mode,
                "audit_mode": requested_mode == "audit",
            })
            selected = result.get("selected_skills") or (
                result.get("decision", {}).get("selected_skills")
                if isinstance(result.get("decision"), dict) else []
            ) or []
            if selected and self._five_stage_task(task):
                from assistant_kernel.services.agent_files import AgentFileLoader
                loader = AgentFileLoader(self.settings)
                profile_path = ProjectRuntimeManager(self.settings.data_root).describe(str(task["project_id"])).vault_root / "agents" / "pm" / "profiles" / f"{requested_mode}.yaml"
                loader.apply_phase_profile(loader.load(str(task["project_id"]), str(task["agent_key"])),
                                           yaml.safe_load(profile_path.read_text(encoding="utf-8")) or {},
                                           requested_skills=selected)
            request_patch["selected_skills"] = selected
            if request.get("supervision_autoretry_paused"):
                task_status = str(task["status"])
            if requested_mode != current_mode:
                request_patch["force_session_rotation"] = {"reason": "phase_changed", "from": current_mode, "to": requested_mode}
                request_patch["pending_auto_publish"] = None
            if session_action == "rotate" and requested_mode == current_mode:
                request_patch["force_session_rotation"] = {
                    "reason": "supervisor_action_plan",
                    "source_action_plan_ref": json_compatible(action_ref),
                }
            elif session_action not in {"keep", "rotate"}:
                raise ValueError("Supervisor selected an unknown Session action")
            updated_task = await (await connection.execute(
                """
                UPDATE assistant.runs
                SET status=%s,available_at=CASE WHEN %s='queued' THEN now() ELSE available_at END,
                    request_json=COALESCE(request_json,'{}'::jsonb) || %s,
                    error_json=NULL,
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
            if confirmation_class == "safe" and not request.get("supervision_autoretry_paused"):
                if request.get("pending_auto_publish") and requested_mode == "audit" and not task.get("waiting_question_message_id"):
                    publication = await self._queue_approved_publication_locked(
                        connection, updated_task, str(supervisor["trigger_message_id"]), authorization_kind="design",
                    )
                    updated_task = publication["task"]
                elif request.get("pending_task_completion") and requested_mode == "operate" and not task.get("waiting_question_message_id"):
                    if not accepted.get("operation_report"):
                        raise ValueError("Operation completion requires the accepted report")
                    unfinished = list(await (await connection.execute(
                        """SELECT run_id,status FROM assistant.runs WHERE parent_run_id=%s
                            AND request_json ? 'operation_action'
                            AND status NOT IN ('succeeded','cancelled')""", (task_id,),
                    )).fetchall())
                    if unfinished:
                        updated_task = await (await connection.execute(
                            """UPDATE assistant.runs SET status='waiting_supervisor',request_json=request_json || %s,
                                updated_at=now() WHERE run_id=%s RETURNING *""",
                            (Jsonb({"pending_task_completion": False,
                                    "pending_operation_runs": [str(row["run_id"]) for row in unfinished],
                                    "pm_instruction": "Wait for the registered action receipts; never repeat the effect to obtain a receipt."}), task_id),
                        )).fetchone()
                    else:
                        updated_task = await (await connection.execute(
                        """UPDATE assistant.runs SET status='succeeded',ended_at=now(),
                            request_json=request_json || %s,updated_at=now() WHERE run_id=%s RETURNING *""",
                        (Jsonb({"pending_task_completion": False, "task_phase": "completed",
                                "task_activation": "closed"}), task_id),
                        )).fetchone()
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
            updated_task = await self._persist_scheduling(connection, updated_task)
            await connection.commit()
            return {
                "task": updated_task, "supervisor_run": finished,
                "state_revision": state_revision,
            }



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
                    status=CASE WHEN status IN ('queued','waiting_user','waiting_supervisor','blocked')
                        THEN 'cancelled' ELSE status END,
                    cancel_requested_at=COALESCE(cancel_requested_at,now()),
                    ended_at=CASE WHEN status IN ('queued','waiting_user','waiting_supervisor','blocked')
                        THEN now() ELSE ended_at END,
                    updated_at=now()
                WHERE project_id=%s AND run_id<>%s
                  AND status IN ('queued','running','waiting_user','waiting_supervisor','blocked')
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
        from assistant_kernel.services.kernel_management import require_active_identity
        require_active_identity(target_project_id, target_agent_key or "project:"+target_project_id+":manager")
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
            target_run = await self._persist_scheduling(connection, target_run)
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

    async def _persist_scheduling(self, connection, row: dict[str, Any]) -> dict[str, Any]:
        row=await self._admit_v12_foreground_locked(connection,row)
        from assistant_kernel.services.runtime_admission import declared_resource_keys,finite_demand,RuntimeAdmission
        declared_resource_keys(row['request_json'] or {})
        if row['run_type']=='python' and 'runtime_resources' in (row['request_json'] or {}):
            if not isinstance(row['request_json']['runtime_resources'],dict):raise ValueError('runtime_resources must be an object')
            _,defaults,_=await RuntimeAdmission(self.settings).configuration_locked(connection)
            finite_demand(row['request_json'],defaults)
        queue, mutex = run_scheduling(str(row['project_id']),str(row['agent_key']),str(row['run_type']),dict(row['request_json'] or {}), engineering_main=self._single_agent_project(str(row['project_id'])) or (row['request_json'] or {}).get('task_protocol') == 'single-agent.v2')
        row = await (await connection.execute(
            "UPDATE assistant.runs SET queue_kind=%s,mutex_key=%s WHERE run_id=%s RETURNING *",
            (queue,mutex,row['run_id']))).fetchone()
        if mutex == f"engineering:{row['project_id']}" and str(row["agent_key"]).endswith(":manager"):
            if self._five_stage_task(row):
                row = await (await connection.execute(
                    "UPDATE assistant.runs SET request_json=request_json || %s WHERE run_id=%s RETURNING *",
                    (Jsonb({"design_required": True, "phase_protocol": "five-stage.v1"}), row["run_id"]),
                )).fetchone()
            if (row.get("request_json") or {}).get("task_activation", "active") == "active" and row["status"] not in {"succeeded", "failed", "dead_letter", "cancelled"}:
                await connection.execute(
                    """INSERT INTO assistant.kernel_meta(key,value) VALUES (%s,%s)
                       ON CONFLICT(key) DO UPDATE SET value=assistant.kernel_meta.value || EXCLUDED.value,updated_at=now()""",
                    (f"project-runtime:{row['project_id']}", Jsonb({"active_task_id": str(row["run_id"])})),
                )
            elif row["status"] in {"succeeded", "cancelled"} or (row.get("request_json") or {}).get("task_activation") == "closed":
                await connection.execute(
                    """UPDATE assistant.kernel_meta SET value=value || '{"active_task_id":null}'::jsonb,updated_at=now()
                       WHERE key=%s AND value->>'active_task_id'=%s""",
                    (f"project-runtime:{row['project_id']}", str(row["run_id"])),
                )
        return row

    async def _admit_v12_foreground_locked(self,connection,row):
        """A complete adopted protocol starts with supervision, never a naked PM call."""
        if (row['run_type']!='chat' or not str(row['agent_key']).endswith(':manager')
                or row['status']!='queued' or (row.get('request_json') or {}).get('protocol_revision')=='1.2'):
            return row
        from assistant_kernel.services.published_agents import PublishedAgentService
        admitted=await PublishedAgentService(self.settings).foreground_package_locked(connection,row)
        if not admitted:return row
        ref,_=admitted;request=dict(row.get('request_json') or {});executor=dict(row.get('executor_json') or {})
        source_id=str(request.get('answer_message_id') or request.get('source_message_id') or row['trigger_message_id'])
        source=await (await connection.execute('SELECT * FROM assistant.messages WHERE message_id=%s',(source_id,))).fetchone()
        if not source or source['project_id']!=row['project_id'] or not source.get('user_id'):
            raise PermissionError('New foreground protocol needs its actual identified user source')
        from assistant_kernel.services.agent_results import normalize_phase
        mode=normalize_phase(request.get('current_stage') or request.get('work_mode') or 'requirements')
        if mode not in {'requirements','design','build','audit','operate'}:mode='requirements'
        request.update(protocol_revision='1.2',task_protocol='single-agent.v2',phase_protocol='five-stage.v1',
            design_required=True,current_stage=mode,work_mode=mode,pending_runtime_package_ref=ref)
        request.setdefault('state_revision',0);request.setdefault('supervisor_context_epoch',0)
        request.setdefault('task_turn_count',0);request.setdefault('task_goal',str((source['content_json'] or {}).get('text') or ''))
        request.setdefault('accepted_documents',{})
        # New Tasks inherit the already-approved Project publication. Existing
        # legacy Tasks keep their old execution binding until an exact Prompt is confirmed.
        if not executor.get('project_policy_ref'):executor['task_runtime_package_ref']=ref
        row=await (await connection.execute("UPDATE assistant.runs SET status='waiting_supervisor',request_json=%s,executor_json=%s WHERE run_id=%s RETURNING *",
            (Jsonb(request),Jsonb(executor),row['run_id']))).fetchone()
        await self._queue_supervision_locked(connection,row,source_id,reason='prepare_confirm_dispatch')
        return row

    async def pin_turn_inputs(self, run_id: str, worker_instance_id: str, claim_token: str) -> list[dict[str, Any]]:
        async with self.database.connection() as connection:
            row = await (await connection.execute("SELECT * FROM assistant.runs WHERE run_id=%s AND worker_instance_id=%s AND claim_token=%s AND status='running' AND cancel_requested_at IS NULL FOR UPDATE",(run_id,worker_instance_id,claim_token))).fetchone()
            if row is None:
                raise PermissionError("Turn input lease was lost")
            inputs = list(await (await connection.execute("SELECT * FROM assistant.messages WHERE (run_id=%s OR content_json->>'task_id'=%s) AND direction='inbound' AND content_json->>'task_input_status'='pending' ORDER BY created_at,message_id LIMIT 100",(run_id,run_id))).fetchall())
            await connection.execute("UPDATE assistant.runs SET executor_json=executor_json || %s WHERE run_id=%s",(Jsonb({'turn_input_message_ids':[str(m['message_id']) for m in inputs]}),run_id))
            await connection.commit()
            return self._filter_revoked_context(inputs)

    async def clear_publisher_process(self, run_id: str, pid: int, start_ticks: int | None) -> None:
        """A finished thread must not retain the reusable worker process as its own."""
        async with self.database.connection() as connection:
            await connection.execute("""UPDATE assistant.runs SET process_pid=NULL,process_start_ticks=NULL
                WHERE run_id=%s AND run_type='publish' AND process_pid=%s
                AND process_start_ticks IS NOT DISTINCT FROM %s""", (run_id, pid, start_ticks))
            await connection.commit()

    async def _settle_turn_inputs(self, connection, row: dict[str, Any]) -> bool:
        ids = list((row.get('executor_json') or {}).get('turn_input_message_ids') or [])
        await connection.execute("""UPDATE assistant.messages SET content_json=content_json || %s,updated_at=now()
            WHERE (run_id=%s OR content_json->>'task_id'=%s) AND message_id=ANY(%s::uuid[]) AND direction='inbound'
              AND content_json->>'task_input_status'='pending'""",(Jsonb({'task_input_status':'consumed','consumed_attempt':int(row.get('attempt') or 0)}),row['run_id'],str(row['run_id']),ids))
        return bool(await (await connection.execute("SELECT 1 FROM assistant.messages WHERE (run_id=%s OR content_json->>'task_id'=%s) AND content_json->>'task_input_status'='pending' LIMIT 1",(row['run_id'],str(row['run_id'])))).fetchone())

    async def has_late_turn_inputs(self, run_id: str) -> bool:
        async with self.database.connection() as connection:
            return bool(await (await connection.execute("""SELECT 1 FROM assistant.messages m
                JOIN assistant.runs r ON (r.run_id=m.run_id OR m.content_json->>'task_id'=r.run_id::text) WHERE r.run_id=%s
                AND m.content_json->>'task_input_status'='pending'
                AND NOT EXISTS (SELECT 1 FROM jsonb_array_elements_text(
                    COALESCE(r.executor_json->'turn_input_message_ids','[]'::jsonb)) pinned(id)
                    WHERE pinned.id=m.message_id::text) LIMIT 1""",(run_id,))).fetchone())

    async def _requeue_pending_inputs(self, connection, row: dict[str, Any]) -> dict[str, Any]:
        task = await (await connection.execute('SELECT * FROM assistant.runs WHERE run_id=%s FOR UPDATE',
            (row['run_id'],))).fetchone()
        if task and (task['request_json'] or {}).get('protocol_revision') == '1.2':
            source = await (await connection.execute("""SELECT message_id FROM assistant.messages
                WHERE (run_id=%s OR content_json->>'task_id'=%s) AND direction='inbound'
                  AND content_json->>'task_input_status'='pending'
                ORDER BY created_at DESC,message_id DESC LIMIT 1""",
                (task['run_id'], str(task['run_id'])))).fetchone()
            if not source:
                raise RuntimeError('Pending-input continuation has no durable source')
            return await self._supervise_answer_locked(connection, task, str(source['message_id']))
        return await (await connection.execute("""UPDATE assistant.runs SET status='queued',available_at=now(),
            worker_id=NULL,worker_instance_id=NULL,claim_token=NULL,lease_until=NULL,
            process_pid=NULL,process_start_ticks=NULL,ended_at=NULL,waiting_question_message_id=NULL,
            interaction_seq=interaction_seq+1,request_json=request_json || %s,
            executor_json=executor_json - 'resolved_plan_uri' - 'resolved_plan_sha256' - 'prompt_bundle_uri' - 'prompt_bundle_sha256',updated_at=now()
            WHERE run_id=%s RETURNING *""",(Jsonb({'task_phase':'working','approval_kind':None,'audit_complete':False,'pending_validation_supervisor_turn':None,'validation_supervisor_approved_turn':None}),row['run_id']))).fetchone()

    async def claim_run(self, worker_id: str, worker_instance_id: str, lease_seconds: int,
                        *, queue_kind: str = "interactive") -> dict[str, Any] | None:
        if queue_kind not in {"interactive", "runtime"}:
            raise ValueError("invalid worker queue")
        try:
            async with self.database.connection() as connection:
                from assistant_kernel.services.runtime_admission import RuntimeAdmission
                admission=RuntimeAdmission(self.settings)
                await admission.lock(connection)
                limits,defaults,config_ref=await admission.configuration_locked(connection)
                occupied=await admission.occupied_locked(connection)
                candidates=await (await connection.execute("""
                        SELECT r.* FROM assistant.runs r
                        WHERE r.status='queued' AND r.available_at<=now() AND r.queue_kind=%s
                          AND (r.run_type<>'chat' OR r.agent_key NOT LIKE '%%:manager'
                            OR COALESCE(r.request_json->>'task_activation','active')='active')
                          AND r.run_type<>'stage'
                          AND NOT EXISTS (SELECT 1 FROM assistant.runs a
                            WHERE a.conversation_key=r.conversation_key AND a.status IN ('running','reconciling'))
                          AND NOT EXISTS (SELECT 1 FROM assistant.runs a
                            WHERE r.mutex_key IS NOT NULL AND a.mutex_key=r.mutex_key
                              AND a.status IN ('running','reconciling'))
                          AND (r.run_type='python' OR NOT EXISTS (SELECT 1 FROM assistant.runs p
                            WHERE p.run_id=r.parent_run_id AND p.status IN ('running','reconciling')))
                        ORDER BY (SELECT count(*) FROM assistant.runs load WHERE load.project_id=r.project_id
                            AND load.status IN ('running','reconciling')),r.created_at,r.run_id
                        FOR UPDATE OF r SKIP LOCKED
                    """,(queue_kind,))).fetchall()
                row=None
                for candidate in candidates:
                    from assistant_kernel.services.kernel_management import retired_identity
                    if retired_identity(candidate["project_id"], candidate["agent_key"]):
                        continue
                    from assistant_kernel.services.schedule_support import authorize_claim
                    if not await authorize_claim(connection,candidate):continue
                    reservation=await admission.finite_reservation_locked(connection,candidate,occupied,limits,defaults,config_ref)
                    if reservation is None:continue
                    row=await (await connection.execute("""UPDATE assistant.runs SET status='running',attempt=attempt+1,
                      worker_id=%s,worker_instance_id=%s,claim_token=%s,
                      process_pid=NULL,process_start_ticks=NULL,
                      lease_until=now()+(%s*interval '1 second'),
                      started_at=COALESCE(started_at,now()),updated_at=now(),request_json=request_json || %s
                      WHERE run_id=%s AND status='queued' RETURNING *""",
                      (worker_id,worker_instance_id,str(uuid.uuid4()),lease_seconds,Jsonb({'capacity_reservation':reservation,
                        **({'schedule_dispatch_permit':candidate['request_json']['schedule_dispatch_permit']}
                           if candidate['request_json'].get('schedule_dispatch_permit') else {})}),candidate['run_id']))).fetchone()
                    break
                await connection.commit()
                return row
        except UniqueViolation as exc:
            if exc.diag.constraint_name not in {"runs_one_active_conversation", "runs_one_active_mutex"}:
                raise
            return None


    async def active_execution_blocker(self, project_id: str, target_run_id: str | None = None) -> dict[str, Any] | None:
        async with self.database.connection() as connection:
            return await (await connection.execute("""
                SELECT a.* FROM assistant.runs a
                WHERE a.status IN ('running','reconciling') AND (
                  (%s::uuid IS NULL AND a.mutex_key=%s) OR EXISTS (
                    SELECT 1 FROM assistant.runs target WHERE target.run_id=%s::uuid
                      AND a.run_id<>target.run_id AND (
                        a.conversation_key=target.conversation_key OR
                        (target.mutex_key IS NOT NULL AND a.mutex_key=target.mutex_key))))
                ORDER BY a.created_at,a.run_id LIMIT 1
                """,(target_run_id,f"engineering:{project_id}",target_run_id))).fetchone()


    async def current_project_task(self, project_id: str) -> dict[str, Any] | None:
        async with self.database.connection() as connection:
            return await (await connection.execute("""
                SELECT * FROM assistant.runs WHERE project_id=%s AND run_type='chat'
                  AND agent_key=%s AND mutex_key=%s
                  AND COALESCE(request_json->>'task_activation','active')='active'
                  AND status IN ('queued','running','waiting_user','waiting_supervisor','blocked','reconciling')
                ORDER BY created_at,run_id LIMIT 1
                """,(project_id,f"project:{project_id}:manager",f"engineering:{project_id}"))).fetchone()

    def _five_stage_project(self, project_id: str, task: dict[str, Any] | None = None) -> bool:
        from assistant_kernel.services.published_agents import PublishedAgentService
        project=PublishedAgentService(self.settings).project_policy(project_id,task=task)
        return bool(project.get("phase_protocol") == "five-stage.v1"
                    or (project.get("single_agent") or {}).get("design_required")
                    or (project.get("work_modes") or {}).get("design"))

    def _five_stage_task(self, task: dict[str, Any]) -> bool:
        request = task.get("request_json") or {}
        return bool(request.get('protocol_revision')=='1.2' or request.get("design_required") or request.get("phase_protocol") == "five-stage.v1"
                    or self._five_stage_project(str(task["project_id"]),task))

    async def list_pending_task_inputs(self, task_id: str) -> list[dict[str, Any]]:
        async with self.database.connection() as connection:
            rows = list(await (await connection.execute(
                """SELECT * FROM assistant.messages WHERE direction='inbound'
                    AND (run_id=%s OR content_json->>'task_id'=%s)
                    AND content_json->>'task_input_status'='pending'
                    ORDER BY created_at,message_id LIMIT 100""", (task_id, task_id),
            )).fetchall())
            return self._filter_revoked_context(rows)

    async def _queue_supervision_locked(self, connection, task: dict[str, Any], source_message_id: str, *, reason: str) -> dict[str, Any] | None:
        """Queue under the Task transaction; coalesce an already-open evaluation."""
        request = dict(task.get("request_json") or {})
        project_id = str(task["project_id"])
        source = await (await connection.execute(
            "SELECT * FROM assistant.messages WHERE message_id=%s", (source_message_id,),
        )).fetchone()
        if source is None:
            source = await (await connection.execute(
                "SELECT * FROM assistant.messages WHERE message_id=%s", (task["trigger_message_id"],),
            )).fetchone()
        if source is None:
            raise RuntimeError("Supervisor has no durable source Message")
        epoch = int(request.get("supervisor_context_epoch") or 0)
        turn = int(request.get("task_turn_count") or 0)
        agent = f"project:{project_id}:supervisor"
        key = "supervisor-task:" + canonical_sha256([project_id, agent, str(task["run_id"])])
        body = {
            "interaction_kind": "supervisor_consultation", "schema_version": "supervisor-consultation.v3",
            "task_id": str(task["run_id"]), "trigger": reason,
            "turn_number": turn, "source_turn_number": turn, "source_context_epoch": epoch,
            "source_message_id": str(source["message_id"]), "source_actor_type": "kernel",
            "source_agent_key": str(task["agent_key"]), "source_run_id": str(task["run_id"]),
            "source_interface_role": "pm", "source_channel": str(source["channel"]),
            "source_chat_id": str(source["chat_id"]), "channel": str(source["channel"]),
            "chat_id": str(source["chat_id"]), "user_id": source.get("user_id"),
            "agent_turn_report": {
                "outcome": (task.get("result_json") or {}).get("outcome"),
                "summary": (task.get("result_json") or {}).get("summary"),
                "usage": (task.get("result_json") or {}).get("token_usage") or {},
            },
        }
        existing = await (await connection.execute(
            """SELECT * FROM assistant.runs WHERE conversation_key=%s
                AND status IN ('queued','running','waiting_user','reconciling')
                ORDER BY created_at DESC LIMIT 1 FOR UPDATE""", (key,),
        )).fetchone()
        if existing:
            if existing["status"] == "queued" and (
                int((existing.get("request_json") or {}).get("source_context_epoch") or 0) < epoch
                or int((existing.get("request_json") or {}).get("source_turn_number") or 0) < turn
            ):
                existing = await (await connection.execute(
                    """UPDATE assistant.runs SET
                        request_json=(request_json-'turn_handoff_ref'-'turn_handoff_sha256') || %s,
                        trigger_message_id=%s,updated_at=now() WHERE run_id=%s RETURNING *""",
                    (Jsonb(body), source["message_id"], existing["run_id"]),
                )).fetchone()
            return existing
        supervision_key=f"supervisor-state:{task['run_id']}:{turn}:{epoch}"
        if request.get('protocol_revision')=='1.2':
            body['protocol_revision']='1.2'
            body['source_state_revision']=int(request.get('state_revision') or 0)
            supervision_key+=':'+str(body['source_state_revision'])+':'+str(source['message_id'])
        row = await (await connection.execute(
            """INSERT INTO assistant.runs(
                run_id,project_id,agent_key,trigger_message_id,parent_run_id,run_type,
                status,conversation_key,request_json,executor_json,idempotency_key,max_attempts)
                VALUES (%s,%s,%s,%s,%s,'chat','queued',%s,%s,'{}'::jsonb,%s,4)
                ON CONFLICT DO NOTHING RETURNING *""",
            (str(uuid.uuid4()), project_id, agent, source["message_id"], task["run_id"], key,
             Jsonb(body), supervision_key),
        )).fetchone()
        if row is not None:
            return await self._persist_scheduling(connection, row)
        existing = await (await connection.execute(
            """SELECT * FROM assistant.runs WHERE conversation_key=%s
                AND status IN ('queued','running','waiting_user','reconciling')
                ORDER BY created_at DESC LIMIT 1 FOR UPDATE""", (key,),
        )).fetchone()
        if existing and existing["status"] == "queued" and (
            int((existing.get("request_json") or {}).get("source_context_epoch") or 0) < epoch
            or int((existing.get("request_json") or {}).get("source_turn_number") or 0) < turn
        ):
            existing = await (await connection.execute(
                """UPDATE assistant.runs SET
                    request_json=(request_json-'turn_handoff_ref'-'turn_handoff_sha256') || %s,
                    trigger_message_id=%s,updated_at=now() WHERE run_id=%s RETURNING *""",
                (Jsonb(body), source["message_id"], existing["run_id"]),
            )).fetchone()
        return existing

    async def attach_supervision_handoff(self, task_id: str, request: dict[str, Any]) -> dict[str, Any] | None:
        async with self.database.connection() as connection:
            task = await (await connection.execute("SELECT * FROM assistant.runs WHERE run_id=%s FOR UPDATE", (task_id,),)).fetchone()
            if not task:
                return None
            row = await self._queue_supervision_locked(
                connection, task, str(request.get("interaction_head", {}).get("pm_reply_message_id") or task["trigger_message_id"]),
                reason="after_agent_turn",
            )
            if row and row["status"] == "queued":
                row = await (await connection.execute(
                    "UPDATE assistant.runs SET request_json=request_json || %s WHERE run_id=%s RETURNING *",
                    (Jsonb(json_compatible(request)), row["run_id"]),
                )).fetchone()
            await connection.commit()
            return row

    async def reconcile_pending_supervision(self) -> int:
        """Fill interrupted enqueue gaps without creating a recovery subsystem."""
        async with self.database.connection() as connection:
            failures = list(await (await connection.execute(
                """SELECT run_id FROM assistant.runs
                   WHERE status IN ('waiting_supervisor','reconciling')
                   AND agent_key LIKE '%%:manager' AND run_type='chat'
                   AND COALESCE((request_json->>'design_required')::boolean,false)
                   AND error_json IS NOT NULL AND cancel_requested_at IS NULL
                   AND COALESCE(request_json->>'failure_settlement_key','') <>
                     'failed-attempt:' || run_id::text || ':' || attempt::text
                   LIMIT 30""",
            )).fetchall())
        for failure in failures:
            await self.settle_failed_pm_turn(str(failure["run_id"]), "PM failure recovered from durable Run facts")
        async with self.database.connection() as connection:
            tasks = list(await (await connection.execute(
                """SELECT * FROM assistant.runs WHERE status='waiting_supervisor'
                    AND agent_key LIKE '%%:manager' AND request_json->>'task_protocol'='single-agent.v2'
                    AND request_json->'pending_operation_runs' IS NULL
                    AND COALESCE(request_json->>'supervision_failure','')=''
                    AND COALESCE(request_json->>'task_activation','active')='active'
                    AND cancel_requested_at IS NULL LIMIT 30 FOR UPDATE SKIP LOCKED""",
            )).fetchall())
            for task in tasks:
                request = dict(task.get("request_json") or {})
                await self._queue_supervision_locked(
                    connection, task, str(request.get("pending_supervision_source_id") or task["trigger_message_id"]),
                    reason="recover_pending_supervision",
                )
            await connection.commit()
            return len(tasks)

    async def _queue_approved_publication_locked(self, connection, task, source_message_id, *, authorization_kind):
        request = dict(task.get("request_json") or {})
        if not request.get("audit_complete") or request.get("reviewed_bundle_sha256") != (request.get("sealed_candidate_bundle") or {}).get("bundle_sha256"):
            raise ValueError("Publication requires Review of this sealed bundle")
        if request.get("design_required") and request.get("reviewed_design_sha256") != (request.get("design_approval") or {}).get("approved_document_sha256"):
            raise ValueError("Publication Review does not cover the current Design")
        if authorization_kind == "design":
            if request.get("review_requires_final_approval"):
                raise PermissionError("Review requires separate final publication approval")
            from assistant_kernel.services.document_runtime import TaskDocumentService
            design_ref = dict((request.get("accepted_documents") or {}).get("solution_design") or {})
            approval = dict(request.get("design_approval") or {})
            if approval.get("approved_document_sha256") != design_ref.get("sha256") or not design_ref:
                raise PermissionError("Automatic publication has no current Design approval")
            design = TaskDocumentService(self.settings).read(str(task["project_id"]), design_ref)["payload"]
            if design.get("risk_class") != "low" or design.get("release_policy") != "automatic_low_risk" or str(task["project_id"]) in {"kernel", "butler-maintenance"} or request.get("capability_change"):
                raise PermissionError("This change requires final publication approval")
            from assistant_kernel.services.agent_results import publication_requires_final_approval
            from assistant_kernel.services.workspace_scope import WorkspaceScopeService
            scope = WorkspaceScopeService(self.settings)
            roots = scope.resolve(str(task["project_id"]), str(task["run_id"]), "implementation")
            sealed = dict(request.get("sealed_candidate_bundle") or {})
            if not sealed.get("uri"):
                raise PermissionError("Automatic publication requires the reviewed bundle")
            prefix = f"vault://{task['project_id']}/"
            if not str(sealed["uri"]).startswith(prefix):
                raise PermissionError("Reviewed bundle belongs to another Project")
            path = ProjectRuntimeManager(self.settings.data_root).resolve_vault_file(str(task["project_id"]), sealed["uri"][len(prefix):])
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != sealed.get("sha256"):
                raise PermissionError("Reviewed bundle file changed")
            bundle = json.loads(raw)
            targets = {item['target_uri'] if isinstance(item,dict) else item for item in design.get('publish_targets') or []}
            files = bundle.get("files") or []
            if not targets or any(item.get("target_uri") not in targets for item in files) or publication_requires_final_approval(self.settings, str(task["project_id"]), files, roots):
                raise PermissionError("This target requires final publication approval")
        manifest = request.get('candidate_manifest_ref') or next((item for item in reversed(task.get("artifact_refs") or [])
                         if isinstance(item, dict) and (item.get("kind") == "candidate_manifest"
                         or str(item.get("uri") or "").endswith("/candidate/manifest.json"))), None)
        if not manifest:
            raise ValueError("Publication requires a candidate manifest")
        publish_request = {
            **request, "candidate_manifest_uri": manifest["uri"],
            "candidate_bundle_uri": (request.get("sealed_candidate_bundle") or {}).get("uri"),
            "candidate_bundle_file_sha256": (request.get("sealed_candidate_bundle") or {}).get("sha256"),
            "implementation_run_id": str(task["run_id"]), "review_run_id": str(task["run_id"]),
            "approval_message_id": (request.get("design_approval") or {}).get("message_id") if authorization_kind=='design' else source_message_id,
            "publication_authorization_kind": authorization_kind,
            "approved_artifacts": list(task.get("artifact_refs") or []),
        }
        publication = await (await connection.execute(
            """INSERT INTO assistant.runs(run_id,project_id,agent_key,trigger_message_id,parent_run_id,
                run_type,stage,status,conversation_key,request_json,executor_json,idempotency_key)
                VALUES (%s,%s,%s,%s,%s,'publish','publish','queued',%s,%s,'{}'::jsonb,%s)
                ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL
                DO UPDATE SET idempotency_key=EXCLUDED.idempotency_key RETURNING *""",
            (str(uuid.uuid4()), task["project_id"], f"project:{task['project_id']}:publisher",
             source_message_id, task["run_id"], f"publish:{task['project_id']}:files", Jsonb(publish_request),
             f"design-publish:{task['run_id']}:{(request.get('sealed_candidate_bundle') or {}).get('bundle_sha256')}"),
        )).fetchone()
        publication = await self._persist_scheduling(connection, publication)
        task = await (await connection.execute(
            """UPDATE assistant.runs SET status='waiting_user',request_json=request_json || %s,
                updated_at=now() WHERE run_id=%s RETURNING *""",
            (Jsonb({"task_phase": "publishing", "publication_state": "queued",
                    "pending_auto_publish": False, "publish_run_id": str(publication["run_id"])}), task["run_id"]),
        )).fetchone()
        return {"task": task, "publication_run": publication}

    async def _supervise_answer_locked(self, connection, run: dict[str, Any], source_message_id: str) -> dict[str, Any]:
        """Accept the answer without granting permission to start the next PM Turn."""
        request = dict(run.get("request_json") or {})
        owner_is_supervisor = request.get("interaction_kind") == "supervisor_consultation"
        if owner_is_supervisor and not request.get("task_id"):
            return await (await connection.execute(
                "UPDATE assistant.runs SET max_attempts=attempt+4 WHERE run_id=%s RETURNING *",
                (run["run_id"],),
            )).fetchone()
        root_id = str(request.get("task_id") or run["run_id"])
        root = await (await connection.execute(
            "SELECT * FROM assistant.runs WHERE run_id=%s FOR UPDATE", (root_id,),
        )).fetchone()
        if root is not None:root=await self._admit_v12_foreground_locked(connection,root)
        if root is None or not self._five_stage_task(root):
            return run
        if root['status'] in {'succeeded','failed','cancelled','dead_letter'} or root.get('cancel_requested_at'):
            return run # Preserve the input; a correction cannot resurrect a closed Task.
        await connection.execute(
            "UPDATE assistant.messages SET content_json=content_json || %s WHERE message_id=%s",
            (Jsonb({"task_id": root_id, "task_input_status": "pending"}), source_message_id),
        )
        seen = await (await connection.execute(
            "SELECT content_json FROM assistant.messages WHERE idempotency_key=%s",
            (f"supervisor-context-event:{root_id}:{canonical_sha256(source_message_id)}",),
        )).fetchone()
        # Text replies may already have advanced this exact physical input at
        # ingress. Final acceptance must not count the same Message twice.
        if seen is None:
            await self._advance_supervisor_context_epoch_locked(
                connection, task=root, reason="semantic_answer", source_id=source_message_id,
            )
        elif (seen["content_json"].get("task_id") != root_id
              or seen["content_json"].get("source_id") != source_message_id):
            raise ValueError("Answer context event belongs to another source")
        root = await (await connection.execute(
            """UPDATE assistant.runs SET status=CASE WHEN status IN ('running','reconciling') THEN status ELSE 'waiting_supervisor' END,
                request_json=request_json || %s,updated_at=now(),ended_at=NULL
                WHERE run_id=%s RETURNING *""",
            (Jsonb({"pending_supervision_turn": int((root["request_json"] or {}).get("task_turn_count") or 0),
                    "pending_supervision_source_id": source_message_id, "pending_interaction_ref": None}), root_id),
        )).fetchone()
        if root['status'] in {'running','reconciling'}:
            # New input is durable, but it must not revoke a live lease or
            # declare unresolved execution settled. Its Turn settlement queues supervision.
            return root
        if owner_is_supervisor:
            return await (await connection.execute(
                """UPDATE assistant.runs SET
                    request_json=(request_json-'turn_handoff_ref'-'turn_handoff_sha256') || %s,
                    max_attempts=attempt+4,updated_at=now() WHERE run_id=%s RETURNING *""",
                (Jsonb({"source_context_epoch": int(root["request_json"].get("supervisor_context_epoch") or 0),
                        "source_turn_number": int(root["request_json"].get("task_turn_count") or 0),
                        "turn_number": 0, "trigger": "user_consultation"}), run["run_id"]),
            )).fetchone()
        await self._queue_supervision_locked(connection, root, source_message_id, reason="user_answer")
        return root

    async def pause_supervision(self, *, task_id: str, turn_number: int, reason: str, retry_run_id: str | None = None, retry_at: str | None = None) -> dict[str, Any] | None:
        async with self.database.connection() as connection:
            task = await (await connection.execute(
                "SELECT * FROM assistant.runs WHERE run_id=%s FOR UPDATE", (task_id,),
            )).fetchone()
            if task is None or task["status"] in {"succeeded", "cancelled"} or (task.get("request_json") or {}).get("task_activation") == "suspended":
                return None
            if task["status"] != "running":
                task = await (await connection.execute(
                    """UPDATE assistant.runs SET
                        status=CASE WHEN waiting_question_message_id IS NOT NULL THEN 'waiting_user'
                            WHEN status='reconciling' THEN status ELSE 'waiting_supervisor' END,
                        request_json=request_json || %s,updated_at=now(),ended_at=NULL
                        WHERE run_id=%s RETURNING *""",
                    (Jsonb({"pending_supervision_turn": turn_number, "supervision_failure": str(reason),
                            "supervision_retry_run_id": retry_run_id, "supervision_retry_at": retry_at,
                            "degraded_supervision": False}), task_id),
                )).fetchone()
            source = await (await connection.execute(
                "SELECT * FROM assistant.messages WHERE message_id=%s", (task["trigger_message_id"],),
            )).fetchone()
            if source is None:
                return task
            content = {
                "schema_version": "supervision-recovery.v1", "interaction_kind": "supervision_recovery",
                "origin": "kernel", "context_visibility": "ui_only", "action_status": "waiting",
                "text": "PM主线已暂停，等待有效Supervisor评估。已批准后台程序按原政策运行。\n" + str(reason),
                "task_id": task_id, "supervisor_run_id": retry_run_id, "turn_number": turn_number,
                "phase": (task["request_json"] or {}).get("current_stage"),
                "context_epoch": int((task["request_json"] or {}).get("supervisor_context_epoch") or 0),
                "actions": [{"id": "retry", "label": "重试监督"}, {"id": "wait", "label": "停止自动重试"},
                            {"id": "cancel", "label": "取消主线"}],
            }
            card = await (await connection.execute(
                """INSERT INTO assistant.messages(message_id,project_id,run_id,channel,chat_id,user_id,
                    direction,message_type,reply_to_message_id,content_json,delivery_status,idempotency_key)
                    VALUES (%s,%s,%s,%s,%s,%s,'outbound','kernel_notice',%s,%s,'pending',%s)
                    ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL
                    DO UPDATE SET idempotency_key=EXCLUDED.idempotency_key RETURNING *""",
                (str(uuid.uuid4()), task["project_id"], task_id, source["channel"], source["chat_id"],
                 source.get("user_id"), source["message_id"], Jsonb(content),
                 f"supervision-recovery:{task_id}:{turn_number}:{retry_run_id or 'none'}:{content['context_epoch']}"),
            )).fetchone()
            await connection.commit()
            return {"task": task, "card": card}

    async def bind_supervisor_interaction(self, task_id: str, supervisor_run_id: str, message_id: str) -> None:
        async with self.database.connection() as connection:
            task = await (await connection.execute("SELECT * FROM assistant.runs WHERE run_id=%s FOR UPDATE", (task_id,),)).fetchone()
            message = await (await connection.execute(
                "SELECT * FROM assistant.messages WHERE message_id=%s AND run_id=%s", (message_id, supervisor_run_id),
            )).fetchone()
            if not task or not message or task["project_id"] != message["project_id"]:
                raise PermissionError("Supervisor interaction is not in this Task")
            if task.get("waiting_question_message_id") or (task.get("request_json") or {}).get("pending_interaction_ref"):
                raise ValueError("Task already has a business interaction")
            await connection.execute(
                """UPDATE assistant.runs SET status='waiting_user',request_json=request_json || %s,
                    updated_at=now() WHERE run_id=%s""",
                (Jsonb({"pending_interaction_ref": {"message_id": message_id, "owner_run_id": supervisor_run_id}}), task_id),
            )
            await connection.commit()

    async def defer_supervisor_questions(self, task_id: str, supervisor_run_id: str, questions: list[dict], decision_ref: Any = None) -> None:
        async with self.database.connection() as connection:
            await connection.execute(
                """UPDATE assistant.runs SET request_json=request_json || %s,updated_at=now()
                    WHERE run_id=%s AND status IN ('waiting_user','waiting_supervisor','blocked')""",
                (Jsonb({"pending_supervisor_questions": questions, "pending_questions_source_run_id": supervisor_run_id,
                        "pending_questions_decision_ref": json_compatible(decision_ref)}), task_id),
            )
            await connection.commit()

    async def record_supervision_recovery(self, *, card_message_id: str, external_event_id: str, actor_user_id: str, action: str) -> dict[str, Any]:
        if action not in {"retry", "wait", "cancel"}:
            raise ValueError("Unknown supervision recovery action")
        async with self.database.connection() as connection:
            card = await (await connection.execute("SELECT * FROM assistant.messages WHERE message_id=%s FOR UPDATE", (card_message_id,),)).fetchone()
            if not card or str(card.get("user_id") or "") != actor_user_id:
                raise PermissionError("Recovery card belongs to another user")
            body = dict(card.get("content_json") or {})
            if body.get("interaction_kind") != "supervision_recovery":
                raise ValueError("Not a supervision recovery card")
            event_key = f"supervision-recovery-action:{card_message_id}:{external_event_id}"
            if await (await connection.execute("SELECT 1 FROM assistant.messages WHERE idempotency_key=%s", (event_key,),)).fetchone():
                return {"duplicate": True, "action": action}
            if body.get("action_status") != "waiting":
                raise PermissionError("Recovery card is already resolved")
            task = await (await connection.execute("SELECT * FROM assistant.runs WHERE run_id=%s FOR UPDATE", (body.get("task_id"),),)).fetchone() if body.get("task_id") else None
            supervisor = await (await connection.execute("SELECT * FROM assistant.runs WHERE run_id=%s FOR UPDATE", (body.get("supervisor_run_id"),),)).fetchone() if body.get("supervisor_run_id") else None
            if task and (task["status"] in {"succeeded", "cancelled"} or (task.get("request_json") or {}).get("task_activation") == "suspended"):
                raise PermissionError("Task is no longer the paused mainline")
            if supervisor and supervisor["project_id"] != card["project_id"]:
                raise PermissionError("Supervisor belongs to another Project")
            if action == "retry":
                if supervisor and supervisor["status"] != "running":
                    patch = {"source_context_epoch": int((task or {}).get("request_json", {}).get("supervisor_context_epoch") or 0),
                             "source_turn_number": int((task or {}).get("request_json", {}).get("task_turn_count") or 0)}
                    await connection.execute(
                        """UPDATE assistant.runs SET status='queued',available_at=now(),ended_at=NULL,
                            cancel_requested_at=NULL,error_json=NULL,max_attempts=attempt+4,
                            request_json=(request_json-'turn_handoff_ref'-'turn_handoff_sha256') || %s,
                            updated_at=now() WHERE run_id=%s""", (Jsonb(patch), supervisor["run_id"]),
                    )
                elif task and not supervisor:
                    await self._queue_supervision_locked(connection, task, str(card["message_id"]), reason="user_recovery")
            elif action == "wait" and supervisor and supervisor["status"] == "queued":
                await connection.execute("UPDATE assistant.runs SET status='blocked',updated_at=now() WHERE run_id=%s", (supervisor["run_id"],))
            elif action == "cancel":
                target = task or supervisor
                if target:
                    await connection.execute(
                        """UPDATE assistant.runs SET cancel_requested_at=now(),
                            status=CASE WHEN status IN ('running','reconciling') THEN status ELSE 'cancelled' END,
                            updated_at=now() WHERE run_id=%s""", (target["run_id"],),
                    )
            if task:
                await connection.execute("UPDATE assistant.runs SET request_json=request_json || %s WHERE run_id=%s",
                    (Jsonb({"supervision_autoretry_paused": action == "wait"}), task["run_id"]))
            await connection.execute(
                """INSERT INTO assistant.messages(message_id,project_id,run_id,channel,chat_id,user_id,
                    direction,message_type,content_json,delivery_status,idempotency_key)
                    VALUES (%s,%s,%s,%s,%s,%s,'system','supervision_recovery_action',%s,'received',%s)""",
                (str(uuid.uuid4()), card["project_id"], card.get("run_id"), card["channel"], card["chat_id"],
                 actor_user_id, Jsonb({"context_visibility": "structured", "action": action, "card_message_id": card_message_id}), event_key),
            )
            body.update({"action_status": "resolved", "resolution_action": action})
            await connection.execute("UPDATE assistant.messages SET content_json=%s WHERE message_id=%s", (Jsonb(body), card_message_id))
            await connection.commit()
            return {"duplicate": False, "action": action, "task_id": body.get("task_id")}

    async def settle_failed_pm_turn(self, task_id: str, reason: str, source_run_id: str | None = None) -> None:
        async with self.database.connection() as connection:
            task = await (await connection.execute("SELECT * FROM assistant.runs WHERE run_id=%s FOR UPDATE", (task_id,),)).fetchone()
            if not task or not self._five_stage_task(task):
                return
            request = dict(task.get("request_json") or {})
            if (request.get("task_protocol") != "single-agent.v2" and not self._single_agent_project(str(task["project_id"]))) or not str(task["agent_key"]).endswith(":manager"):
                return
            if task.get("cancel_requested_at") or task["status"] == "cancelled":
                # A user's cancellation is final; it cannot become a recovery Run.
                return
            settlement_key = f"failed-attempt:{task_id}:{int(task.get('attempt') or 0)}"
            if request.get("failure_settlement_key") == settlement_key:
                return
            turn = int(request.get("task_turn_count") or 0) + 1
            source_id = str(task["trigger_message_id"])
            from assistant_kernel.services.document_runtime import TaskDocumentService
            ref = TaskDocumentService(self.settings).write_revision(
                project_id=str(task["project_id"]), task_id=task_id, document_kind="failed-turn",
                document_schema="turn-record.v1", payload={"outcome": "failed", "summary": reason, "turn_number": turn,
                    "trace_ref": (task.get("executor_json") or {}).get("trace_uri"),
                    "artifact_refs": list(task.get("artifact_refs") or []), "error": task.get("error_json")},
                status="failed", actor_type="kernel", actor_id="kernel", run_id=source_run_id or task_id,
                reason="failed PM call settled without claiming success", source_refs=[],
            )
            accepted = {**dict(request.get("accepted_documents") or {}), "failed_turn": ref}
            task = await (await connection.execute(
                """UPDATE assistant.runs SET status=CASE WHEN status='reconciling' THEN status ELSE 'waiting_supervisor' END,
                    request_json=request_json || %s,updated_at=now(),ended_at=NULL WHERE run_id=%s RETURNING *""",
                (Jsonb({"failed_turn_ref": ref, "pending_supervision_turn": turn,
                        "task_turn_count": turn, "failure_settlement_key": settlement_key,
                        "accepted_documents": accepted,
                        "pending_supervision_source_id": source_id, "supervision_failure": reason}), task_id),
            )).fetchone()
            await self._queue_supervision_locked(connection, task, source_id, reason="run_failure")
            await connection.commit()

    async def resume_completed_operation(self, operation_run_id: str) -> None:
        """Wake a PM waiting for registered receipts, not ordinary background jobs."""
        async with self.database.connection() as connection:
            operation = await (await connection.execute("SELECT * FROM assistant.runs WHERE run_id=%s", (operation_run_id,),)).fetchone()
            if not operation or not (operation.get("request_json") or {}).get("operation_action") or not operation.get("parent_run_id"):
                return
            task = await (await connection.execute("SELECT * FROM assistant.runs WHERE run_id=%s FOR UPDATE", (operation["parent_run_id"],),)).fetchone()
            if not task or task.get("cancel_requested_at") or task["status"] != "waiting_supervisor":
                return
            ids = list((task.get("request_json") or {}).get("pending_operation_runs") or [])
            if operation_run_id not in ids:
                return
            remaining = await (await connection.execute(
                "SELECT 1 FROM assistant.runs WHERE run_id=ANY(%s::uuid[]) AND status IN ('queued','running','reconciling') LIMIT 1", (ids,),
            )).fetchone()
            if remaining:
                return
            await connection.execute(
                "UPDATE assistant.runs SET request_json=request_json || %s WHERE run_id=%s",
                (Jsonb({"pending_operation_runs": [], "pm_instruction": "Registered action receipts are available. Inspect real results and finish or report the failure."}), task["run_id"]),
            )
            task = await (await connection.execute("SELECT * FROM assistant.runs WHERE run_id=%s", (task["run_id"],),)).fetchone()
            await self._advance_supervisor_context_epoch_locked(connection, task=task, reason="operation_receipts", source_id=operation_run_id)
            task = await (await connection.execute("SELECT * FROM assistant.runs WHERE run_id=%s", (task["run_id"],),)).fetchone()
            await self._queue_supervision_locked(connection, task, str(operation.get("trigger_message_id") or task["trigger_message_id"]), reason="operation_receipts")
            await connection.commit()


    async def validate_current_waiting_message(
        self, *, message_id: str, project_id: str, channel: str, chat_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        """Fence a user interaction before any external/file-side effect."""
        async with self.database.connection() as connection:
            from assistant_kernel.services.question_groups import projection_ref
            projection=await (await connection.execute('SELECT m.*,r.status AS run_status,r.cancel_requested_at FROM assistant.messages m JOIN assistant.runs r ON r.run_id=m.run_id WHERE m.message_id=%s',
                (message_id,))).fetchone()
            ref=projection_ref(projection['content_json'] or {}) if projection else None
            if ref:
                group=await (await connection.execute('SELECT * FROM assistant.messages WHERE message_id=%s',(ref['id'],))).fetchone()
                if ((projection['project_id'],projection['channel'],projection['chat_id'],projection['user_id'])!=(project_id,channel,chat_id,user_id)
                        or not group or group['project_id']!=project_id or group['content_json']['owner_user_id']!=user_id
                        or group['content_json']['group']['status']!='open'
                        or projection['content_json']['question_projection']['view_status']!='active'
                        or projection['content_json']['question_projection']['content_version']!=group['content_json']['group']['content_version']
                        or projection['run_status'] in {'succeeded','failed','cancelled','dead_letter'} or projection['cancel_requested_at']):
                    raise PermissionError('Canonical question projection is no longer current or belongs to another owner')
                return projection
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

    async def record_python_attempt(
        self, run_id: str, worker_instance_id: str, claim_token: str,
        receipt: dict[str, Any], artifact_refs: list[str],
    ) -> bool:
        """Fence Python evidence/identity to this attempt; never publish an old result."""
        identity = receipt.get("process_identity") or {}
        async with self.database.connection() as connection:
            row = await (await connection.execute(
                """UPDATE assistant.runs
                   SET executor_json=executor_json || %s, artifact_refs=%s,
                       result_json=NULL, process_pid=%s,process_start_ticks=%s,updated_at=now()
                   WHERE run_id=%s AND run_type='python' AND status='running'
                     AND attempt=%s AND worker_instance_id=%s AND claim_token=%s
                   RETURNING *""",
                (Jsonb({"python_attempt": json_compatible(receipt)}), Jsonb(artifact_refs),
                 identity.get("pid"), identity.get("start_ticks"), run_id,
                 receipt["attempt"], worker_instance_id, claim_token),
            )).fetchone()
            if row and receipt.get('settlement_ref'):
                from assistant_kernel.services.python_evidence import PythonEvidence
                await PythonEvidence(self.settings,row).record_documents_locked(connection,self,receipt)
            if row and receipt.get('ended_at') and receipt.get('stop_reason') not in {None,'CancelledError'}:
                from assistant_kernel.services.runtime_notifications import RuntimeNotifications
                await RuntimeNotifications(self.settings,self).failure_locked(connection,row,receipt)
            await connection.commit()
            return row is not None

    async def record_python_cleanup(
        self, run_id: str, attempt: int, cleanup: dict[str, Any],
    ) -> bool:
        """Record a fresh reconciliation probe without reopening or completing a Run."""
        async with self.database.connection() as connection:
            row = await (await connection.execute(
                """UPDATE assistant.runs SET executor_json=jsonb_set(
                       executor_json,'{python_attempt,cleanup}',%s),updated_at=now()
                   WHERE run_id=%s AND run_type='python' AND status='reconciling'
                     AND attempt=%s AND executor_json ? 'python_attempt'
                   RETURNING run_id""",
                (Jsonb(json_compatible(cleanup)), run_id, attempt),
            )).fetchone()
            await connection.commit()
            return row is not None

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
            from assistant_kernel.services.runtime_admission import RuntimeAdmission
            reservation=await RuntimeAdmission(self.settings).model_reservation_locked(connection,project_id,invocation_id)
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
                    Jsonb(json_compatible({**(metadata or {}),'capacity_reservation':reservation})),
                ),
            )
            await connection.commit()

    async def defer_model_capacity(self,run_id:str,worker_instance_id:str,claim_token:str,reason:str)->bool:
        """Only a pre-launch claim can wait for capacity without a failed Turn."""
        async with self.database.connection() as connection:
            row=await (await connection.execute("""UPDATE assistant.runs SET status='queued',attempt=GREATEST(attempt-1,0),
                available_at=now()+interval '2 seconds',worker_id=NULL,worker_instance_id=NULL,claim_token=NULL,lease_until=NULL,
                request_json=request_json || %s,updated_at=now()
                WHERE run_id=%s AND status='running' AND worker_instance_id=%s AND claim_token=%s
                  AND process_pid IS NULL AND cancel_requested_at IS NULL RETURNING run_id""",
                (Jsonb({'capacity_wait':{'resource':'model','reason':reason}}),run_id,worker_instance_id,claim_token))).fetchone()
            await connection.commit();return row is not None

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
            # Auxiliary Python belongs to this actual invocation, not merely to
            # the longer-lived PM Task. Never detach it when the model returns.
            await connection.execute("""UPDATE assistant.runs SET cancel_requested_at=COALESCE(cancel_requested_at,now()),
                status=CASE WHEN status='queued' THEN 'cancelled' ELSE status END,
                ended_at=CASE WHEN status='queued' THEN now() ELSE ended_at END,updated_at=now()
                WHERE run_type='python' AND status IN ('queued','running')
                  AND request_json->>'creating_invocation_id'=%s
                  AND request_json->>'lifecycle_relation'='invocation_dependent'""",(invocation_id,))
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
            if await self._settle_turn_inputs(connection, locked):
                updated = await self._requeue_pending_inputs(connection, locked)
                await connection.commit()
                return {'run':updated,'reply':None,'next_run':None,'input_pending':True}
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
        raise PermissionError("Stage execution is retired; use single-agent.v2")

    async def finalize_implementation_validation_rework(
        self, *, run_id: str, worker_instance_id: str, claim_token: str,
        summary: str, validation_artifact: dict[str, Any],
        artifact_refs: list[Any], max_reworks: int = 2,
    ) -> dict[str, Any]:
        raise PermissionError("Stage execution is retired; use single-agent.v2")

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

        Ordinary Discord questions share a durable paged card. Other channels
        and special inputs retain one message per question for native replies.
        """
        if message_type not in {"question", "approval"}:
            raise ValueError("message_type must be question or approval")
        async with self.database.connection() as connection:
            run = await (
                await connection.execute(
                    """
                    SELECT * FROM assistant.runs
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
            if str(run.get("agent_key") or "").endswith(":manager") and await self._settle_turn_inputs(connection, run):
                updated = await self._requeue_pending_inputs(connection, run)
                await connection.commit()
                return {'run':updated,'reply':None,'next_run':None,'input_pending':True}
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
            disclosed_subjects={}
            if message_type == "question":
                questions = [
                    {**dict(item),
                     "question_id": str(item.get("question_id") or "").strip(),
                     "question_text": str(item.get("question_text") or "").strip(),
                     "required": bool(item.get("required", True))}
                    for item in question.get("questions") or []
                ]
                from assistant_kernel.services.proposal_presentations import enrich_questions
                questions,disclosed_subjects=enrich_questions(self.settings,project_id,run_id,questions)
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
                        + ("（推荐）" if option.get("id") == item.get("recommended_option_id") else "")
                        + (f" — {option['description']}" if option.get("description") else "")
                        for index, option in enumerate(options, start=1)
                    )
                    label = "\n\n".join(
                        part for part in (
                            summary if ordinal == 1 else "",
                            str(item.get("topic") or ""),
                            question_text,
                            option_text,
                            (
                                "推荐理由：" + str(item["recommendation_reason"])
                                if item.get("recommendation_reason") and item.get("recommended_option_id")
                                in {option.get("id") for option in options} else ""
                            ),
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
                        "topic": item.get("topic"),
                        "recommended_option_id": item.get("recommended_option_id"),
                        "recommendation_reason": item.get("recommendation_reason"),
                        "ordinal": ordinal,
                        "interaction_seq": sequence,
                        "correlation": {**correlation, "question_id": question_id},
                    })
                if channel == "discord" and user_id and questionnaire_eligible(questions):
                    payloads = [make_questionnaire(
                        questions, summary=str(question.get("summary") or ""),
                        sequence=sequence, correlation=correlation,
                    )]
            else:
                payload = dict(question)
                payload["correlation"] = correlation
                payload["interaction_seq"] = sequence
                payloads.append(payload)

            messages: list[dict[str, Any]] = []
            for ordinal, payload in enumerate(payloads, start=1):
                if disclosed_subjects:payload['disclosed_subject_refs']=disclosed_subjects
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
            if is_questionnaire(payloads[0]):
                question_ids = [str(item["question_id"]) for item in questions]
                required_question_ids = [
                    str(item["question_id"]) for item in questions if item.get("required", True)
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
            if self._five_stage_task(run) and (run.get("request_json") or {}).get("task_protocol") == "single-agent.v2" and str(run.get("agent_key") or "").endswith(":manager"):
                task = await (await connection.execute("SELECT * FROM assistant.runs WHERE run_id=%s", (run_id,),)).fetchone()
                task = await (await connection.execute(
                    "UPDATE assistant.runs SET request_json=request_json || %s WHERE run_id=%s RETURNING *",
                    (Jsonb({"pending_supervision_turn": int((task.get("request_json") or {}).get("task_turn_count") or 0),
                            "pending_supervision_source_id": str(message["message_id"])}), run_id),
                )).fetchone()
                await self._queue_supervision_locked(connection, task, str(message["message_id"]), reason="after_agent_turn")
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
            await self._settle_turn_inputs(connection, locked)
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
        raise PermissionError("Stage execution is retired; use single-agent.v2")

    async def unblock_run(self, run_id: str, note: str) -> str:
        if not note.strip():
            raise ValueError("unblock note is required")
        async with self.database.connection() as connection:
            await self._require_active_run(connection, run_id)
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
        parent = None
        async with self.database.connection() as connection:
            locked = await (
                await connection.execute(
                    """
                    SELECT r.*,m.channel,m.chat_id,m.user_id
                    FROM assistant.runs r
                    JOIN assistant.messages m ON m.message_id=r.trigger_message_id
                    WHERE r.run_id=%s AND r.worker_instance_id=%s AND r.claim_token=%s
                      AND r.status='running' AND r.run_type='publish' AND r.cancel_requested_at IS NULL
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
            if (locked['request_json'] or {}).get('protocol_revision')=='1.2':
                parent=await (await connection.execute('SELECT * FROM assistant.runs WHERE run_id=%s FOR UPDATE',
                    (locked['parent_run_id'],))).fetchone()
                if not parent or parent['project_id']!=locked['project_id']:
                    raise PermissionError('Typed publication Task owner unavailable')
                from assistant_kernel.services.phase_control_v12 import PhaseControlV12
                parent=await PhaseControlV12(self.settings,self).publication_locked(connection,publication=locked,
                    parent=parent,result=result,reply_id=str(reply['message_id']))
                await connection.commit()
                return {'run_id':run_id,'reply_message_id':str(reply['message_id']),'parent_status':parent['status']}
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
                        RETURNING *
                        """,
                        (
                            Jsonb({"task_phase": "published", "publication_state": "published"}),
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
                if self._five_stage_project(str(parent["project_id"])):
                    parent_request = dict(parent.get("request_json") or {})
                    accepted = dict(parent_request.get("accepted_documents") or {})
                    from assistant_kernel.services.document_runtime import TaskDocumentService
                    documents = TaskDocumentService(self.settings)
                    publication_ref = documents.write_revision(
                        project_id=str(parent["project_id"]), task_id=str(parent["run_id"]),
                        document_kind="publication", document_schema="publication-receipt.v1",
                        payload=result, status="published", actor_type="publisher", actor_id="kernel",
                        run_id=run_id, reason="actual Publisher completion", source_refs=[],
                    )
                    accepted["publication"] = publication_ref
                    await connection.execute(
                        "UPDATE assistant.runs SET request_json=request_json || %s WHERE run_id=%s",
                        (Jsonb({"accepted_documents": accepted, "task_activation": "closed",
                                "state_revision": int(parent_request.get("state_revision") or 0) + 1}), parent["run_id"]),
                    )
                    design_ref = dict(accepted.get("solution_design") or {})
                    design = documents.read(str(parent["project_id"]), design_ref)["payload"] if design_ref else {}
                    changes = list((result.get("map_change_batch") or {}).get("changes") or result.get("files") or [])
                    shas = {str(item.get("target_uri") or ""): item.get("candidate_sha256") or item.get("after_sha256")
                            for item in changes if isinstance(item, dict)}
                    from assistant_kernel.services.workspace_scope import WorkspaceScopeService
                    scope = WorkspaceScopeService(self.settings)
                    roots = scope.resolve(str(parent["project_id"]), str(parent["run_id"]), None)
                    vault = ProjectRuntimeManager(self.settings.data_root).describe(str(parent["project_id"])).vault_root.resolve()
                    for uri, digest in list(shas.items()):
                        if uri.startswith("workspace://"):
                            try:
                                _, path, _ = scope.resolve_uri(roots, uri)
                                if vault in path.resolve().parents:
                                    shas[f"vault://{parent['project_id']}/{path.resolve().relative_to(vault).as_posix()}"] = digest
                            except (ValueError, PermissionError):
                                pass
                    actions = []
                    for original in design.get("runbook_actions") or []:
                        action = dict(original)
                        script_ref = dict(action.get("script_ref") or {})
                        current_sha = shas.get(str(script_ref.get("uri") or ""))
                        if current_sha:
                            if script_ref.get("sha256") and script_ref["sha256"] != current_sha:
                                action["activation_warning"] = "published_source_differs_from_design"
                            else:
                                script_ref["sha256"] = current_sha
                        action["script_ref"] = script_ref
                        actions.append(action)
                    baseline = {
                        "schema_version": "operational-baseline.v1", "task_id": str(parent["run_id"]),
                        "publication_run_id": run_id, "publication_receipt": result,
                        "task_contract_ref": accepted.get("task_contract"), "solution_design_ref": design_ref,
                        "requirements_approval": parent_request.get("requirements_approval"),
                        "design_approval": parent_request.get("design_approval"), "runbook_actions": actions,
                        "publication_ref": publication_ref, "created_at": datetime.now(timezone.utc).isoformat(),
                    }
                    await connection.execute(
                        """INSERT INTO assistant.kernel_meta(key,value) VALUES (%s,%s)
                            ON CONFLICT(key) DO UPDATE SET value=assistant.kernel_meta.value || EXCLUDED.value,updated_at=now()""",
                        (f"project-runtime:{parent['project_id']}", Jsonb({"active_task_id": None, "operational_baseline_ref": baseline})),
                    )
                pending = await (await connection.execute(
                    "SELECT 1 FROM assistant.messages WHERE run_id=%s AND content_json->>'task_input_status'='pending' LIMIT 1",
                    (parent["run_id"],),
                )).fetchone()
                if pending and not self._five_stage_project(str(parent["project_id"])):
                    await self._requeue_pending_inputs(connection, parent)
            await connection.commit()
        if parent is not None and self._five_stage_project(str(parent["project_id"])):
            from assistant_kernel.services.task_queue import ProjectTaskQueue
            queue = ProjectTaskQueue(self.settings, self)
            pending = await self.list_pending_task_inputs(str(parent["run_id"]))
            for message in pending:
                text = _history_text(dict(message.get("content_json") or {}))
                if text:
                    await queue.queue(str(parent["project_id"]), text, str(message["message_id"]),
                                      idempotency_key=f"post-publish-input:{message['message_id']}")
            approval_source = (parent.get("request_json") or {}).get("design_approval", {}).get("message_id") or str(parent["trigger_message_id"])
            await queue.queue(str(parent["project_id"]), "核验当前发布版本的启用状态并生成首份运营报告",
                              str(approval_source), task_kind="operation", idempotency_key=f"initial-operation:{run_id}")
            await queue.activate_next(str(parent["project_id"]))
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
            await self._require_active_run(connection, run_id)
            cursor = await connection.execute(
                """
                UPDATE assistant.runs
                SET status=CASE
                      WHEN executor_json ? 'validation_process'
                        AND COALESCE(executor_json#>>'{validation_process,cleanup,status}','unknown')<>'verified' THEN 'reconciling'
                      WHEN run_type='python' AND executor_json ? 'python_attempt'
                        AND COALESCE(executor_json#>>'{python_attempt,cleanup,status}','unknown')<>'verified'
                        THEN 'reconciling'
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
                      WHEN run_type='chat' AND agent_key LIKE '%%:manager'
                        AND COALESCE((request_json->>'design_required')::boolean,false) THEN 'waiting_supervisor'
                      WHEN attempt>=max_attempts THEN 'dead_letter'
                      ELSE 'queued' END,
                    available_at=CASE
                      WHEN run_type='python' AND executor_json ? 'python_attempt'
                        AND COALESCE(executor_json#>>'{python_attempt,cleanup,status}','unknown')<>'verified'
                        THEN available_at
                      WHEN cancel_requested_at IS NOT NULL THEN available_at
                      WHEN run_type='publish' OR (process_pid IS NOT NULL AND (
                        run_type='python' OR COALESCE((executor_json->>'may_write_shared')::boolean,
                            COALESCE(executor_json->>'sandbox','')<>'read-only')
                      )) THEN available_at
                      WHEN attempt>=max_attempts THEN available_at
                      WHEN request_json->>'interaction_kind'='supervisor_consultation'
                        THEN now() + (CASE GREATEST(max_attempts-attempt,0)
                          WHEN 3 THEN 30 WHEN 2 THEN 120 ELSE 600 END) * interval '1 second'
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
                    ended_at=CASE
                      WHEN run_type='python' AND executor_json ? 'python_attempt'
                        AND COALESCE(executor_json#>>'{python_attempt,cleanup,status}','unknown')<>'verified'
                        THEN NULL
                      WHEN cancel_requested_at IS NOT NULL THEN now()
                      WHEN attempt>=max_attempts AND NOT (
                        run_type='publish' OR (process_pid IS NOT NULL AND (
                          run_type='python' OR COALESCE((executor_json->>'may_write_shared')::boolean,
                              COALESCE(executor_json->>'sandbox','')<>'read-only')
                        ))
                      ) THEN now() ELSE NULL END,
                    updated_at=now()
                WHERE run_id=%s AND worker_instance_id=%s AND claim_token=%s
                  AND status='running'
                RETURNING *
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
            await self._require_active_run(connection, run_id)
            owner=await (await connection.execute('SELECT executor_json FROM assistant.runs WHERE run_id=%s FOR UPDATE',(run_id,))).fetchone()
            validation=(owner['executor_json'] or {}).get('validation_process') if owner else None
            if validation and validation.get('cleanup',{}).get('status')!='verified':
                raise PermissionError('Validator cleanup is unresolved; inspect the actual receipt before any reconciliation action')
            cursor = await connection.execute(
                """
                UPDATE assistant.runs
                SET status=%s,available_at=CASE WHEN %s='queued' THEN now() ELSE available_at END,
                    ended_at=CASE WHEN %s IN ('failed','cancelled') THEN now() ELSE NULL END,
                    cancel_requested_at=CASE WHEN %s='queued' THEN NULL ELSE cancel_requested_at END,
                    error_json=COALESCE(error_json,'{}'::jsonb) || %s,
                    process_pid=NULL,process_start_ticks=NULL,updated_at=now()
                WHERE run_id=%s AND status='reconciling'
                    AND (NOT executor_json ? 'validation_process' OR executor_json#>>'{validation_process,cleanup,status}'='verified')
                RETURNING *
                """,
                (target, target, target, target, Jsonb({"reconciliation_note": note}), run_id),
            )
            row = await cursor.fetchone()
            if row is not None and action == "retry" and str(row["agent_key"]).endswith(":manager") and self._five_stage_task(row):
                row = await (await connection.execute(
                    """UPDATE assistant.runs SET status='waiting_supervisor',request_json=request_json || %s
                       WHERE run_id=%s RETURNING *""",
                    (Jsonb({"supervision_failure": None}), run_id),
                )).fetchone()
                await self._queue_supervision_locked(
                    connection, row, str(row["trigger_message_id"]), reason="pm_reconciled",
                )
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
                WHERE r.status='reconciling' AND r.project_id NOT IN ('system','butler-maintenance') AND r.agent_key NOT IN ('system-butler','project:kernel:intake','project:butler-maintenance:intake','retired:management') AND m.channel='discord'
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
                WHERE status='reconciling' AND project_id NOT IN ('system','butler-maintenance') AND agent_key NOT IN ('system-butler','project:kernel:intake','project:butler-maintenance:intake','retired:management')
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
            await self._require_active_run(connection, run_id)
            cursor = await connection.execute(
                """
                UPDATE assistant.runs
                SET status=CASE WHEN attempt>=max_attempts THEN 'dead_letter'
                                ELSE 'queued' END,
                    available_at=CASE
                      WHEN attempt>=max_attempts THEN available_at
                      WHEN request_json->>'interaction_kind'='supervisor_consultation'
                        THEN now() + (CASE GREATEST(max_attempts-attempt,0)
                          WHEN 3 THEN 30 WHEN 2 THEN 120 ELSE 600 END) * interval '1 second'
                      ELSE now()+(LEAST(attempt,6)*interval '5 seconds') END,
                    ended_at=CASE WHEN attempt>=max_attempts THEN now()
                                  ELSE ended_at END,
                    error_json=COALESCE(error_json,'{}'::jsonb) || %s,
                    updated_at=now()
                WHERE run_id=%s AND status='reconciling'
                    AND (NOT executor_json ? 'validation_process' OR executor_json#>>'{validation_process,cleanup,status}'='verified')
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
                SELECT m.message_id,m.run_id,m.channel,m.chat_id,m.direction,m.reply_to_message_id,
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
                "text": (_history_text(dict(row["content_json"] or {})) or (
                    json.dumps({key: (row["content_json"] or {})[key] for key in
                        ("questions", "answers", "choice_ids", "additional_text")
                        if key in (row["content_json"] or {})}, ensure_ascii=False)
                    if (row["content_json"] or {}).get("schema_version") == "questionnaire-answer.v1" else ""
                ))[:4000],
                "content_json": {key: (row["content_json"] or {})[key] for key in
                    ("schema_version", "questions", "answers", "choice_ids", "additional_text", "text")
                    if key in (row["content_json"] or {})},
                "created_at": str(row["created_at"]),
            }
            for row in self._filter_revoked_context(rows)
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
                  AND status NOT IN ('succeeded','failed','dead_letter','cancelled')
                  AND cancel_requested_at IS NULL
                  AND COALESCE(request_json->>'task_activation','active')='active'
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
            await self._require_active_run(connection, run_id)
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
            await self._require_active_run(connection, str(card['run_id']))
            executor = dict(card.get("executor_json") or {})
            reconciliation = dict(executor.get("reconciliation") or {})
            python_attempt = executor.get("python_attempt") or {}
            if (card["run_type"] == "python" and python_attempt and action != "wait"
                    and (python_attempt.get("cleanup") or {}).get("status") != "verified"):
                raise PermissionError("Python process cleanup must be verified before resolving this Run")
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
                    status=CASE WHEN status IN ('queued','waiting_user','waiting_supervisor') THEN 'cancelled' ELSE status END,
                    ended_at=CASE WHEN status IN ('queued','waiting_user','waiting_supervisor') THEN now() ELSE ended_at END,
                    updated_at=now()
                WHERE run_id=%s AND status IN ('queued','running','waiting_user','waiting_supervisor')
                  AND NOT (
                    COALESCE(request_json->>'task_protocol','')='single-agent.v2'
                    AND COALESCE(request_json->>'task_phase','') IN
                        ('publishing','publication_reconciling')
                    AND COALESCE(request_json->>'protocol_revision','')<>'1.2'
                  )
                RETURNING *
                """,
                (run_id,),
            )
            row=await cursor.fetchone()
            updated=row is not None
            if row and (row['request_json'] or {}).get('protocol_revision')=='1.2':
                source_id=str(uuid.uuid4())
                await connection.execute("""INSERT INTO assistant.messages(message_id,project_id,run_id,channel,chat_id,
                    direction,message_type,content_json,delivery_status,idempotency_key)
                    VALUES (%s,%s,%s,'kernel',%s,'system','cancel_request',%s,'received',%s)""",
                    (source_id,row['project_id'],run_id,'cancel:'+run_id,Jsonb({'run_id':run_id,'origin':'compiled_cancel_controller',
                        'context_visibility':'structured','human_answer':False}), 'v12-cancel:'+run_id+':'+source_id))
                from assistant_kernel.services.question_groups import QuestionGroupService
                groups=await (await connection.execute("""SELECT message_id FROM assistant.messages WHERE project_id=%s
                    AND message_type='question_group' AND content_json#>>'{group,status}' IN ('open','answered')
                    AND (run_id=%s OR content_json->>'task_id'=%s) FOR UPDATE""",(row['project_id'],run_id,run_id))).fetchall()
                for group in groups:
                    await QuestionGroupService(self.settings,self).close_locked(connection,group_id=str(group['message_id']),
                        status='cancelled',source_message_id=source_id)
                await connection.execute("""UPDATE assistant.runs SET cancel_requested_at=COALESCE(cancel_requested_at,now()),
                    status=CASE WHEN status IN ('queued','waiting_user','waiting_supervisor') THEN 'cancelled' ELSE status END,
                    ended_at=CASE WHEN status IN ('queued','waiting_user','waiting_supervisor') THEN now() ELSE ended_at END
                    WHERE project_id=%s AND request_json->>'task_id'=%s AND agent_key LIKE '%%:supervisor'
                    AND NOT COALESCE((request_json->>'mainline_context_only')::boolean,false)
                    AND status IN ('queued','running','waiting_user','waiting_supervisor','reconciling')""",(row['project_id'],run_id))
                queued_publication=await (await connection.execute("""UPDATE assistant.runs SET status='cancelled',cancel_requested_at=now(),ended_at=now()
                    WHERE parent_run_id=%s AND run_type='publish' AND status='queued' RETURNING run_id""",(run_id,))).fetchone()
                patch={'cancel_source_message_id':source_id}
                if queued_publication:patch['publication_disposition']='cancelled_before_start'
                await connection.execute('UPDATE assistant.runs SET request_json=request_json || %s WHERE run_id=%s',(Jsonb(patch),run_id))
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
        from assistant_kernel.services.kernel_management import require_active_identity
        require_active_identity(project_id, agent_key)
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
            # Context occupancy and unavailable usage are telemetry, never a
            # Kernel session boundary. Only a verified explicit boundary can
            # seal the current native session.
            rotation_needed = bool(current and rotation_checkpoint)
            if current and not rotation_needed:
                if current['status']=='rotation_required':
                    await connection.execute("UPDATE assistant.sessions SET status='active',updated_at=now() WHERE session_id=%s",(current['session_id'],))
                    current={**current,'status':'active'}
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
        async with self.database.connection() as connection:
            row = await self._create_outbound_locked(
                connection, project_id=project_id,session_id=session_id,run_id=run_id,
                channel=channel,chat_id=chat_id,user_id=user_id,message_type=message_type,
                content=content,reply_to_message_id=reply_to_message_id,idempotency_key=idempotency_key,
            )
            await connection.commit()
            return row

    async def _create_outbound_locked(
        self, connection, *, project_id: str, session_id: str | None,
        run_id: str | None, channel: str, chat_id: str, user_id: str | None,
        message_type: str, content: dict[str, Any], reply_to_message_id: str | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Join the caller's transaction; no second queue and no hidden commit."""
        message_id = str(uuid.uuid4())
        run_id = str(run_id) if run_id is not None else None
        session_id = str(session_id) if session_id is not None else None
        reply_to_message_id = str(reply_to_message_id) if reply_to_message_id is not None else None
        from assistant_kernel.services.system_config import STATE_KEY,SystemConfigService
        configured=await (await connection.execute('SELECT value FROM assistant.kernel_meta WHERE key=%s',(STATE_KEY,))).fetchone()
        retry_limit=10
        if configured:
            policy=SystemConfigService(self.settings)._config(configured['value']['desired_ref'])['notifications']
            retry_limit=policy['delivery_max_attempts']
            content={**content,'delivery':{**(content.get('delivery') or {}),'policy':{**policy,'source_ref':configured['value']['desired_ref']}}}
        cursor = await connection.execute(
                """
                INSERT INTO assistant.messages(
                    message_id,project_id,session_id,run_id,channel,chat_id,user_id,direction,
                    message_type,reply_to_message_id,content_json,delivery_status,idempotency_key,max_retries
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,'outbound',%s,%s,%s,'pending',%s,%s)
                ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL
                DO UPDATE SET idempotency_key=EXCLUDED.idempotency_key RETURNING *
                """,
                (
                    message_id, project_id, session_id, run_id, channel, chat_id, user_id,
                    message_type, reply_to_message_id, Jsonb(json_compatible(content)),
                    idempotency_key,retry_limit,
                ),
            )
        row = await cursor.fetchone()
        semantic = lambda body: {k:v for k,v in body.items() if k != "delivery"}
        if (
                str(row["project_id"]) != project_id
                or str(row["channel"]) != channel
                or str(row["chat_id"]) != chat_id
                or str(row["message_type"]) != message_type
                or semantic(row["content_json"] or {}) != semantic(json_compatible(content))
                or (str(row["run_id"]) if row["run_id"] else None) != run_id
                or (str(row["session_id"]) if row["session_id"] else None) != session_id
                or (str(row["reply_to_message_id"]) if row["reply_to_message_id"] else None) != reply_to_message_id
                or row["user_id"] != user_id
            ):
            raise ValueError("idempotency_key was reused for different outbound content or ownership")
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
                      AND (content_json->>'schema_version' IS DISTINCT FROM 'question-projection-refresh.v1'
                        OR EXISTS (SELECT 1 FROM assistant.messages target
                            WHERE target.message_id::text=messages.content_json->>'target_message_id'
                              AND target.external_message_id IS NOT NULL))
                      AND NOT EXISTS (SELECT 1 FROM assistant.messages active
                          WHERE active.direction='outbound' AND active.delivery_status='delivering'
                            AND active.content_json->>'target_message_id'=messages.content_json->>'target_message_id')
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
            if row:
                from assistant_kernel.services.question_groups import QuestionGroupService, projection_ref, REFRESH_SCHEMA
                if projection_ref(row['content_json'] or {}) or (row['content_json'] or {}).get('schema_version')==REFRESH_SCHEMA:
                    row = await QuestionGroupService(self.settings,self).hydrate(connection,row)
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
                        OR (m.content_json->>'schema_version'='question-group-projection.v1'
                            AND m.content_json#>>'{question_projection,view_status}'='active'
                            AND r.status IN ('waiting_user','waiting_supervisor'))
                        OR (m.content_json->>'interaction_kind'='supervision_recovery'
                            AND m.content_json->>'action_status'='waiting'
                            AND (r.status IN ('waiting_supervisor','waiting_user','blocked','dead_letter','failed')
                                OR m.content_json->>'task_id' IS NULL))
                        OR (m.content_json->>'interaction_kind'='supervisor_status'
                            AND m.content_json->>'action_status'='waiting'
                            AND r.status IN ('waiting_user','waiting_supervisor'))
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
            from assistant_kernel.services.question_groups import QuestionGroupService, projection_ref
            service=QuestionGroupService(self.settings,self)
            rows=[await service.hydrate(connection,row) if projection_ref(row['content_json'] or {}) else row for row in rows]
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
            original=await (await connection.execute('SELECT * FROM assistant.messages WHERE message_id=%s',(message_id,))).fetchone()
            if original and original['content_json'].get('interaction_kind')=='operation_notice':
                from assistant_kernel.services.operation_records import OperationRecords
                target=await OperationRecords(self.settings,self).delivery_target(connection,original)
                if not target['active']:
                    await connection.execute("UPDATE assistant.messages SET delivery_status='cancelled',worker_id=NULL,worker_instance_id=NULL,lease_until=NULL,error_text='operation notice policy or route changed',updated_at=now() WHERE message_id=%s",(message_id,))
                    await connection.commit();return False
                if target['route']!=(original['channel'],original['chat_id'],original['user_id']):
                    route=target['route'] or ('internal','pending-project-operations',None)
                    await connection.execute("UPDATE assistant.messages SET channel=%s,chat_id=%s,user_id=%s,delivery_status='pending',worker_id=NULL,worker_instance_id=NULL,lease_until=NULL,content_json=jsonb_set(content_json,'{pending_operation_route}',%s::jsonb),updated_at=now() WHERE message_id=%s",
                        (*route,Jsonb(not bool(target['route'])),message_id))
                    await connection.commit();return False
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
                    content_json=jsonb_set(content_json,'{delivery}',COALESCE(content_json->'delivery','{}'::jsonb) || %s::jsonb),worker_id=NULL,worker_instance_id=NULL,
                    lease_until=NULL,sent_at=now(),updated_at=now(),error_text=NULL
                WHERE message_id=%s AND worker_instance_id=%s AND delivery_status='delivering'
                RETURNING message_id
                """,
                (
                    external_message_id, Jsonb(json_compatible(delivery or {})),
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
                      ELSE now()+LEAST(retry_count*COALESCE((content_json#>>'{delivery,policy,retry_base_seconds}')::integer,10),
                        COALESCE((content_json#>>'{delivery,policy,retry_max_seconds}')::integer,60))*interval '1 second' END,
                    worker_id=NULL,worker_instance_id=NULL,lease_until=NULL,
                    error_text=left(%s,COALESCE((content_json#>>'{delivery,policy,error_excerpt_chars}')::integer,2000)),updated_at=now()
                WHERE message_id=%s AND worker_instance_id=%s AND delivery_status='delivering'
                RETURNING delivery_status
                """,
                (error, message_id, worker_instance_id),
            )
            row = await cursor.fetchone()
            await connection.commit()
            return str(row["delivery_status"]) if row else "lease_lost"
