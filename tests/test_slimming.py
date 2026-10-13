"""Concurrency and durable-input regressions against isolated PostgreSQL."""
import asyncio
import importlib
import json
import sys
import uuid

import httpx
import pytest

from test_core4 import core_settings, prepare_agent, prepare_bindable_project, prepare_single_agent_build_run, FakeHarness
from assistant_kernel.repositories import CoreRepository
from assistant_kernel.storage import ProjectRuntimeManager
from assistant_kernel.worker.main import execute_python
from assistant_kernel.services.run_processor import RunProcessor


async def incoming(repo, text="start", event=None):
    return await repo.ingest_message(project_id="demo", agent_key="project:demo:manager",
        channel="discord",chat_id="c",user_id="u",external_event_id=event or uuid.uuid4().hex,
        external_message_id=None,content={"text":text})


async def python_run(repo, name="task.py", **kwargs):
    return await repo.create_run(project_id="demo",agent_key="project:demo:python",run_type="python",
        request={"source_uri":f"vault://demo/{name}",**kwargs.pop("request",{})},**kwargs)


async def test_parallel_scripts_are_real_subprocesses_and_leave_interactive_capacity(core_settings):
    prepare_agent(core_settings)
    repo=CoreRepository(core_settings)
    root=ProjectRuntimeManager(core_settings.data_root).ensure("demo").vault_root
    (root/"task.py").write_text("import os,time,json\nstart=time.time()\ntime.sleep(0.6)\njson.dump({'start':start,'end':time.time(),'id':os.environ['ASSISTANT_RUN_ID']},open(os.environ['ASSISTANT_RESULT_PATH'],'w'))\n")
    scripts=[await python_run(repo) for _ in range(4)]
    workers=[str(uuid.uuid4()) for _ in scripts]
    claimed=await asyncio.gather(*(repo.claim_run("runtime",w,60,queue_kind="runtime") for w in workers))
    assert all(claimed) and len({r['run_id'] for r in claimed})==4
    pm=await incoming(repo)
    assert (await repo.claim_run("pm",str(uuid.uuid4()),60))['run_id']==pm['run']['run_id']
    await asyncio.gather(*(execute_python(r,core_settings,repo,w,asyncio.Event()) for r,w in zip(claimed,workers)))
    results=[(await repo.get_run(str(r['run_id'])))['result_json'] for r in claimed]
    assert max(r['start'] for r in results)<min(r['end'] for r in results)
    assert len({r['id'] for r in results})==4


async def test_mutex_reconciliation_scope_and_python_parent_provenance(core_settings):
    prepare_agent(core_settings); repo=CoreRepository(core_settings)
    pm=await incoming(repo)
    claimed_pm=await repo.claim_run("pm",str(uuid.uuid4()),60)
    script=await python_run(repo,parent_run_id=str(claimed_pm['run_id']),trigger_message_id=str(pm['message']['message_id']))
    assert (await repo.claim_run("runtime",str(uuid.uuid4()),60,queue_kind="runtime"))['run_id']==script['run_id']
    first=await python_run(repo,request={'shared_write':True})
    second=await python_run(repo,request={'shared_write':True})
    assert (await repo.claim_run("runtime",str(uuid.uuid4()),60,queue_kind="runtime"))['run_id']==first['run_id']
    async with repo.database.connection() as conn:
        await conn.execute("UPDATE assistant.runs SET status='reconciling' WHERE run_id=%s",(first['run_id'],));await conn.commit()
    assert await repo.claim_run("runtime",str(uuid.uuid4()),60,queue_kind="runtime") is None
    free=await python_run(repo)
    assert (await repo.claim_run("runtime",str(uuid.uuid4()),60,queue_kind="runtime"))['run_id']==free['run_id']
    assert (await repo.active_execution_blocker('demo',str(second['run_id'])))['run_id']==first['run_id']


async def test_claim_race_and_old_fence(core_settings):
    repo=CoreRepository(core_settings); run=await python_run(repo)
    claims=await asyncio.gather(*(repo.claim_run('race',str(uuid.uuid4()),60,queue_kind='runtime') for _ in range(8)))
    winner=next(r for r in claims if r)
    assert sum(r is not None for r in claims)==1
    assert not await repo.finish_run(str(run['run_id']),str(uuid.uuid4()),str(uuid.uuid4()),status='succeeded')
    assert (await repo.get_run(str(run['run_id'])))['status']=='running'


async def test_pending_inputs_survive_failure_and_late_input_fences_completion(core_settings):
    prepare_bindable_project(core_settings);repo=CoreRepository(core_settings)
    root=ProjectRuntimeManager(core_settings.data_root).ensure('demo').vault_root
    import yaml
    config=yaml.safe_load((root/'project.yaml').read_text());config['task_protocol']='single-agent.v2';(root/'project.yaml').write_text(yaml.safe_dump(config))
    first=await incoming(repo);worker=str(uuid.uuid4());run=await repo.claim_run('pm',worker,60)
    supplement=await incoming(repo,'supplement')
    assert supplement['run'] is None
    pinned=await repo.pin_turn_inputs(str(run['run_id']),worker,str(run['claim_token']))
    assert [r['message_id'] for r in pinned]==[supplement['message']['message_id']]
    assert (await repo.get_message(str(pinned[0]['message_id'])))['content_json']['task_input_status']=='pending'
    late=await incoming(repo,'late')
    session=await repo.ensure_session('demo','project:demo:manager','discord','c')
    result=await repo.finalize_agent_completed(run_id=str(run['run_id']),worker_instance_id=worker,
        claim_token=str(run['claim_token']),session_id=str(session['session_id']),native_session_id='n',
        input_tokens=1,context_window_tokens=1000,archive_uri=None,archive_sha256=None,
        project_id='demo',channel='discord',chat_id='c',user_id='u',source_message_id=str(first['message']['message_id']),
        summary='done',result={},artifact_refs=[],usage_snapshot={})
    assert result['input_pending'] and result['run']['status']=='queued'
    assert (await repo.get_message(str(pinned[0]['message_id'])))['content_json']['task_input_status']=='consumed'
    restarted=CoreRepository(core_settings); next_run=await restarted.claim_run('pm',str(uuid.uuid4()),60)
    next_inputs=await restarted.pin_turn_inputs(str(next_run['run_id']),str(next_run['worker_instance_id']),str(next_run['claim_token']))
    assert [r['message_id'] for r in next_inputs]==[late['message']['message_id']]


async def test_same_body_distinct_events_and_event_retry(core_settings):
    prepare_agent(core_settings);repo=CoreRepository(core_settings)
    first=await incoming(repo,'same','event-1');second=await incoming(repo,'same','event-2')
    duplicate=await incoming(repo,'same','event-1')
    assert first['run']['run_id']!=second['run']['run_id']
    assert duplicate['duplicate']
    assert all(r['status']=='queued' for r in await repo.list_runs('demo'))


async def test_python_api_idempotency_and_stage_retirement(core_settings,monkeypatch):
    prepare_agent(core_settings);repo=CoreRepository(core_settings)
    root=ProjectRuntimeManager(core_settings.data_root).ensure('demo').vault_root
    (root/'task.py').write_text('print("ok")')
    import assistant_kernel.api.main as api
    monkeypatch.setattr(api,'repository',repo)
    monkeypatch.setattr(api,'runtime_manager',ProjectRuntimeManager(core_settings.data_root))
    api.app.dependency_overrides[api.require_kernel_token]=lambda:None
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app),base_url='http://test') as client:
            body={'project_id':'demo','relative_script_path':'task.py'}
            first=await client.post('/v1/python-executions',json=body,headers={'Idempotency-Key':'stable'})
            retry=await client.post('/v1/python-executions',json=body,headers={'Idempotency-Key':'stable'})
            conflict=await client.post('/v1/python-executions',json={**body,'shared_write':True},headers={'Idempotency-Key':'stable'})
            assert first.status_code==retry.status_code==200
            assert first.json()['run_id']==retry.json()['run_id']
            assert conflict.status_code==409
    finally:
        api.app.dependency_overrides.clear()
    with pytest.raises(PermissionError,match='retired'):
        await repo.create_run(project_id='demo',agent_key='project:demo:review',run_type='stage',request={})


def current_project(settings):
    import yaml
    prepare_bindable_project(settings)
    root=ProjectRuntimeManager(settings.data_root).ensure('demo').vault_root
    config=yaml.safe_load((root/'project.yaml').read_text());config['task_protocol']='single-agent.v2'
    (root/'project.yaml').write_text(yaml.safe_dump(config))
    return root


async def test_v2_api_requires_type_and_shared_write_idempotency(core_settings):
    from fastapi import FastAPI
    from assistant_kernel.api.v2 import build_v2_router
    app=FastAPI();repo=CoreRepository(core_settings)
    app.include_router(build_v2_router(core_settings,repo,lambda:None))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        assert (await client.post('/v2/runs',json={'project_id':'demo'})).status_code==422
        assert (await client.post('/v2/runs',json={'project_id':'demo','run_type':'stage'})).status_code==409
        body={'project_id':'demo','run_type':'python','request':{'source_uri':'vault://demo/x.py'},'idempotency_key':'v2-stable'}
        first=await client.post('/v2/runs',json=body);retry=await client.post('/v2/runs',json=body)
        assert first.status_code==retry.status_code==202
        assert first.json()['run_id']==retry.json()['run_id']
        assert (await client.post('/v2/runs',json={**body,'shared_write':True})).status_code==409


async def test_supplement_is_in_pm_prompt_and_document_inputs(core_settings):
    root=current_project(core_settings);repo=CoreRepository(core_settings)
    first=await incoming(repo);worker=str(uuid.uuid4());run=await repo.claim_run('pm',worker,60)
    supplement=await incoming(repo,'keep the exact supplement')
    response=json.dumps({'outcome':'continue','summary':'read input','task_checkpoint':{'goal':'g','completed':['read'],'pending':['next'],'next_action':'next'},'artifacts':[]})
    harness=FakeHarness([(response,10,'native-pm')])
    await RunProcessor(core_settings,repo,harness).process(run,worker)
    assert 'keep the exact supplement' in harness.requests[0].prompt
    message=await repo.get_message(str(supplement['message']['message_id']))
    assert message['content_json']['task_input_status']=='consumed'
    settled=await repo.get_run(str(run['run_id']))
    from assistant_kernel.services.document_runtime import TaskDocumentService
    turn=TaskDocumentService(core_settings).read('demo',settled['request_json']['accepted_documents']['last_turn'])
    assert str(supplement['message']['message_id']) in {ref['id'] for ref in turn['payload']['input_refs']}


async def test_current_audit_rework_stays_on_same_task(core_settings):
    current_project(core_settings);repo=CoreRepository(core_settings)
    first=await incoming(repo);worker=str(uuid.uuid4());run=await repo.claim_run('pm',worker,60)
    run=await prepare_single_agent_build_run(core_settings,repo,run,worker,'g')
    run=await repo.patch_claimed_run_request(run_id=str(run['run_id']),worker_instance_id=worker,
        claim_token=str(run['claim_token']),request_patch={'audit_mode':True})
    response=json.dumps({'outcome':'request_changes','summary':'defect found','issues':[{'issue_id':'i','required_fix':'fix','evidence':[]}],
        'task_checkpoint':{'goal':'g','pending':['fix'],'next_action':'fix'},'artifacts':[]})
    result=await RunProcessor(core_settings,repo,FakeHarness([(response,10,'audit-native')])).process(run,worker)
    assert result['outcome']=='continue'
    updated=await repo.get_run(str(run['run_id']))
    assert updated['request_json']['audit_mode'] is False
    assert updated['request_json']['audit_feedback'][0]['issue_id']=='i'
    assert len(await repo.list_runs('demo'))==1


async def test_old_stage_history_reads_but_cannot_be_claimed(core_settings):
    repo=CoreRepository(core_settings);old_id=str(uuid.uuid4())
    async with repo.database.connection() as conn:
        await conn.execute("""INSERT INTO assistant.runs(run_id,project_id,agent_key,conversation_key,run_type,status)
            VALUES (%s,'demo','project:demo:review','historical','stage','succeeded')""",(old_id,));await conn.commit()
    assert (await repo.get_run(old_id))['status']=='succeeded'
    with pytest.raises(PermissionError,match='retired'):
        await repo.start_task_from_claim(source_run_id=old_id,worker_instance_id=str(uuid.uuid4()),claim_token=str(uuid.uuid4()),goal='g',stages=['review'])
    assert await repo.claim_run('pm',str(uuid.uuid4()),60) is None


async def test_migration_round_trip_preserves_waiting_question(core_settings,monkeypatch):
    from test_core4 import _questionnaire_waiting
    from scripts import migrate_core5_v7 as migration
    repo,run,message=await _questionnaire_waiting(core_settings)
    monkeypatch.setattr(migration.KernelSettings,'from_environment',classmethod(lambda cls:core_settings))
    before=await repo.get_run(str(run['run_id']))
    assert (await migration.migrate(downgrade=True))['schema_version']=='core5.v6'
    result=await migration.migrate()
    after=await repo.get_run(str(run['run_id']))
    for field in ('request_json','result_json','status','waiting_question_message_id','interaction_seq'):
        assert before[field]==after[field]
    assert result['waiting_preserved']==1
    busy=await python_run(repo)
    await repo.claim_run('runtime',str(uuid.uuid4()),60,queue_kind='runtime')
    with pytest.raises(RuntimeError,match='refused'):
        await migration.migrate(downgrade=True)


async def test_real_supervisor_snapshot_has_no_retired_business_source_dependency(core_settings):
    from assistant_kernel.services.goal_supervisor import GoalSupervisorService
    current_project(core_settings)
    repo=CoreRepository(core_settings)
    start=await incoming(repo)
    snapshot=await GoalSupervisorService(core_settings,repo).build_snapshot(
        project_id='demo',task_id=str(start['run']['run_id']),trigger='user_consultation')
    assert snapshot['business_state_refs']==[]
    assert snapshot['kernel_health']['schema_version']=='core5.v7'


async def test_cancelled_publisher_cannot_commit_and_worker_pid_cleanup_is_scoped(core_settings):
    prepare_agent(core_settings)
    repo=CoreRepository(core_settings)
    message=await incoming(repo)
    run_id,owner,token=(str(uuid.uuid4()) for _ in range(3))
    async with repo.database.connection() as connection:
        await connection.execute("""INSERT INTO assistant.runs(run_id,project_id,agent_key,conversation_key,
            trigger_message_id,run_type,status,worker_instance_id,claim_token,process_pid,process_start_ticks,cancel_requested_at)
            VALUES (%s,'demo','project:demo:publisher','publisher',%s,'publish','running',%s,%s,123,456,now())""",
            (run_id,message['message']['message_id'],owner,token))
        await connection.commit()
    with pytest.raises(RuntimeError,match='lease was lost'):
        await repo.finish_publish_run(run_id,owner,token,result={},artifact_refs=[])
    await repo.clear_publisher_process(run_id,123,999)
    assert (await repo.get_run(run_id))['process_pid']==123
    await repo.clear_publisher_process(run_id,123,456)
    assert (await repo.get_run(run_id))['process_pid'] is None
    assert (await repo.get_run(run_id))['status']=='running'
