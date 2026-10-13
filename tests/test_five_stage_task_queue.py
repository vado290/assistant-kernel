from __future__ import annotations

import asyncio
import hashlib
import uuid

import pytest
import yaml
from psycopg.types.json import Jsonb

from test_core4 import core_settings
from assistant_kernel.repositories import CoreRepository
from assistant_kernel.services.document_runtime import TaskDocumentService, resource_ref
from assistant_kernel.services.project_directory import default_work_modes
from assistant_kernel.services.task_queue import ProjectTaskQueue
from assistant_kernel.storage import ProjectRuntimeManager


def prepare_project(settings):
    root = ProjectRuntimeManager(settings.data_root).ensure("demo").vault_root
    (root / "project.yaml").write_text(yaml.safe_dump({
        "project_id": "demo", "layout_version": 2, "task_protocol": "single-agent.v2",
        "single_agent": {"design_required": True}, "work_modes": default_work_modes(),
    }), encoding="utf-8")
    return root


async def source(service, text="User request", project_id="demo"):
    identity = str(uuid.uuid4())
    async with service.repository.database.connection() as connection:
        await connection.execute(
            """INSERT INTO assistant.messages(message_id,project_id,channel,chat_id,user_id,
                direction,message_type,content_json) VALUES (%s,%s,'discord','chat','user','inbound','text',%s)""",
            (identity, project_id, Jsonb({"text": text})),
        )
        await connection.commit()
    return identity


async def edit_task(service, task_id, *, status=None, patch=None):
    async with service.repository.database.connection() as connection:
        await connection.execute(
            "UPDATE assistant.runs SET status=COALESCE(%s,status),request_json=request_json || %s WHERE run_id=%s",
            (status, Jsonb(patch or {}), task_id),
        )
        await connection.commit()


async def test_queue_is_durable_idempotent_and_does_not_create_pm_run(core_settings):
    prepare_project(core_settings)
    service = ProjectTaskQueue(core_settings)
    source_id = await source(service)
    request = await service.queue("demo", "Build feature", source_id, idempotency_key="source-one")
    retry = await service.queue("demo", "Build feature", source_id, idempotency_key="source-one")
    assert request["message_id"] == retry["message_id"]
    assert request["direction"] == "system"
    assert request["content_json"]["source_ref"]["id"] == source_id
    assert len((await service.status("demo"))["queued"]) == 1
    assert await service.repository.list_runs("demo") == []
    with pytest.raises(ValueError, match="different content"):
        await service.queue("demo", "Different goal", source_id, idempotency_key="source-one")
    other = await source(service, project_id="other")
    with pytest.raises(PermissionError, match="original user"):
        await service.queue("demo", "Cross-project", other)


async def test_parallel_activation_starts_only_one_foreground_task(core_settings):
    prepare_project(core_settings)
    service = ProjectTaskQueue(core_settings)
    work = await service.queue("demo", "First feature", await source(service))
    await service.queue("demo", "Second feature", await source(service))
    results = await asyncio.gather(service.activate_next("demo"), service.activate_next("demo"))
    assert sorted(result["outcome"] for result in results) == ["activated", "already_active"]
    state = await service.status("demo")
    assert len(state["active"]) == 1 and len(state["queued"]) == 1
    task = state["active"][0]
    assert task["trigger_message_id"] == work["message_id"]
    assert task["request_json"]["source_message_id"] == work["content_json"]["source_message_id"]
    assert task["mutex_key"] == "engineering:demo"
    assert state["metadata"]["active_task_id"] == task["run_id"]
    assert task["request_json"]["current_stage"] == "requirements"
    TaskDocumentService(core_settings).read("demo", task["request_json"]["accepted_documents"]["task_contract"])


async def test_urgent_then_suspended_then_normal_and_resume_stays_supervised(core_settings):
    prepare_project(core_settings)
    service = ProjectTaskQueue(core_settings)
    await service.queue("demo", "Current work", await source(service))
    first = (await service.activate_next("demo"))["task"]
    checkpoint = {"goal": "Current work", "completed": ["investigation"], "pending": ["design"]}
    await edit_task(service, first["run_id"], status="waiting_supervisor", patch={"task_checkpoint": checkpoint})
    await service.queue("demo", "Normal later", await source(service))
    await service.queue("demo", "Urgent incident", await source(service), priority="urgent")
    await service.suspend(first["run_id"], "Urgent incident", incident_ref={"id": "incident-one"})
    urgent = (await service.activate_next("demo"))["task"]
    assert urgent["request_json"]["task_goal"] == "Urgent incident"
    with pytest.raises(PermissionError, match="another Project"):
        await service.resume(first["run_id"])
    await edit_task(service, urgent["run_id"], status="succeeded")
    resumed = await service.activate_next("demo")
    assert resumed["outcome"] == "resumed"
    assert resumed["task"]["run_id"] == first["run_id"]
    assert resumed["task"]["status"] == "waiting_supervisor"
    assert resumed["task"]["request_json"]["task_checkpoint"] == checkpoint
    assert len((await service.status("demo"))["queued"]) == 1
    runs = await service.repository.list_runs("demo")
    assert any(run["agent_key"] == "project:demo:supervisor" for run in runs)
    await edit_task(service, first["run_id"], status="succeeded")
    normal = (await service.activate_next("demo"))["task"]
    assert normal["request_json"]["task_goal"] == "Normal later"


async def test_waiting_question_draft_and_approval_survive_suspend_resume(core_settings):
    prepare_project(core_settings)
    service = ProjectTaskQueue(core_settings)
    await service.queue("demo", "Clarify feature", await source(service))
    task = (await service.activate_next("demo"))["task"]
    contract = task["request_json"]["accepted_documents"]["task_contract"]
    approval = {"message_id": str(uuid.uuid4()), "approved_document_sha256": contract["sha256"], "task_contract_ref": contract}
    question_id = str(uuid.uuid4())
    content = {"questions": [{"question_id": "deployment", "question_text": "Where?"}],
               "questionnaire": {"cursor": 0, "revision": 2, "answers": {"deployment": "Docker"}}}
    async with service.repository.database.connection() as connection:
        await connection.execute(
            """INSERT INTO assistant.messages(message_id,project_id,run_id,channel,chat_id,user_id,
                direction,message_type,content_json) VALUES (%s,'demo',%s,'discord','chat','user','outbound','question',%s)""",
            (question_id, task["run_id"], Jsonb(content)),
        )
        await connection.execute(
            "UPDATE assistant.runs SET status='waiting_user',waiting_question_message_id=%s,request_json=request_json || %s WHERE run_id=%s",
            (question_id, Jsonb({"requirements_approval": approval}), task["run_id"]),
        )
        await connection.commit()
    await service.suspend(task["run_id"], "Temporary urgent work")
    resumed = await service.resume(task["run_id"])
    assert resumed["status"] == "waiting_user"
    assert resumed["waiting_question_message_id"] == question_id
    assert resumed["request_json"]["requirements_approval"] == approval
    assert (await service.repository.get_message(question_id))["content_json"] == content


async def test_running_or_publishing_tasks_cannot_suspend(core_settings):
    prepare_project(core_settings)
    service = ProjectTaskQueue(core_settings)
    await service.queue("demo", "Build", await source(service))
    task = (await service.activate_next("demo"))["task"]
    await edit_task(service, task["run_id"], status="running")
    with pytest.raises(PermissionError, match="safe boundary"):
        await service.suspend(task["run_id"], "unsafe")
    await edit_task(service, task["run_id"], status="blocked", patch={"publication_state": "queued"})
    with pytest.raises(PermissionError, match="publishing"):
        await service.suspend(task["run_id"], "unsafe")


async def test_changed_design_source_returns_to_design_and_keeps_requirements(core_settings):
    root = prepare_project(core_settings)
    service = ProjectTaskQueue(core_settings)
    await service.queue("demo", "Implement", await source(service))
    task = (await service.activate_next("demo"))["task"]
    path = root / "source.txt"
    path.write_text("old environment", encoding="utf-8")
    design = service.documents.write_revision(
        project_id="demo", task_id=task["run_id"], document_kind="solution-design", document_schema="solution-design.v1",
        payload={"source_refs": [resource_ref(kind="source", identity="environment", uri="vault://demo/source.txt",
                                             sha256=hashlib.sha256(path.read_bytes()).hexdigest())]},
        status="approved", actor_type="kernel", actor_id="kernel", run_id=task["run_id"], reason="Test design",
    )
    contract = task["request_json"]["accepted_documents"]["task_contract"]
    req_approval = {"approved_document_sha256": contract["sha256"]}
    await edit_task(service, task["run_id"], patch={
        "current_stage": "build", "accepted_documents": {"task_contract": contract, "solution_design": design},
        "requirements_approval": req_approval, "design_approval": {"approved_document_sha256": design["sha256"]},
        "task_checkpoint": {"completed": ["module-one"]},
    })
    await service.suspend(task["run_id"], "Pause")
    path.write_text("changed environment", encoding="utf-8")
    resumed = await service.resume(task["run_id"])
    assert resumed["request_json"]["current_stage"] == "design"
    assert resumed["request_json"]["requirements_approval"] == req_approval
    assert resumed["request_json"]["design_approval"] is None
    assert resumed["request_json"]["task_checkpoint"]["completed"] == ["module-one"]
    assert resumed["status"] == "waiting_supervisor"


async def test_managed_action_blocks_suspension_but_provenance_background_does_not(core_settings):
    prepare_project(core_settings)
    service = ProjectTaskQueue(core_settings)
    await service.queue("demo", "Operations", await source(service))
    task = (await service.activate_next("demo"))["task"]
    async with service.repository.database.connection() as connection:
        ordinary_id, action_id = str(uuid.uuid4()), str(uuid.uuid4())
        for run_id, request in ((ordinary_id, {"background": True, "operation_action": None}),
                                (action_id, {"operation_action": {"action_id": "restart"}})):
            await connection.execute(
                """INSERT INTO assistant.runs(run_id,project_id,agent_key,parent_run_id,run_type,
                    status,conversation_key,request_json,queue_kind)
                    VALUES (%s,'demo','project:demo:python',%s,'python','running',%s,%s,'runtime')""",
                (run_id, task["run_id"], f"python:demo:{run_id}", Jsonb(request)),
            )
        await connection.commit()
    with pytest.raises(PermissionError, match="managed operation"):
        await service.suspend(task["run_id"], "Urgent request")
    async with service.repository.database.connection() as connection:
        await connection.execute("UPDATE assistant.runs SET status='succeeded' WHERE run_id=%s", (action_id,))
        await connection.commit()
    suspended = await service.suspend(task["run_id"], "Urgent request")
    assert suspended["request_json"]["task_activation"] == "suspended"
    assert (await service.repository.get_run(ordinary_id))["status"] == "running"


async def test_operation_requires_baseline_and_report_window_is_idempotent(core_settings):
    prepare_project(core_settings)
    service = ProjectTaskQueue(core_settings)
    source_id = await source(service)
    window = {"start": "2026-10-03", "end": "2026-10-04"}
    request = await service.queue("demo", "Daily report", source_id, task_kind="operation", report_window=window)
    assert (await service.activate_next("demo"))["outcome"] == "idle"
    assert len((await service.status("demo"))["queued"]) == 1
    policy_task_id = str(uuid.uuid4())
    contract = service.documents.write_revision(
        project_id="demo", task_id=policy_task_id, document_kind="task-contract", document_schema="task-contract.v1",
        payload={"goal": "Original project goal", "requirements": ["Operate and report"]}, status="approved",
        actor_type="kernel", actor_id="kernel", run_id=policy_task_id, reason="Published requirements",
    )
    design = service.documents.write_revision(
        project_id="demo", task_id=policy_task_id, document_kind="solution-design", document_schema="solution-design.v1",
        payload={"operating_policy": "Daily report"}, status="approved", actor_type="kernel", actor_id="kernel",
        run_id=policy_task_id, reason="Published design",
    )
    baseline = {"schema_version": "operational-baseline.v1", "task_id": policy_task_id,
                "task_contract_ref": contract, "solution_design_ref": design,
                "requirements_approval": {"approved_document_sha256": contract["sha256"]},
                "design_approval": {"approved_document_sha256": design["sha256"]}, "runbook_actions": []}
    async with service.repository.database.connection() as connection:
        await connection.execute(
            "UPDATE assistant.kernel_meta SET value=value || %s WHERE key=%s",
            (Jsonb({"operational_baseline_ref": baseline}), service.metadata_key("demo")),
        )
        await connection.commit()
    activated = (await service.activate_next("demo"))["task"]
    assert activated["request_json"]["current_stage"] == "operate"
    assert activated["request_json"]["permission_profile_name"] == "read-only"
    assert activated["request_json"]["report_window"] == window
    assert activated["request_json"]["task_goal"] == "Daily report"
    assert activated["request_json"]["accepted_documents"]["task_contract"] == contract
    original_contract = service.documents.read("demo", contract)
    assert original_contract["payload"]["goal"] == "Original project goal"
    episode = service.documents.read("demo", activated["request_json"]["accepted_documents"]["operation_request"])
    assert episode["payload"]["goal"] == "Daily report"
    assert episode["schema_version"] == "operation-request.v1"
    assert activated["mutex_key"] == "engineering:demo"
    retry = await service.queue("demo", "Daily report", source_id, task_kind="operation", report_window=window)
    assert retry["message_id"] == request["message_id"]
    assert len((await service.status("demo"))["active"]) == 1
    await edit_task(service, activated["run_id"], status="succeeded")
    await service.activate_next("demo")
    assert (await service.status("demo"))["metadata"]["last_report_window"] == window
