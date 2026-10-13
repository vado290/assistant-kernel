"""Small Project foreground queue over existing Messages, Runs and metadata."""
from __future__ import annotations

import copy
import hashlib
import uuid
from datetime import datetime, timezone
from typing import Any

from psycopg.types.json import Jsonb
import yaml

from assistant_kernel.config import KernelSettings
from assistant_kernel.repositories import CoreRepository
from assistant_kernel.repositories.core import canonical_sha256, json_compatible
from assistant_kernel.services.document_runtime import TaskDocumentService, resource_ref
from assistant_kernel.services.workspace_scope import WorkspaceScopeService
from assistant_kernel.storage import ProjectRuntimeManager
from assistant_kernel.storage.project_runtime import PROJECT_ID_PATTERN


ACTIVE_STATUSES = ("queued", "running", "waiting_user", "waiting_supervisor", "blocked", "reconciling")
WORK_SCHEMA = "project-work-request.v1"


class ProjectTaskQueue:
    def __init__(self, settings: KernelSettings, repository: CoreRepository | None = None):
        self.settings = settings
        self.repository = repository or CoreRepository(settings)
        self.runtime = ProjectRuntimeManager(settings.data_root)
        self.documents = TaskDocumentService(settings)

    @staticmethod
    def metadata_key(project_id: str) -> str:
        return f"project-runtime:{project_id}"

    @staticmethod
    async def _lock(connection, project_id: str) -> None:
        await connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                                 (f"project-mainline:{project_id}",))

    def _project(self, project_id: str, task: dict[str, Any] | None = None) -> dict[str, Any]:
        if not PROJECT_ID_PATTERN.fullmatch(project_id):
            raise ValueError("project_id must be a safe slug")
        from assistant_kernel.services.published_agents import PublishedAgentService
        project=PublishedAgentService(self.settings).project_policy(project_id,task=task)
        if not isinstance(project, dict) or project.get("task_protocol") != "single-agent.v2":
            raise PermissionError("foreground work requires a single-agent.v2 Project")
        return project

    async def _meta(self, connection, project_id: str) -> dict[str, Any]:
        await connection.execute(
            "INSERT INTO assistant.kernel_meta(key,value) VALUES (%s,'{}'::jsonb) ON CONFLICT DO NOTHING",
            (self.metadata_key(project_id),),
        )
        row = await (await connection.execute(
            "SELECT value FROM assistant.kernel_meta WHERE key=%s FOR UPDATE",
            (self.metadata_key(project_id),),
        )).fetchone()
        return dict(row["value"] or {})

    async def _patch_meta(self, connection, project_id: str, patch: dict[str, Any]) -> None:
        await connection.execute(
            "UPDATE assistant.kernel_meta SET value=value || %s,updated_at=now() WHERE key=%s",
            (Jsonb(json_compatible(patch)), self.metadata_key(project_id)),
        )

    @staticmethod
    async def _active(connection, project_id: str, *, except_id: str | None = None):
        return await (await connection.execute(
            """SELECT * FROM assistant.runs WHERE project_id=%s AND agent_key=%s
                AND run_type='chat' AND status=ANY(%s)
                AND COALESCE(request_json->>'task_activation','active')='active'
                AND (%s::uuid IS NULL OR run_id<>%s::uuid)
                ORDER BY created_at,run_id FOR UPDATE""",
            (project_id, f"project:{project_id}:manager", list(ACTIVE_STATUSES), except_id, except_id),
        )).fetchall()

    async def queue(
        self, project_id: str, goal: str, source_message_id: str, *, priority: str = "normal",
        task_kind: str = "delivery", report_window: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        self._project(project_id)
        if priority not in {"normal", "urgent"} or task_kind not in {"delivery", "operation"}:
            raise ValueError("unknown work priority or kind")
        if not isinstance(goal, str) or not goal.strip():
            raise ValueError("work goal is required")
        source_message_id = str(uuid.UUID(source_message_id))
        if report_window is not None and not isinstance(report_window, dict):
            raise ValueError("report_window must be an object")
        async with self.repository.database.connection() as connection:
            await self._lock(connection, project_id)
            source = await (await connection.execute(
                "SELECT * FROM assistant.messages WHERE message_id=%s", (source_message_id,),
            )).fetchone()
            if source is None or str(source["project_id"]) != project_id or source["direction"] != "inbound":
                raise PermissionError("work must reference an original user Message in this Project")
            content = {
                "schema_version": WORK_SCHEMA, "goal": goal.strip(), "text": goal.strip(),
                "priority": priority, "task_kind": task_kind, "work_status": "queued",
                "source_message_id": source_message_id,
                "source_ref": resource_ref(kind="message", identity=source_message_id, purpose="user_work_source"),
                "report_window": report_window,
            }
            key = f"project-work:{project_id}:" + (idempotency_key or canonical_sha256(content))
            content["work_idempotency_key"] = key
            row = await (await connection.execute(
                """INSERT INTO assistant.messages(message_id,project_id,channel,chat_id,user_id,
                    direction,message_type,reply_to_message_id,content_json,delivery_status,idempotency_key)
                    VALUES (%s,%s,%s,%s,%s,'system','work_request',%s,%s,'received',%s)
                    ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL
                    DO UPDATE SET idempotency_key=EXCLUDED.idempotency_key RETURNING *""",
                (str(uuid.uuid4()), project_id, source["channel"], source["chat_id"], source.get("user_id"),
                 source_message_id, Jsonb(content), key),
            )).fetchone()
            original = dict(row["content_json"] or {})
            comparable = {key: original.get(key) for key in content if key != "work_status"}
            expected = {key: value for key, value in content.items() if key != "work_status"}
            if comparable != expected:
                raise ValueError("work idempotency key was reused with different content")
            await connection.commit()
            return json_compatible(row)

    async def suspend(self, task_id: str, reason: str, incident_ref: Any = None) -> dict[str, Any]:
        if not str(reason).strip():
            raise ValueError("suspension reason is required")
        async with self.repository.database.connection() as connection:
            found = await (await connection.execute(
                "SELECT project_id FROM assistant.runs WHERE run_id=%s", (task_id,),
            )).fetchone()
            if found is None:
                raise KeyError("Task was not found")
            project_id = str(found["project_id"])
            self._project(project_id)
            await self._lock(connection, project_id)
            metadata = await self._meta(connection, project_id)
            task = await (await connection.execute(
                "SELECT * FROM assistant.runs WHERE run_id=%s FOR UPDATE", (task_id,),
            )).fetchone()
            updated = await self._suspend_locked(connection, task, metadata, reason, incident_ref)
            await connection.commit()
            return json_compatible(updated)

    async def _suspend_locked(self, connection, task, metadata, reason, incident_ref=None):
        project_id, task_id = str(task['project_id']), str(task['run_id'])
        request = dict(task["request_json"] or {})
        if request.get("task_activation") == "suspended":
            return task
        if task["agent_key"] != f"project:{project_id}:manager" or task["run_type"] != "chat":
            raise PermissionError("only the Project PM Task can be suspended")
        if task["status"] not in {"queued", "waiting_user", "waiting_supervisor", "blocked"}:
            raise PermissionError("Task can only suspend at a settled safe boundary")
        if task.get("cancel_requested_at"):
            raise PermissionError("a cancelled Task cannot suspend")
        publisher = await (await connection.execute(
            """SELECT 1 FROM assistant.runs WHERE parent_run_id=%s AND run_type='publish'
                AND status NOT IN ('succeeded','failed','cancelled','dead_letter') LIMIT 1""", (task_id,),
        )).fetchone()
        if publisher or request.get("task_phase") in {"publishing", "publication_reconciling"} or request.get("publication_state") in {"queued", "running", "reconciling"}:
            raise PermissionError("a publishing Task cannot suspend")
        managed_action = await (await connection.execute(
            """SELECT 1 FROM assistant.runs WHERE parent_run_id=%s AND run_type='python'
                AND jsonb_typeof(request_json->'operation_action')='object'
                AND request_json->'operation_action'<>'{}'::jsonb
                AND status IN ('running','reconciling') LIMIT 1""",
            (task_id,),
        )).fetchone()
        if managed_action:
            raise PermissionError("a Task with a managed operation action cannot suspend yet")
        active = await self._active(connection, project_id)
        if len(active) != 1 or str(active[0]["run_id"]) != str(task["run_id"]):
            raise PermissionError("only the current Project foreground Task can suspend")
        patch = {
            "task_activation": "suspended",
            "suspension": {"previous_status": task["status"], "reason": reason.strip(),
                           "incident_ref": incident_ref, "created_at": datetime.now(timezone.utc).isoformat(),
                           "operational_baseline_ref": metadata.get("operational_baseline_ref")},
            "supervisor_context_epoch": int(request.get("supervisor_context_epoch") or 0) + 1,
        }
        updated = await (await connection.execute(
            "UPDATE assistant.runs SET status='blocked',request_json=request_json || %s,updated_at=now() WHERE run_id=%s RETURNING *",
            (Jsonb(json_compatible(patch)), task_id),
        )).fetchone()
        await self._patch_meta(connection, project_id, {"active_task_id": None})
        return updated

    def _validate_resume_refs(self, project_id: str, task_id: str, request: dict[str, Any]) -> tuple[str | None, list[str]]:
        invalid = []
        accepted = request.get("accepted_documents") or {}
        contract = accepted.get("task_contract")
        design = accepted.get("solution_design") or accepted.get("design")
        for kind, ref in (("requirements", contract), ("design", design)):
            if ref is None:
                if kind == "requirements":
                    invalid.append(kind)
                continue
            try:
                document = self.documents.read(project_id, ref)
                self._verify_local_source_refs(project_id, task_id, document.get("payload"))
                approval = request.get("requirements_approval" if kind == "requirements" else "design_approval") or {}
                if approval and approval.get("approved_document_sha256") != ref.get("sha256"):
                    invalid.append(kind)
            except (OSError, ValueError, KeyError, PermissionError, TypeError):
                invalid.append(kind)
        if accepted.get("last_turn"):
            try:
                self.documents.read(project_id, accepted["last_turn"])
            except (OSError, ValueError, KeyError, PermissionError, TypeError):
                invalid.append("last_turn")
        return ("requirements" if "requirements" in invalid else "design" if invalid else None), invalid

    def _verify_local_source_refs(self, project_id: str, task_id: str, value: Any) -> None:
        """Check recorded local hashes; remote evidence stays a Supervisor concern."""
        if isinstance(value, list):
            for item in value:
                self._verify_local_source_refs(project_id, task_id, item)
        elif isinstance(value, dict):
            uri, expected = value.get("uri"), value.get("sha256")
            if isinstance(uri, str) and expected:
                if uri.startswith(f"vault://{project_id}/"):
                    path = self.runtime.resolve_vault_file(project_id, uri.split("/", 3)[3])
                    if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                        raise ValueError("recorded source hash changed")
                elif uri.startswith("workspace://"):
                    key, _, relative = uri.removeprefix("workspace://").partition("/")
                    roots = WorkspaceScopeService(self.settings).resolve(project_id, task_id, "requirements")
                    root = next((item for item in roots if item.key == key), None)
                    if root is None:
                        raise PermissionError("recorded source is no longer authorized")
                    path = self.runtime.resolve_below(root.path, relative)
                    if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                        raise ValueError("recorded source hash changed")
            for item in value.values():
                if isinstance(item, (dict, list)):
                    self._verify_local_source_refs(project_id, task_id, item)

    async def _resume_locked(self, connection, task: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
        project_id, task_id = str(task["project_id"]), str(task["run_id"])
        request = copy.deepcopy(dict(task["request_json"] or {}))
        if request.get("task_activation") != "suspended" or task["status"] != "blocked" or task.get("cancel_requested_at"):
            raise PermissionError("Task is not suspended")
        if await self._active(connection, project_id, except_id=task_id):
            raise PermissionError("another Project foreground Task is active")
        if request.get('protocol_revision')=='1.2':
            invalid=[]
            for name,value in (request.get('accepted_documents') or {}).items():
                for ref in value if isinstance(value,list) else [value]:
                    if ref is None:continue
                    try:
                        self.documents.read(project_id,ref)
                        self._verify_local_source_refs(project_id,task_id,ref)
                    except (OSError,ValueError,KeyError,PermissionError,TypeError):invalid.append(name)
            waiting=False
            if not invalid and task.get('waiting_question_message_id'):
                question=await (await connection.execute('SELECT * FROM assistant.messages WHERE message_id=%s',(task['waiting_question_message_id'],))).fetchone()
                if question:
                    from assistant_kernel.services.question_groups import projection_ref
                    group_ref=projection_ref(question['content_json'])
                    if group_ref:
                        group=await (await connection.execute('SELECT content_json FROM assistant.messages WHERE message_id=%s',(group_ref['id'],))).fetchone()
                        waiting=bool(group and group['content_json']['group']['status']=='open')
                    else:waiting=(request.get('suspension') or {}).get('previous_status')=='waiting_user' and question['content_json'].get('interaction_status') not in {'answered','superseded','cancelled'}
            request.update(task_activation='active',resume_invalid_refs=invalid,resumed_at=datetime.now(timezone.utc).isoformat(),
                supervisor_context_epoch=int(request.get('supervisor_context_epoch') or 0)+1,force_session_rotation=None)
            updated=await (await connection.execute("""UPDATE assistant.runs SET status=%s,request_json=%s,ended_at=NULL,
                waiting_question_message_id=CASE WHEN %s THEN waiting_question_message_id ELSE NULL END,updated_at=now()
                WHERE run_id=%s RETURNING *""",('waiting_user' if waiting else 'waiting_supervisor',Jsonb(json_compatible(request)),waiting,task_id))).fetchone()
            await self._patch_meta(connection,project_id,{'active_task_id':task_id})
            if not waiting:await self.repository._queue_supervision_locked(connection,updated,str(task['trigger_message_id']),reason='task_resumed')
            return updated
        latest_baseline = metadata.get("operational_baseline_ref")
        if request.get("current_stage") == "operate" and isinstance(latest_baseline, dict):
            if latest_baseline.get("schema_version") == "operational-baseline.v1":
                accepted = dict(request.get("accepted_documents") or {})
                accepted.update({"task_contract": latest_baseline["task_contract_ref"],
                                 "solution_design": latest_baseline["solution_design_ref"]})
                request.update({"accepted_documents": accepted, "operational_baseline_ref": latest_baseline,
                                "requirements_approval": latest_baseline.get("requirements_approval"),
                                "design_approval": latest_baseline.get("design_approval")})
        mode, invalid = self._validate_resume_refs(project_id, task_id, request)
        project = self._project(project_id,task)
        if not mode and request.get("current_stage") in {"build", "audit"}:
            if not request.get("requirements_approval"):
                mode, invalid = "requirements", ["missing_requirements_approval"]
            elif (project.get("single_agent") or {}).get("design_required") and not request.get("design_approval"):
                mode, invalid = "design", ["missing_design_approval"]
        previous = (request.get("suspension") or {}).get("previous_status")
        old_baseline = (request.get("suspension") or {}).get("operational_baseline_ref")
        if not mode and request.get("current_stage") in {"build", "audit"} and old_baseline != metadata.get("operational_baseline_ref"):
            mode, invalid = "design", ["operational_baseline_changed"]
        accepted = dict(request.get("accepted_documents") or {})
        if "last_turn" in invalid:
            accepted.pop("last_turn", None)
        if mode == "requirements":
            if "requirements" in invalid:
                source_id = str(request.get("source_message_id") or task["trigger_message_id"])
                accepted["task_contract"] = self.documents.write_revision(
                    project_id=project_id, task_id=task_id, document_kind="task-contract", document_schema="task-contract.v1",
                    payload={"goal": request.get("task_goal") or "Resume task", "scope": {"included": [], "excluded": []},
                             "requirements": [], "acceptance_criteria": [], "constraints": [],
                             "source_refs": [resource_ref(kind="message", identity=source_id, purpose="resume_user_source")]},
                    status="draft", actor_type="kernel", actor_id="kernel", run_id=task_id,
                    reason="Resume requires a valid Requirements revision",
                )
            request["requirements_approval"] = None
        if mode:
            request["design_approval"] = None
            if "design" in invalid:
                accepted.pop("solution_design", None)
                accepted.pop("design", None)
            request.update({"current_stage": mode, "work_mode": mode, "task_phase": "working",
                            "approval_kind": None, "accepted_documents": accepted,
                            "audit_complete": False, "validation_artifact": None})
            if task.get("waiting_question_message_id"):
                await connection.execute(
                    "UPDATE assistant.messages SET content_json=content_json || %s,updated_at=now() WHERE message_id=%s",
                    (Jsonb({"interaction_status": "superseded", "superseded_reason": "resume_sources_changed"}),
                     task["waiting_question_message_id"]),
                )
        status = "waiting_user" if not mode and previous == "waiting_user" and task.get("waiting_question_message_id") else "waiting_supervisor"
        request.update({"task_activation": "active", "resume_invalid_refs": invalid,
                        "resumed_at": datetime.now(timezone.utc).isoformat(),
                        "supervisor_context_epoch": int(request.get("supervisor_context_epoch") or 0) + 1,
                        "force_session_rotation": {"reason": "task_resume"}})
        updated = await (await connection.execute(
            """UPDATE assistant.runs SET status=%s,request_json=%s,ended_at=NULL,updated_at=now(),
                waiting_question_message_id=CASE WHEN %s THEN NULL ELSE waiting_question_message_id END,
                executor_json=executor_json-'config_snapshot_uri'-'config_bundle_hash'-'resolved_plan_uri'-'resolved_plan_sha256'-'prompt_bundle_uri'-'prompt_bundle_sha256'
                WHERE run_id=%s RETURNING *""",
            (status, Jsonb(json_compatible(request)), bool(mode), task_id),
        )).fetchone()
        await self._patch_meta(connection, project_id, {"active_task_id": task_id})
        if status == "waiting_supervisor":
            source_id = str(request.get("source_message_id") or task["trigger_message_id"])
            await self.repository._queue_supervision_locked(connection, updated, source_id, reason="task_resumed")
        return updated

    async def resume(self, task_id: str) -> dict[str, Any]:
        async with self.repository.database.connection() as connection:
            found = await (await connection.execute("SELECT project_id FROM assistant.runs WHERE run_id=%s", (task_id,))).fetchone()
            if found is None:
                raise KeyError("Task was not found")
            project_id = str(found["project_id"])
            self._project(project_id)
            await self._lock(connection, project_id)
            metadata = await self._meta(connection, project_id)
            task = await (await connection.execute("SELECT * FROM assistant.runs WHERE run_id=%s FOR UPDATE", (task_id,))).fetchone()
            updated = await self._resume_locked(connection, task, metadata)
            await connection.commit()
            return json_compatible(updated)

    async def _activate_work(self, connection, project_id: str, work, metadata: dict[str, Any]):
        content = dict(work["content_json"] or {})
        if content.get('bound_task_id'):
            task=await (await connection.execute('SELECT * FROM assistant.runs WHERE run_id=%s AND project_id=%s FOR UPDATE',
                (content['bound_task_id'],project_id))).fetchone()
            state=(task or {}).get('request_json') or {}
            if (not task or task.get('cancel_requested_at') or task['status'] in {'succeeded','failed','cancelled','dead_letter'}
                    or not state.get('task_scope_proposal',{}).get('confirmed') or not state.get('confirmed_turn_package_ref')):
                raise PermissionError('Queued work has no valid confirmed bound Task package')
            task=await (await connection.execute("""UPDATE assistant.runs SET status='queued',available_at=now(),
                request_json=request_json || %s WHERE run_id=%s RETURNING *""",
                (Jsonb({'task_activation':'active','task_phase':'working'}),task['run_id']))).fetchone()
            await connection.execute('UPDATE assistant.messages SET content_json=content_json || %s WHERE message_id=%s',
                (Jsonb({'work_status':'activated','task_id':str(task['run_id'])}),work['message_id']))
            await self._patch_meta(connection,project_id,{'active_task_id':str(task['run_id'])})
            return await self.repository._persist_scheduling(connection,task)
        task_id = str(uuid.uuid4())
        operation = content.get("task_kind") == "operation"
        source_ref = content["source_ref"]
        if operation:
            baseline = metadata["operational_baseline_ref"]
            if not isinstance(baseline, dict) or baseline.get("schema_version") != "operational-baseline.v1":
                raise ValueError("operation requires a published operational-baseline.v1")
            contract, design = baseline.get("task_contract_ref"), baseline.get("solution_design_ref")
            if not contract or not design:
                raise ValueError("operating baseline must include approved Requirements and Design")
            self.documents.read(project_id, contract)
            self.documents.read(project_id, design)
            accepted = {"task_contract": contract, "solution_design": design}
            accepted["operation_request"] = self.documents.write_revision(
                project_id=project_id, task_id=task_id, document_kind="operation-request", document_schema="operation-request.v1",
                payload={"goal": content["goal"], "report_window": content.get("report_window"),
                         "operational_baseline_ref": baseline, "source_refs": [source_ref]},
                status="ready", actor_type="kernel", actor_id="kernel", run_id=task_id,
                reason="Activate an operational work episode within the published baseline",
            )
        else:
            contract = self.documents.write_revision(
                project_id=project_id, task_id=task_id, document_kind="task-contract", document_schema="task-contract.v1",
                payload={"goal": content["goal"], "scope": {"included": [], "excluded": []}, "requirements": [],
                         "acceptance_criteria": [], "constraints": [], "source_refs": [source_ref]},
                status="draft", actor_type="kernel", actor_id="kernel", run_id=task_id, reason="Activate queued Project work",
            )
            accepted = {"task_contract": contract}
        project = self._project(project_id)
        mode = "operate" if operation else "requirements"
        request = {
            "task_protocol": "single-agent.v2", "task_root_run_id": task_id, "task_goal": content["goal"],
            "task_activation": "active", "task_kind": content["task_kind"], "task_turn_count": 0,
            "task_phase": "working", "current_stage": mode, "work_mode": mode, "state_revision": 1,
            "source_message_id": content["source_message_id"], "work_request_message_id": str(work["message_id"]),
            "accepted_documents": accepted, "candidate_only": True,
            "network_allowed": True, "max_task_turns": int((project.get("single_agent") or {}).get("max_task_turns") or 12),
            "audit_required": bool((project.get("single_agent") or {}).get("audit_required")),
            "design_required": bool((project.get("single_agent") or {}).get("design_required")),
            "validation_policy": (project.get("single_agent") or {}).get("validation_policy") or {},
            "multi_agent_enabled": bool((project.get("multi_agent") or {}).get("enabled")),
            "max_concurrent_subagents": min(max(int((project.get("multi_agent") or {}).get("max_concurrent_subagents") or 3), 1), 3),
            "permission_profile_name": "read-only", "report_window": content.get("report_window"),
            "work_idempotency_key": content["work_idempotency_key"],
            "force_session_rotation": {"reason": "new_queued_task"},
        }
        if operation:
            request.update({"operational_baseline_ref": baseline, "publication_state": "published",
                            "requirements_approval": baseline.get("requirements_approval"),
                            "design_approval": baseline.get("design_approval")})
        task = await (await connection.execute(
            """INSERT INTO assistant.runs(run_id,project_id,agent_key,trigger_message_id,run_type,status,
                conversation_key,request_json,executor_json,idempotency_key,queue_kind,mutex_key)
                VALUES (%s,%s,%s,%s,'chat','queued',%s,%s,'{}'::jsonb,%s,'interactive',%s) RETURNING *""",
            (task_id, project_id, f"project:{project_id}:manager", work["message_id"],
             f"project-task:{project_id}:{task_id}", Jsonb(json_compatible(request)),
             f"project-work-run:{work['message_id']}", f"engineering:{project_id}"),
        )).fetchone()
        await connection.execute(
            "UPDATE assistant.messages SET run_id=%s,content_json=content_json || %s,updated_at=now() WHERE message_id=%s",
            (task_id, Jsonb({"work_status": "activated", "task_id": task_id}), work["message_id"]),
        )
        await self._patch_meta(connection, project_id, {"active_task_id": task_id})
        return await self.repository._persist_scheduling(connection,task)

    async def activate_next(self, project_id: str) -> dict[str, Any]:
        self._project(project_id)
        async with self.repository.database.connection() as connection:
            await self._lock(connection, project_id)
            metadata = await self._meta(connection, project_id)
            if metadata.get("active_task_id"):
                completed = await (await connection.execute(
                    "SELECT status,request_json FROM assistant.runs WHERE run_id=%s AND project_id=%s",
                    (metadata["active_task_id"], project_id),
                )).fetchone()
                if completed and completed["status"] == "succeeded":
                    completed_request = completed["request_json"] or {}
                    if completed_request.get("task_kind") == "operation" and completed_request.get("report_window"):
                        await self._patch_meta(connection, project_id, {"last_report_window": completed_request["report_window"]})
            active = await self._active(connection, project_id)
            if active:
                if len(active) != 1:
                    raise RuntimeError("Project has multiple active foreground Tasks")
                urgent=await (await connection.execute("""SELECT * FROM assistant.messages WHERE project_id=%s
                    AND direction='system' AND message_type='work_request' AND content_json->>'work_status'='queued'
                    AND content_json->>'priority'='urgent' AND content_json->>'interrupts_task_id'=%s
                    AND content_json ? 'scope_confirmation_ref' AND content_json ? 'bound_task_id'
                    ORDER BY created_at,message_id LIMIT 1 FOR UPDATE""",(project_id,str(active[0]['run_id'])))).fetchone()
                if urgent:
                    try:
                        await self._suspend_locked(connection,active[0],metadata,'User-confirmed priority task',urgent['content_json']['scope_confirmation_ref'])
                    except PermissionError:
                        pass  # Existing queue retries at the next settled boundary.
                    else:
                        selected=await self._activate_work(connection,project_id,urgent,metadata)
                        await connection.commit()
                        return {'outcome':'activated','task':json_compatible(selected),'suspended_task_id':str(active[0]['run_id'])}
                await self._patch_meta(connection, project_id, {"active_task_id": str(active[0]["run_id"])})
                await connection.commit()
                return {"outcome": "already_active", "task": json_compatible(active[0])}
            work = await (await connection.execute(
                """SELECT * FROM assistant.messages WHERE project_id=%s AND direction='system'
                    AND message_type='work_request' AND content_json->>'schema_version'=%s
                    AND content_json->>'work_status'='queued'
                    AND (content_json->>'task_kind'<>'operation' OR %s)
                    ORDER BY CASE WHEN content_json->>'priority'='urgent' THEN 0 ELSE 1 END,
                    created_at,message_id FOR UPDATE""", (project_id, WORK_SCHEMA, bool(metadata.get("operational_baseline_ref"))),
            )).fetchall()
            urgent = next((row for row in work if row["content_json"].get("priority") == "urgent"), None)
            suspended = await (await connection.execute(
                """SELECT * FROM assistant.runs WHERE project_id=%s AND agent_key=%s AND run_type='chat'
                    AND request_json->>'task_activation'='suspended' AND status='blocked'
                    ORDER BY updated_at,created_at,run_id FOR UPDATE LIMIT 1""",
                (project_id, f"project:{project_id}:manager"),
            )).fetchone()
            if urgent:
                task = await self._activate_work(connection, project_id, urgent, metadata)
                outcome = "activated"
            elif suspended:
                task = await self._resume_locked(connection, suspended, metadata)
                outcome = "resumed"
            elif work:
                task = await self._activate_work(connection, project_id, work[0], metadata)
                outcome = "activated"
            else:
                await self._patch_meta(connection, project_id, {"active_task_id": None})
                await connection.commit()
                return {"outcome": "idle", "task": None}
            await connection.commit()
            return {"outcome": outcome, "task": json_compatible(task)}

    async def status(self, project_id: str) -> dict[str, Any]:
        self._project(project_id)
        async with self.repository.database.connection() as connection:
            tasks = await (await connection.execute(
                """SELECT * FROM assistant.runs WHERE project_id=%s AND agent_key=%s
                    AND run_type='chat' AND status=ANY(%s) ORDER BY created_at,run_id""",
                (project_id, f"project:{project_id}:manager", list(ACTIVE_STATUSES)),
            )).fetchall()
            queued = await (await connection.execute(
                """SELECT * FROM assistant.messages WHERE project_id=%s AND direction='system'
                    AND message_type='work_request' AND content_json->>'schema_version'=%s
                    AND content_json->>'work_status'='queued' ORDER BY created_at,message_id""", (project_id, WORK_SCHEMA),
            )).fetchall()
            row = await (await connection.execute("SELECT value FROM assistant.kernel_meta WHERE key=%s", (self.metadata_key(project_id),))).fetchone()
            return json_compatible({
                "project_id": project_id,
                "active": [task for task in tasks if (task["request_json"] or {}).get("task_activation", "active") == "active"],
                "suspended": [task for task in tasks if (task["request_json"] or {}).get("task_activation") == "suspended"],
                "queued": queued, "metadata": (row or {}).get("value") or {},
            })
