"""Business stage records and approvals; one completed phase schedules the next job."""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from psycopg.types.json import Jsonb

from assistant_kernel.domain.project_build import BUILD_WORKFLOW_ID, Transition, after_stage, after_approval, allowed_actions
from assistant_kernel.repositories.database import AssistantDatabase
from assistant_kernel.repositories.control_plane import canonical_sha256, json_compatible


def definition_hash():
    return hashlib.sha256((Path(__file__).parents[1]/"domain"/"project_build.py").read_bytes()).hexdigest()


class StageRepository:
    def __init__(self, settings):
        self.settings = settings
        self.database = AssistantDatabase(settings)

    async def start(self, *, project_id, request_data, idempotency_key, flow="build"):
        if flow not in {"build", "optimization"}:
            raise ValueError("unknown business flow")
        route = request_data.get("delivery")
        if route is not None:
            if not isinstance(route,dict) or route.get("channel") != "feishu" or not route.get("chat_id") or not request_data.get("user_id"):
                raise ValueError("Feishu delivery requires channel, chat_id and the expected user_id")
        workflow_id = BUILD_WORKFLOW_ID if flow == "build" else "system-project-optimization"
        scope = "project_build" if flow == "build" else "project_optimization"
        await self.database.ensure_project(project_id)
        digest = canonical_sha256({"project_id":project_id,"request":request_data})
        async with self.database.connection() as c:
            await c.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (f"idempotency:{scope}:{idempotency_key}",))
            previous = await (await c.execute("SELECT * FROM assistant.idempotency_records WHERE scope=%s AND idempotency_key=%s", (scope,idempotency_key))).fetchone()
            if previous:
                if previous["request_sha256"] != digest:
                    raise ValueError("idempotency key belongs to a different build request")
                return previous["response_json"]
            await c.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (f"workflow-version:{project_id}:{workflow_id}",))
            code_hash = definition_hash()
            version = await (await c.execute("""
                SELECT * FROM assistant.workflow_versions WHERE project_id=%s AND workflow_id=%s
                  AND definition_sha256=%s AND status='active' AND spec_json->'executor'->>'type'='python_stages'
            """, (project_id, workflow_id, code_hash))).fetchone()
            if not version:
                await c.execute("UPDATE assistant.workflow_versions SET status='retired',retired_at=now() WHERE project_id=%s AND workflow_id=%s AND status='active'", (project_id, workflow_id))
                version = await (await c.execute("""
                    INSERT INTO assistant.workflow_versions(workflow_version_id,project_id,workflow_id,version,
                        definition_path,definition_sha256,spec_json,status,activated_at)
                    SELECT %s,%s,%s,COALESCE(MAX(version),0)+1,'builtin://project-build/stages-v1',%s,
                        '{"executor":{"type":"python_stages"}}'::jsonb,'active',now()
                    FROM assistant.workflow_versions WHERE project_id=%s AND workflow_id=%s RETURNING *
                """, (str(uuid.uuid4()),project_id,workflow_id,code_hash,project_id,workflow_id))).fetchone()
                await c.execute("""
                    INSERT INTO assistant.workflow_current_versions(project_id,workflow_id,workflow_version_id)
                    VALUES (%s,%s,%s) ON CONFLICT(project_id,workflow_id) DO UPDATE
                    SET workflow_version_id=EXCLUDED.workflow_version_id,pointer_version=assistant.workflow_current_versions.pointer_version+1,updated_at=now()
                """, (project_id,workflow_id,version["workflow_version_id"]))
            task_id,run_id = str(uuid.uuid4()),str(uuid.uuid4())
            await c.execute("""INSERT INTO assistant.tasks(task_id,project_id,workflow_version_id,task_type,status,request_json)
                VALUES (%s,%s,%s,%s,'queued',%s)""", (task_id,project_id,version["workflow_version_id"],scope,Jsonb(request_data)))
            run = await (await c.execute("""INSERT INTO assistant.workflow_runs(workflow_run_id,task_id,workflow_version_id,status,input_json)
                VALUES (%s,%s,%s,'queued',%s) RETURNING *""", (run_id,task_id,version["workflow_version_id"],Jsonb(request_data)))).fetchone()
            state = {"project_id":project_id,"task_id":task_id,"request":request_data,"rework_attempt":0,"max_rework_attempts":2,"flow":flow}
            if flow == "optimization":
                await c.execute("INSERT INTO assistant.change_requests(change_request_id,project_id,task_id,target_type,target_id,impact,proposal,evidence_refs) VALUES (%s,%s,%s,'optimization',%s,'medium',%s,%s)",
                    (request_data['change_request_id'],project_id,task_id,run_id,Jsonb({'status':'analyzing','baseline':request_data['baseline'],'feedback_ids':request_data.get('feedback_ids',[])}),Jsonb(request_data.get('feedback_ids',[]))))
            job_id = await self._enqueue(c,run,"requirements",state)
            result = {"task_id":task_id,"workflow_run_id":run_id,"workflow_version_id":str(version["workflow_version_id"]),"job_id":job_id,"status":"queued"}
            await c.execute("""INSERT INTO assistant.idempotency_records(idempotency_id,scope,idempotency_key,request_sha256,response_status,response_json)
                VALUES (%s,%s,%s,%s,202,%s)""", (str(uuid.uuid4()),scope,idempotency_key,digest,Jsonb(result)))
            await c.commit()
            return result

    @staticmethod
    async def _enqueue(c,run,stage,state):
        job_id,node_id = str(uuid.uuid4()),str(uuid.uuid4())
        attempt = int(run["current_stage_attempt"]) + 1
        await c.execute("""INSERT INTO assistant.control_jobs(job_id,job_type,task_id,workflow_run_id,payload)
            VALUES (%s,'stage_run',%s,%s,%s)""", (job_id,run["task_id"],run["workflow_run_id"],Jsonb({"node_run_id":node_id})))
        await c.execute("""INSERT INTO assistant.node_runs(node_run_id,workflow_run_id,node_id,attempt,status,input_json,job_id)
            VALUES (%s,%s,%s,%s,'queued',%s,%s)""", (node_id,run["workflow_run_id"],stage,attempt,Jsonb(state),job_id))
        await c.execute("""UPDATE assistant.workflow_runs SET current_stage=%s,current_stage_attempt=%s,status='queued',
            output_json=%s,status_version=status_version+1,ended_at=NULL WHERE workflow_run_id=%s""", (stage,attempt,Jsonb(state),run["workflow_run_id"]))
        await c.execute("UPDATE assistant.tasks SET status='queued',status_version=status_version+1,updated_at=now() WHERE task_id=%s", (run["task_id"],))
        return job_id

    async def context(self,job):
        async with self.database.connection() as c:
            return await (await c.execute("""
                SELECT r.*,t.project_id,t.request_json,n.node_run_id,n.node_id,n.attempt stage_attempt,
                    n.status stage_status,n.input_json stage_input,n.output_json stage_output,n.prompt_binding,v.definition_sha256
                FROM assistant.node_runs n JOIN assistant.workflow_runs r USING(workflow_run_id)
                JOIN assistant.tasks t ON t.task_id=r.task_id JOIN assistant.workflow_versions v ON v.workflow_version_id=r.workflow_version_id
                WHERE n.job_id=%s AND n.node_run_id=%s AND r.workflow_run_id=%s AND r.task_id=%s
            """, (job["job_id"],(job.get("payload") or {}).get("node_run_id"),job["workflow_run_id"],job["task_id"]))).fetchone()

    @staticmethod
    async def fence(c,job):
        current = await (await c.execute("""SELECT * FROM assistant.control_jobs WHERE job_id=%s
            AND worker_instance_id=%s AND status='running' AND lease_until>now() FOR UPDATE""", (job["job_id"],job["worker_instance_id"]))).fetchone()
        if not current:
            raise RuntimeError("stage job lease lost")
        return current

    async def begin(self,context,job):
        async with self.database.connection() as c:
            current = await self.fence(c,job)
            if current["cancel_requested_at"]:
                return False
            if context["definition_sha256"] != definition_hash():
                raise ValueError("build definition changed; explicit business-version migration required")
            if (context["stage_status"] not in {"queued","running"} or context["status"] not in {"queued","running"}
                    or context["stage_attempt"] != context["current_stage_attempt"]):
                raise ValueError("stage is no longer executable")
            await c.execute("UPDATE assistant.node_runs SET status='running',started_at=COALESCE(started_at,now()) WHERE node_run_id=%s", (context["node_run_id"],))
            await c.execute("UPDATE assistant.workflow_runs SET status='running',started_at=COALESCE(started_at,now()) WHERE workflow_run_id=%s", (context["workflow_run_id"],))
            await c.execute("UPDATE assistant.tasks SET status='running',updated_at=now() WHERE task_id=%s", (context["task_id"],))
            await c.commit()
            return True

    async def finish(self,context,job,artifact):
        async with self.database.connection() as c:
            current = await self.fence(c,job)
            run = await (await c.execute("SELECT * FROM assistant.workflow_runs WHERE workflow_run_id=%s FOR UPDATE", (context["workflow_run_id"],))).fetchone()
            transition = (Transition(context["stage_input"],terminal="cancelled") if current["cancel_requested_at"]
                          else after_stage(context["node_id"],context["stage_input"],artifact))
            result = await self._apply(c,run,transition,context)
            await c.execute("""UPDATE assistant.node_runs SET status=%s,output_json=%s,error_json=NULL,ended_at=now() WHERE node_run_id=%s""",
                ("cancelled" if current["cancel_requested_at"] else "succeeded",Jsonb({"artifact":artifact,"job_result":result}),context["node_run_id"]))
            await c.execute("UPDATE assistant.workflow_runs SET last_job_id=%s,last_job_result=%s,error_json=NULL WHERE workflow_run_id=%s", (job["job_id"],Jsonb(result),run["workflow_run_id"]))
            await c.commit()
            return result

    async def _apply(self,c,run,transition,context):
        change_id = (transition.state.get("request") or {}).get("change_request_id")
        if change_id:
            await c.execute("UPDATE assistant.change_requests SET proposal=proposal || %s::jsonb WHERE change_request_id=%s AND task_id=%s",
                (Jsonb(json_compatible({"workflow_run_id":str(run["workflow_run_id"]),"analysis_artifact":transition.state.get("requirements_artifact"),
                    "implementation_artifact":transition.state.get("implementation_artifact"),"stage_status":transition.terminal or transition.approval or transition.next_stage})), change_id,run["task_id"]))
        if transition.next_stage:
            job_id = await self._enqueue(c,run,transition.next_stage,transition.state)
            return {"status":"queued","job_id":job_id,"next_stage":transition.next_stage}
        if transition.approval:
            kind = transition.approval
            approval_id = str(uuid.uuid5(uuid.NAMESPACE_URL,f"stage-approval:{context['node_run_id']}"))
            payload = {"approval_id":approval_id,"task_id":str(run["task_id"]),"workflow_run_id":str(run["workflow_run_id"]),
                "project_id":context["project_id"],"approval_type":kind,"allowed_actions":allowed_actions(kind),
                "node_run_id":str(context["node_run_id"]),"stage_attempt":context["stage_attempt"],
                "stage":context["node_id"],"delivery":(context["request_json"] or {}).get("delivery"),
                "source_conversation_id":(context["request_json"] or {}).get("source_conversation_id")}
            artifact = transition.state.get(f"{context['node_id']}_artifact") or {}
            if kind == "acceptance":
                payload.update({name:transition.state.get(f"{name}_artifact") for name in ("implementation","review")})
            else:
                payload["artifact"] = artifact
            await c.execute("""INSERT INTO assistant.approval_requests(approval_id,project_id,task_id,workflow_run_id,node_run_id,
                approval_type,payload,required_user_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (approval_id,context["project_id"],run["task_id"],run["workflow_run_id"],context["node_run_id"],kind,Jsonb(payload),(context["request_json"] or {}).get("user_id")))
            from assistant_kernel.repositories.interactions import attach_questions
            await attach_questions(c, approval_id, payload, transition.state, artifact, kind)
            await c.execute("UPDATE assistant.approval_requests SET payload=%s WHERE approval_id=%s", (Jsonb(json_compatible(payload)),approval_id))
            if artifact.get("stage_request_id"):
                await c.execute("UPDATE assistant.stage_tool_requests SET status='published' WHERE request_id=%s AND node_run_id=%s", (artifact["stage_request_id"],context["node_run_id"]))
            await self._outbox(c,approval_id,"approval","approval.requested",payload,f"approval.requested:{approval_id}")
            status = "waiting_user"
            result = {"status":status,"approval_id":approval_id}
        else:
            status = transition.terminal
            if status not in {"succeeded","failed","cancelled"}:
                raise ValueError("transition has no next action")
            result = {"status":status,"result":transition.state}
            if transition.state.get("flow") == "optimization":
                result["publication_status"] = "candidate_accepted_not_published" if status == "succeeded" else "not_published"
            await c.execute("UPDATE assistant.approval_requests SET status='superseded' WHERE workflow_run_id=%s AND status='pending'", (run["workflow_run_id"],))
            await self._outbox(c,str(run["workflow_run_id"]),"workflow_run",f"project_build.{status}",
                {"task_id":str(run["task_id"]),"workflow_run_id":str(run["workflow_run_id"]),
                 "delivery":(transition.state.get("request") or {}).get("delivery"),**result},f"build.terminal:{run['workflow_run_id']}")
        await c.execute("""UPDATE assistant.workflow_runs SET status=%s,output_json=%s,status_version=status_version+1,
            ended_at=CASE WHEN %s='waiting_user' THEN NULL ELSE now() END WHERE workflow_run_id=%s""", (status,Jsonb(transition.state),status,run["workflow_run_id"]))
        await c.execute("UPDATE assistant.tasks SET status=%s,result_json=%s,status_version=status_version+1,updated_at=now() WHERE task_id=%s", (status,Jsonb(transition.state),run["task_id"]))
        return result

    @staticmethod
    async def _outbox(c,aggregate_id,kind,event,payload,key):
        await c.execute("""INSERT INTO assistant.outbox_events(outbox_event_id,aggregate_type,aggregate_id,event_type,payload,dedupe_key)
            VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING""",
            (str(uuid.uuid4()),kind,aggregate_id,event,Jsonb(json_compatible(payload)),key))

    async def respond(self,*,approval_id,answer,decided_by,idempotency_key,decision="answer"):
        json.dumps(answer,allow_nan=False)
        async with self.database.connection() as probe:
            stored = await (await probe.execute("SELECT payload FROM assistant.approval_requests WHERE approval_id=%s",(approval_id,))).fetchone()
        if stored and stored["payload"].get("protocol_version") == 7:
            from assistant_kernel.repositories.interactions import InteractionRepository
            return await InteractionRepository(self.settings).respond(approval_id=approval_id,answer=answer,decided_by=decided_by,idempotency_key=idempotency_key)
        async with self.database.connection() as c:
            await c.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (f"stage-answer:{idempotency_key}",))
            approval = await (await c.execute("SELECT * FROM assistant.approval_requests WHERE approval_id=%s", (approval_id,))).fetchone()
            if not approval:
                raise ValueError("approval not found")
            if approval["required_user_id"] and approval["required_user_id"] != decided_by:
                raise PermissionError("approval actor does not match required user")
            run = await (await c.execute("SELECT * FROM assistant.workflow_runs WHERE workflow_run_id=%s FOR UPDATE", (approval["workflow_run_id"],))).fetchone()
            previous = await (await c.execute("SELECT * FROM assistant.approval_decisions WHERE approval_id=%s OR idempotency_key=%s", (approval_id,idempotency_key))).fetchall()
            if previous:
                row = previous[0]
                old_answer = row["answer_json"] if row["answer_json"] is not None else {"action":row["decision"],"edited_payload":row["edited_payload"] or {}}
                if (len(previous)!=1 or str(row["approval_id"])!=approval_id or row["decided_by"]!=decided_by
                        or canonical_sha256(old_answer)!=canonical_sha256(answer)):
                    raise ValueError("conflicting duplicate approval response")
                return {"approval_id":approval_id,"job_id":row["next_job_id"],"duplicate":True}
            if not run or run["status"]!="waiting_user" or approval["status"]!="pending" or not approval["node_run_id"]:
                raise ValueError("approval is no longer pending")
            node = await (await c.execute("SELECT * FROM assistant.node_runs WHERE node_run_id=%s", (approval["node_run_id"],))).fetchone()
            if (not node or node["workflow_run_id"]!=run["workflow_run_id"] or node["attempt"]!=run["current_stage_attempt"] or node["status"]!="succeeded"):
                raise ValueError("stale approval stage")
            if approval["expires_at"] and approval["expires_at"] < datetime.now(timezone.utc):
                raise ValueError("approval expired")
            transition = after_approval(approval["approval_type"],run["output_json"] or {},answer)
            await c.execute("UPDATE assistant.approval_requests SET status='decided',decided_at=now() WHERE approval_id=%s", (approval_id,))
            result = await self._apply(c,run,transition,{})
            job_id = result.get("job_id")
            await c.execute("""INSERT INTO assistant.approval_decisions(approval_decision_id,approval_id,decision,edited_payload,
                decided_by,idempotency_key,answer_json,next_job_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (str(uuid.uuid4()),approval_id,decision,Jsonb(answer.get("edited_payload")),decided_by,idempotency_key,Jsonb(answer),job_id))
            if job_id:
                await c.execute("UPDATE assistant.approval_requests SET next_job_enqueued_at=now() WHERE approval_id=%s", (approval_id,))
            await c.commit()
            return {"approval_id":approval_id,"job_id":job_id,"status":result["status"],"duplicate":False}

    async def retry_failed(self,run_id,idempotency_key):
        async with self.database.connection() as c:
            await c.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (f"stage-retry:{idempotency_key}",))
            previous = await (await c.execute("SELECT * FROM assistant.idempotency_records WHERE scope='stage_retry' AND idempotency_key=%s", (idempotency_key,))).fetchone()
            if previous:
                if previous["request_sha256"]!=canonical_sha256(run_id):
                    raise ValueError("retry key belongs to another run")
                return previous["response_json"]
            run = await (await c.execute("SELECT * FROM assistant.workflow_runs WHERE workflow_run_id=%s FOR UPDATE", (run_id,))).fetchone()
            if not run or run["status"]!="failed" or not run["error_json"]:
                raise ValueError("only infrastructure-failed stage runs may be retried")
            node = await (await c.execute("""SELECT n.*,j.status job_status FROM assistant.node_runs n JOIN assistant.control_jobs j USING(job_id)
                WHERE n.workflow_run_id=%s AND n.attempt=%s AND n.status='failed'""", (run_id,run["current_stage_attempt"]))).fetchone()
            if not node or node["job_status"]!="dead_letter":
                raise ValueError("no exhausted stage job to retry")
            job_id = str(uuid.uuid4())
            await c.execute("INSERT INTO assistant.control_jobs(job_id,job_type,task_id,workflow_run_id,payload) VALUES (%s,'stage_run',%s,%s,%s)", (job_id,run["task_id"],run_id,Jsonb({"node_run_id":str(node["node_run_id"])})))
            await c.execute("UPDATE assistant.node_runs SET job_id=%s,status='queued',ended_at=NULL WHERE node_run_id=%s", (job_id,node["node_run_id"]))
            await c.execute("UPDATE assistant.workflow_runs SET status='queued',ended_at=NULL WHERE workflow_run_id=%s", (run_id,))
            await c.execute("UPDATE assistant.tasks SET status='queued',updated_at=now() WHERE task_id=%s", (run["task_id"],))
            result = {"workflow_run_id":run_id,"job_id":job_id,"status":"queued"}
            await c.execute("INSERT INTO assistant.idempotency_records(idempotency_id,scope,idempotency_key,request_sha256,response_status,response_json) VALUES (%s,'stage_retry',%s,%s,202,%s)", (str(uuid.uuid4()),idempotency_key,canonical_sha256(run_id),Jsonb(result)))
            await c.commit()
            return result

    @staticmethod
    async def finish_aborted_job(c,job_id,status,error=None):
        node = await (await c.execute("""UPDATE assistant.node_runs n SET status=%s,ended_at=now(),error_json=%s
            FROM assistant.control_jobs j WHERE j.job_id=%s AND n.job_id=j.job_id AND j.job_type='stage_run'
              AND j.status=%s AND n.status IN ('queued','running') RETURNING n.workflow_run_id,n.node_run_id""",
            (status,Jsonb(error) if error else None,job_id,"dead_letter" if status=="failed" else "cancelled"))).fetchone()
        if not node:
            return False
        run = await (await c.execute("UPDATE assistant.workflow_runs SET status=%s,error_json=%s,ended_at=now() WHERE workflow_run_id=%s RETURNING *", (status,Jsonb(error) if error else None,node["workflow_run_id"]))).fetchone()
        await c.execute("UPDATE assistant.tasks SET status=%s,status_version=status_version+1,updated_at=now() WHERE task_id=%s", (status,run["task_id"]))
        await c.execute("UPDATE assistant.approval_requests SET status='superseded' WHERE workflow_run_id=%s AND status='pending'", (run["workflow_run_id"],))
        await StageRepository._outbox(c,str(run["workflow_run_id"]),"workflow_run",f"project_build.{status}",
            {"task_id":str(run["task_id"]),"workflow_run_id":str(run["workflow_run_id"]),"status":status,"error":error},f"stage.abort:{job_id}")
        return True
