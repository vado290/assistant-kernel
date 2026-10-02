"""The two stage tools persist intent; only successful job completion publishes it."""
from __future__ import annotations

import uuid
from typing import Any

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field

from assistant_kernel.repositories import ControlPlaneRepository, canonical_sha256


class QuestionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=6000)
    required: bool = True


class StageIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str = Field(pattern="^(input|confirmation)$")
    document: str = Field(min_length=1, max_length=100000)
    questions: list[QuestionInput] = Field(min_length=1, max_length=8)
    next_action: str = Field(min_length=1, max_length=2000)


async def register_stage_intent(settings, *, job_id: str, worker_instance_id: str,
                                node_run_id: str, turn_key: str, intent: dict) -> dict:
    request = StageIntent.model_validate(intent).model_dump()
    if len({q["key"] for q in request["questions"]}) != len(request["questions"]):
        raise ValueError("question keys must be unique within a request")
    if any(not q["text"].strip() for q in request["questions"]):
        raise ValueError("question text cannot be blank")
    if request["kind"] == "confirmation" and len(request["questions"]) != 1:
        raise ValueError("submit one confirmation question for one exact document version")
    if not any(q["required"] for q in request["questions"]):
        raise ValueError("at least one question must be required")
    repo = ControlPlaneRepository(settings)
    digest = canonical_sha256(request)
    async with repo.database.connection() as c:
        await c.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (str(turn_key),))
        node = await (await c.execute(
            "SELECT n.node_id FROM assistant.node_runs n JOIN assistant.control_jobs j USING(job_id) "
            "WHERE n.node_run_id=%s AND j.job_id=%s AND j.worker_instance_id=%s "
            "AND j.status='running' AND j.lease_until>now() AND j.cancel_requested_at IS NULL "
            "AND n.status='running' FOR UPDATE OF j",
            (node_run_id, job_id, worker_instance_id),
        )).fetchone()
        if not node:
            raise ValueError("stage tool is not bound to a current leased stage job")
        if request["kind"] == "confirmation" and node["node_id"] != "requirements":
            raise ValueError("this stage cannot submit a requirements/design confirmation")
        old = await (await c.execute("SELECT * FROM assistant.stage_tool_requests WHERE turn_key=%s", (turn_key,))).fetchone()
        if old:
            if old["request_sha256"] != digest or str(old["node_run_id"]) != node_run_id:
                raise ValueError("one final stage intent per turn; conflicting duplicate request")
            request_id = str(old["request_id"])
        else:
            request_id = str(uuid.uuid4())
            await c.execute("INSERT INTO assistant.stage_tool_requests(request_id,node_run_id,job_id,worker_instance_id,turn_key,request_json,request_sha256) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (request_id, node_run_id, job_id, worker_instance_id, turn_key, Jsonb(request), digest))
        await c.commit()
    return {"request_id": request_id, "status": "draft", "next_action": "End this turn. Kernel publishes after successful completion."}


async def read_stage_intent(settings, turn_key: str) -> dict | None:
    async with ControlPlaneRepository(settings).database.connection() as c:
        return await (await c.execute("SELECT * FROM assistant.stage_tool_requests WHERE turn_key=%s", (turn_key,))).fetchone()
