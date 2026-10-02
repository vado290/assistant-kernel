"""Questions and answers extend the existing approval, stage and outbox authority."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from psycopg.types.json import Jsonb

from assistant_kernel.domain.project_build import Transition, after_approval
from assistant_kernel.repositories import ControlPlaneRepository, canonical_sha256, json_compatible


async def attach_questions(c, approval_id, payload, state, artifact, kind):
    intent = artifact.get("interaction_request") or {}
    questions = intent.get("questions") or [{"key": "confirm-document", "text":
        f"是否确认本次{kind}阶段成果并按流程继续？如需修改，请写明修改意见。", "required": True}]
    rows = []
    previous = state.get("question_history", [])
    for i, q in enumerate(questions):
        question_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{approval_id}:{i}"))
        supersedes = next((p["question_id"] for p in reversed(previous)
                           if p.get("question_key") == q["key"] and p.get("stage") == payload["stage"]), None)
        text = q["text"]
        if kind != "stage_input":
            text += f"\n成果版本：{artifact.get('artifact_version_id', '见阶段记录')}\nSHA-256：{artifact.get('content_sha256', '见阶段记录')}"
        row = {"question_id": question_id, "key": q["key"], "text": text,
               "required": q.get("required", True), "supersedes_question_id": supersedes}
        await c.execute("INSERT INTO assistant.interaction_questions(question_id,approval_id,ordinal,question_key,question_text,required,supersedes_question_id,artifact_version_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (question_id, approval_id, i, q["key"], text, row["required"], supersedes, artifact.get("artifact_version_id")))
        rows.append(row)
    payload.update({"protocol_version": 7, "interaction_id": approval_id, "questions": rows,
                    "interaction_kind": "input" if kind == "stage_input" else "confirmation"})


class InteractionRepository:
    def __init__(self, settings):
        self.settings = settings
        self.repository = ControlPlaneRepository(settings)

    async def get(self, approval_id):
        async with self.repository.database.connection() as c:
            a = await (await c.execute("SELECT * FROM assistant.approval_requests WHERE approval_id=%s", (approval_id,))).fetchone()
            if not a:
                return None
            a["questions"] = await (await c.execute(
                "SELECT q.*,x.answer_json,x.answered_by,x.source_json,x.created_at answered_at FROM assistant.interaction_questions q "
                "LEFT JOIN assistant.interaction_answers x USING(question_id) WHERE q.approval_id=%s ORDER BY ordinal", (approval_id,))).fetchall()
            return a

    async def respond(self, *, approval_id, answer, decided_by, idempotency_key, source=None):
        from assistant_kernel.repositories.stages import StageRepository
        if not isinstance(answer, dict):
            raise ValueError("answer must be an object")
        normalized = json_compatible(answer)
        digest = canonical_sha256({"approval_id": approval_id, "answer": normalized, "actor": decided_by})
        source = json_compatible(source or {})
        stages = StageRepository(self.settings)
        async with self.repository.database.connection() as c:
            await c.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (f"interaction-answer:{idempotency_key}",))
            receipt = await (await c.execute("SELECT * FROM assistant.idempotency_records WHERE scope='interaction_answer' AND idempotency_key=%s", (idempotency_key,))).fetchone()
            if receipt:
                if receipt["request_sha256"] != digest:
                    raise ValueError("conflicting duplicate interaction response")
                return {**receipt["response_json"], "duplicate": True}
            a = await (await c.execute("SELECT * FROM assistant.approval_requests WHERE approval_id=%s", (approval_id,))).fetchone()
            if not a or a["payload"].get("protocol_version") != 7:
                raise ValueError("interaction not found")
            if a["required_user_id"] and decided_by != a["required_user_id"]:
                raise PermissionError("interaction actor does not match required user")
            run = await (await c.execute("SELECT * FROM assistant.workflow_runs WHERE workflow_run_id=%s FOR UPDATE", (a["workflow_run_id"],))).fetchone()
            # Re-read under the run lock: simultaneous answers must see the committed state.
            a = await (await c.execute("SELECT * FROM assistant.approval_requests WHERE approval_id=%s", (approval_id,))).fetchone()
            if a["status"] != "pending" or run["status"] != "waiting_user":
                old = await (await c.execute("SELECT * FROM assistant.approval_decisions WHERE approval_id=%s", (approval_id,))).fetchone()
                if old and old["decided_by"] == decided_by and old["answer_json"] == normalized:
                    return {"approval_id": approval_id, "job_id": str(old["next_job_id"]) if old["next_job_id"] else None, "duplicate": True}
                if old:
                    raise ValueError("conflicting duplicate interaction response")
                raise ValueError("interaction is no longer pending")
            node = await (await c.execute("SELECT * FROM assistant.node_runs WHERE node_run_id=%s", (a["node_run_id"],))).fetchone()
            if not node or node["attempt"] != run["current_stage_attempt"] or node["status"] != "succeeded":
                raise ValueError("stale interaction stage")
            if a["expires_at"] and a["expires_at"] < datetime.now(timezone.utc):
                raise ValueError("interaction expired")
            questions = await (await c.execute("SELECT * FROM assistant.interaction_questions WHERE approval_id=%s ORDER BY ordinal", (approval_id,))).fetchall()
            lookup = {str(q["question_id"]): q for q in questions}
            action = normalized.get("action", "answer")
            if a["approval_type"] == "stage_input":
                if action not in {"answer", "reject"}:
                    raise ValueError("clarification answers cannot approve the next stage")
                entries = normalized.get("answers", [])
                if action != "reject" and not entries:
                    raise ValueError("at least one question answer is required")
            else:
                if action not in a["payload"]["allowed_actions"]:
                    raise ValueError("invalid confirmation action")
                # Preserve the complete user's response, not just a derived approve flag.
                entries = [{"question_id": str(questions[0]["question_id"]), "value": normalized}]
            if not isinstance(entries, list) or len(entries) > len(questions):
                raise ValueError("invalid answer list")
            seen = set()
            for entry in entries:
                qid = str(entry.get("question_id", ""))
                if qid not in lookup or qid in seen or "value" not in entry:
                    raise ValueError("answer must reference one distinct question in this interaction")
                seen.add(qid)
                old = await (await c.execute("SELECT * FROM assistant.interaction_answers WHERE question_id=%s", (qid,))).fetchone()
                if old:
                    if old["answer_json"] != entry["value"] or old["answered_by"] != decided_by:
                        raise ValueError("conflicting answer; request a revised question instead")
                else:
                    await c.execute("INSERT INTO assistant.interaction_answers(question_id,answer_json,answered_by,source_json) VALUES (%s,%s,%s,%s)",
                        (qid, Jsonb(entry["value"]), decided_by, Jsonb(source)))
            pairs = await (await c.execute("SELECT q.*,x.answer_json,x.answered_by,x.source_json,x.created_at answered_at FROM assistant.interaction_questions q LEFT JOIN assistant.interaction_answers x USING(question_id) WHERE q.approval_id=%s ORDER BY ordinal", (approval_id,))).fetchall()
            unanswered = [str(p["question_id"]) for p in pairs if p["required"] and p["answered_at"] is None]
            if unanswered and action != "reject":
                result = {"approval_id": approval_id, "status": "waiting_user", "unanswered_question_ids": unanswered, "job_id": None, "duplicate": False}
            else:
                state = dict(run["output_json"] or {})
                history = [{"question_id": str(p["question_id"]), "question_key": p["question_key"],
                    "question_text": p["question_text"], "answer": p["answer_json"], "answered": p["answered_at"] is not None,
                    "source": p["source_json"], "answered_at": p["answered_at"], "stage": a["payload"]["stage"],
                    "approval_id": approval_id, "artifact_version_id": str(p["artifact_version_id"]) if p["artifact_version_id"] else None} for p in pairs]
                state["question_history"] = [*state.get("question_history", []), *json_compatible(history)]
                if a["approval_type"] == "stage_input":
                    transition = Transition(state, terminal="cancelled") if action == "reject" else Transition(state, next_stage=a["payload"]["stage"])
                else:
                    effective = dict(normalized)
                    if action == "approve" and (effective.get("edited_payload") or effective.get("feedback")):
                        effective["action"] = "edit"
                        effective.setdefault("edited_payload", {"feedback": effective.get("feedback")})
                    transition = after_approval(a["approval_type"], state, effective)
                await c.execute("UPDATE assistant.approval_requests SET status='decided',decided_at=now() WHERE approval_id=%s", (approval_id,))
                result = await stages._apply(c, run, transition, {})
                job_id = result.get("job_id")
                await c.execute("INSERT INTO assistant.approval_decisions(approval_decision_id,approval_id,decision,edited_payload,decided_by,idempotency_key,answer_json,next_job_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    (str(uuid.uuid4()), approval_id, action, Jsonb(normalized.get("edited_payload")), decided_by, idempotency_key, Jsonb(normalized), job_id))
                await c.execute("UPDATE assistant.approval_requests SET next_job_enqueued_at=CASE WHEN %s::uuid IS NULL THEN NULL ELSE now() END WHERE approval_id=%s", (job_id, approval_id))
                result = {**result, "approval_id": approval_id, "job_id": job_id, "duplicate": False}
            await c.execute("INSERT INTO assistant.idempotency_records(idempotency_id,scope,idempotency_key,request_sha256,response_status,response_json) VALUES (%s,'interaction_answer',%s,%s,202,%s)",
                (str(uuid.uuid4()), idempotency_key, digest, Jsonb(json_compatible(result))))
            await c.commit()
            return result
