"""Isolated database tests; no model calls and no production test records."""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path

import httpx
import psycopg
import pytest
from fastapi import FastAPI
from psycopg import sql

from assistant_kernel.api.v2 import build_v2_router
from assistant_kernel.config import KernelSettings
from assistant_kernel.repositories import ControlPlaneRepository
from assistant_kernel.repositories.stages import StageRepository
from assistant_kernel.services.project_build import ProjectBuildProcessor

pytestmark=pytest.mark.skipif(os.getenv("KERNEL_STAGE_INTEGRATION")!="1",reason="isolated PostgreSQL opt-in")


@pytest.fixture
def settings(tmp_path,request):
    base=KernelSettings.from_environment()
    name="kernel_stage_test_"+uuid.uuid4().hex
    kwargs=dict(host=base.postgres_host,port=base.postgres_port,dbname=base.postgres_database,
                user=base.postgres_user,password=base.postgres_password.get_secret_value())
    with psycopg.connect(**kwargs,autocommit=True) as c:
        c.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    setting=base.model_copy(update={"postgres_database":name,"data_root":tmp_path})
    try:
        with psycopg.connect(**{**kwargs,"dbname":name},autocommit=True) as c:
            for migration in sorted((Path(__file__).parents[1]/"migrations").glob("*.sql")):
                if int(migration.name[:4])<=getattr(request,"param",8):
                    c.execute(migration.read_text())
        yield setting
    finally:
        assert name.startswith("kernel_stage_test_") and len(name.split("_")[-1])==32
        with psycopg.connect(**kwargs,autocommit=True) as c:
            c.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))


class FakeStages:
    def __init__(self): self.calls=[]
    async def execute(self,stage,state):
        self.calls.append((stage,state["stage_attempt"]))
        return {"summary":stage,"verdict":"approve"}


def processor(settings,repo,fake=None):
    value=ProjectBuildProcessor(settings,repo,None)
    value.stage_executor=fake or FakeStages()
    return value


async def start(repo):
    return await repo.start_project_build(project_id="test",request_data={"goal":"test","user_id":"owner",
        "execution_profile":{"model":"explicit"}},idempotency_key=str(uuid.uuid4()))


async def work(repo,p):
    job=await repo.claim_job("test",str(uuid.uuid4()),60,job_types=["stage_run"])
    assert job
    result=await p.process(job)
    assert await repo.finish_job(str(job["job_id"]),str(job["worker_instance_id"]),result=result)
    return job,result


async def answer(repo,approval,action="approve",key=None):
    return await repo.decide_approval(approval_id=approval,decision=action,edited_payload=None if action=="approve" else {"note":"reviewed"},
        decided_by="owner",idempotency_key=key or str(uuid.uuid4()))


async def test_full_build_and_no_replay_of_finished_stage(settings):
    repo=ControlPlaneRepository(settings); fake=FakeStages(); p=processor(settings,repo,fake)
    run=await start(repo)
    first,waiting=await work(repo,p)
    first_receipt=waiting
    assert waiting["status"]=="waiting_user"
    await answer(repo,waiting["approval_id"])
    assert (await work(repo,p))[1]["next_stage"]=="review"
    _,waiting=await work(repo,p)
    assert (await answer(repo,waiting["approval_id"]))["status"]=="succeeded"
    assert (await repo.get_workflow_run(run["workflow_run_id"]))["status"]=="succeeded"
    assert await p.process(first)==first_receipt
    assert [x[0] for x in fake.calls]==["requirements","implementation","review"]


async def test_concurrent_duplicate_answer_and_old_answers(settings):
    repo=ControlPlaneRepository(settings); p=processor(settings,repo)
    await start(repo); _,waiting=await work(repo,p)
    responses=await asyncio.gather(answer(repo,waiting["approval_id"],key="same"),answer(repo,waiting["approval_id"],key="same"))
    assert sum(x["duplicate"] for x in responses)==1
    with pytest.raises(ValueError,match="conflicting"):
        await answer(repo,waiting["approval_id"],"reject")
    with pytest.raises(PermissionError):
        await repo.decide_approval(approval_id=waiting["approval_id"],decision="approve",edited_payload=None,decided_by="stranger",idempotency_key="wrong")
    async with repo.database.connection() as c:
        assert (await (await c.execute("SELECT count(*) n FROM assistant.control_jobs WHERE status='queued'")).fetchone())["n"]==1


async def test_stage_receipt_and_concurrent_workers(settings):
    repo=ControlPlaneRepository(settings); await start(repo)
    fake=FakeStages(); p=processor(settings,repo,fake)
    job=await repo.claim_job("test",str(uuid.uuid4()),60)
    first,duplicate=await asyncio.gather(p.process(job),p.process(job))
    assert first==duplicate
    assert len(fake.calls)==1
    assert await p.process(job)==first


async def test_edit_reject_and_stage_attempt_identity(settings):
    repo=ControlPlaneRepository(settings);run=await start(repo); fake=FakeStages();p=processor(settings,repo,fake)
    _,first=await work(repo,p)
    await answer(repo,first["approval_id"],"edit")
    _,second=await work(repo,p)
    assert first["approval_id"]!=second["approval_id"]
    assert fake.calls==[("requirements",1),("requirements",2)]
    await answer(repo,second["approval_id"],"reject")
    assert (await repo.database.get_task(run["task_id"]))["status"]=="cancelled"
    with pytest.raises(ValueError):
        await StageRepository(settings).retry_failed(run["workflow_run_id"],"illegal")


async def test_dead_letter_and_retry_only_current_stage(settings):
    repo=ControlPlaneRepository(settings); run=await start(repo);p=processor(settings,repo)
    _,first=await work(repo,p); await answer(repo,first["approval_id"])
    async with repo.database.connection() as c:
        await c.execute("UPDATE assistant.control_jobs SET max_attempts=1 WHERE status='queued'");await c.commit()
    job=await repo.claim_job("test",str(uuid.uuid4()),60)
    class Broken:
        async def execute(self,stage,state): raise RuntimeError("temporary")
    p.stage_executor=Broken()
    with pytest.raises(RuntimeError,match="temporary"): await p.process(job)
    assert await repo.retry_job(str(job["job_id"]),str(job["worker_instance_id"]),{"type":"temporary"})=="dead_letter"
    await repo.fail_task_for_dead_letter(str(job["job_id"]),{"type":"temporary"})
    assert (await repo.get_workflow_run(run["workflow_run_id"]))["status"]=="failed"
    recovered=await StageRepository(settings).retry_failed(run["workflow_run_id"],"retry")
    assert await StageRepository(settings).retry_failed(run["workflow_run_id"],"retry")==recovered
    fake=FakeStages();p.stage_executor=fake
    assert (await work(repo,p))[1]["next_stage"]=="review"
    assert fake.calls==[("implementation",2)]


async def test_cancel_and_expired_worker_fencing(settings):
    repo=ControlPlaneRepository(settings); run=await start(repo)
    assert await repo.request_job_cancel(run["job_id"])
    assert (await repo.get_workflow_run(run["workflow_run_id"]))["status"]=="cancelled"
    await start(repo);job=await repo.claim_job("test",str(uuid.uuid4()),60)
    async with repo.database.connection() as c:
        await c.execute("UPDATE assistant.control_jobs SET worker_instance_id=%s WHERE job_id=%s",(str(uuid.uuid4()),job["job_id"]));await c.commit()
    with pytest.raises(RuntimeError,match="lease lost"):
        await processor(settings,repo).process(job)


async def test_separate_process_runs_next_stage_without_graph_libraries(settings):
    repo=ControlPlaneRepository(settings);run=await start(repo)
    _,waiting=await work(repo,processor(settings,repo));await answer(repo,waiting["approval_id"])
    code='''import sys,asyncio,uuid
class BlockGraph:
 def find_spec(self,fullname,*args):
  if fullname.startswith("langgraph"):raise AssertionError("graph import is forbidden")
sys.meta_path.insert(0,BlockGraph())
from assistant_kernel.config import KernelSettings
from assistant_kernel.repositories import ControlPlaneRepository
from assistant_kernel.services.project_build import ProjectBuildProcessor
import assistant_kernel.api.main
import assistant_kernel.worker.main
import assistant_kernel.mcp_server.server
class Fake:
 async def execute(self,stage,state):
  assert stage=="implementation"
  return {"summary":"implementation"}
async def main():
 s=KernelSettings.from_environment();r=ControlPlaneRepository(s);p=ProjectBuildProcessor(s,r,None);p.stage_executor=Fake()
 job=await r.claim_job("child",str(uuid.uuid4()),60,job_types=["stage_run"])
 result=await p.process(job);assert result["next_stage"]=="review"
 assert await r.finish_job(str(job["job_id"]),str(job["worker_instance_id"]),result=result)
 print("PYTHON_STAGE_RESTART_OK")
asyncio.run(main())
'''
    child=await asyncio.create_subprocess_exec(sys.executable,"-B","-c",code,env={**{k:v for k,v in os.environ.items() if k!="PYTHONPATH"},"POSTGRES_DB":settings.postgres_database,
        "ASSISTANT_DATA_ROOT":str(settings.data_root),"PYTHONPATH":str(Path(__file__).parents[1]/"src")},stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    stdout,stderr=await asyncio.wait_for(child.communicate(),30)
    assert child.returncode==0,stderr.decode()
    assert b"PYTHON_STAGE_RESTART_OK" in stdout


async def test_api_and_existing_python_script_dispatch(settings):
    import hashlib
    from assistant_kernel.storage import ProjectRuntimeManager
    from assistant_kernel.services.routine_workflow import RoutineWorkflowProcessor
    repo=ControlPlaneRepository(settings);app=FastAPI();app.include_router(build_v2_router(settings,repo,lambda:None))
    source=ProjectRuntimeManager(settings.data_root).resolve_vault_file("test","script.py");source.write_text('print("test")')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
        bad=await client.post("/v2/projects/test/workflows/w/versions",json={"definition_relative_path":"script.py","spec":{"executor":{"type":"langgraph_factory"}}})
        assert bad.status_code==422
        version=await client.post("/v2/projects/test/workflows/w/versions",json={"definition_relative_path":"script.py","spec":{"executor":{"type":"python_script"}}})
        assert version.status_code==200,version.text
        key=version.json()["workflow_version_id"]
        assert (await client.post(f"/v2/workflow-versions/{key}/activate",json={"tests_uri":"test://ok","review_uri":"test://ok","accepted_by":"test","acceptance_evidence":{"ok":True}})).status_code==200
        run=await client.post("/v2/projects/test/workflows/w/runs",json={"input":{}},headers={"X-Idempotency-Key":"script"})
        assert run.status_code==202,run.text
        job=await repo.claim_job("test",str(uuid.uuid4()),60,job_types=["routine_workflow"])
        assert (await RoutineWorkflowProcessor(settings,repo).process(job))["execution_id"]
        build=await client.post("/v2/projects/test/builds",json={"request":{"goal":"test","user_id":"owner"}},headers={"X-Idempotency-Key":"build"})
        assert build.status_code==202,build.text
        _,waiting=await work(repo,processor(settings,repo))
        response=await client.post(f"/v2/approvals/{waiting['approval_id']}/responses",json={"answer":{"action":"approve"},"decided_by":"owner"},headers={"X-Idempotency-Key":"approval"})
        assert response.status_code==202,response.text


@pytest.mark.parametrize("settings",[5],indirect=True)
async def test_migration_preserves_session_checkpoint_and_retires_stale_notifications(settings):
    repo=ControlPlaneRepository(settings);checkpoint=str(uuid.uuid4())
    async with repo.database.connection() as c:
        await c.execute("INSERT INTO assistant.structured_checkpoints(checkpoint_id,state_json,checkpoint_kind) VALUES (%s,'{}','session_rotation')",(checkpoint,))
        await c.execute("INSERT INTO assistant.outbox_events(outbox_event_id,aggregate_type,aggregate_id,event_type,payload) VALUES (%s,'approval','old','approval.requested','{}')",(str(uuid.uuid4()),))
        await c.commit()
    migration=(Path(__file__).parents[1]/"migrations"/"0006_python_stages.sql").read_text()
    async with repo.database.connection() as c:
        await c.execute(migration)
        assert (await (await c.execute("SELECT count(*) n FROM assistant.structured_checkpoints WHERE checkpoint_id=%s",(checkpoint,))).fetchone())["n"]==1
        assert (await (await c.execute("SELECT status FROM assistant.outbox_events")).fetchone())["status"]=="cancelled"
        assert not await (await c.execute("SELECT 1 FROM information_schema.schemata WHERE schema_name='assistant_checkpoints'")).fetchone()


async def test_session_rotation_checkpoint_is_still_functional(settings):
    repo=ControlPlaneRepository(settings)
    agent=await repo.ensure_default_agent("test")
    logical=await repo.resolve_logical_session(agent_id=agent,scope_type="conversation",scope_id="test",project_id="test")
    physical=await repo.record_successful_turn(logical_session_id=str(logical["logical_session_id"]),expected_current_record_id=None,
        native_session_id="old",harness="fake",provider=None,model=None,role_version_id=None,prompt_manifest={"test":True},message_id=None,
        active_input_tokens=60,context_window_tokens=100,archive_uri=None,archive_sha256=None,starts_new_physical_session=True)
    checkpoint=await repo.require_rotation(logical_session_id=str(logical["logical_session_id"]),
        physical_session_record_id=str(physical["physical_session_record_id"]),project_id="test",task_id=None,
        checkpoint_state={"next_action":"continue"})
    assert checkpoint["checkpoint_id"]
    current=await repo.resolve_logical_session(agent_id=agent,scope_type="conversation",scope_id="test",project_id="test")
    assert current["checkpoint"]["state_json"]=={"next_action":"continue"}
    assert current["physical_session"]["native_session_id"]=="old"


async def test_saved_codex_artifact_is_reused_after_worker_crash(settings):
    repo=ControlPlaneRepository(settings);run=await start(repo)
    job=await repo.claim_job("test",str(uuid.uuid4()),60,job_types=["stage_run"])
    context=await StageRepository(settings).context(job)
    artifact=await repo.create_artifact_version(project_id="test",task_id=run["task_id"],stage_id="requirements",artifact_type="requirements",
        storage_uri="test://saved-result",content_sha256="0"*64,metadata={"stage_run_id":str(context["node_run_id"]),
        "stage_result":{"summary":"already computed","storage_uri":"test://saved-result","content_sha256":"0"*64}})
    # Real executor, no Harness supplied: any model call would fail this test.
    result=await ProjectBuildProcessor(settings,repo,None).process(job)
    assert result["status"]=="waiting_user"
    approval=await repo.get_approval(result["approval_id"])
    assert approval["payload"]["artifact"]["artifact_version_id"]==str(artifact["artifact_version_id"])


@pytest.mark.parametrize("settings",[5],indirect=True)
async def test_cutover_rejects_live_legacy_work(settings):
    repo=ControlPlaneRepository(settings)
    async with repo.database.connection() as c:
        await c.execute("INSERT INTO assistant.control_jobs(job_id,job_type,status) VALUES (%s,'graph_resume','queued')",(str(uuid.uuid4()),))
        await c.commit()
    migration=(Path(__file__).parents[1]/"migrations"/"0006_python_stages.sql").read_text()
    async with repo.database.connection() as c:
        with pytest.raises(psycopg.errors.RaiseException,match="Live legacy work"):
            await c.execute(migration)
        await c.rollback()
        assert not await (await c.execute("SELECT 1 FROM assistant.schema_migrations WHERE version='0006'")).fetchone()
