"""Real isolated PG + fixed interpreter execution. All authority records are fixtures."""
import asyncio
import json
import uuid
from pathlib import Path

import pytest
from psycopg.types.json import Jsonb

from assistant_kernel.repositories import CoreRepository
from assistant_kernel.services.document_runtime import TaskDocumentService,resource_ref
from assistant_kernel.services.project_python import ProjectPython
from assistant_kernel.services.runtime_notifications import RuntimeNotifications
from assistant_kernel.worker.main import execute_python
from test_core4 import core_settings
from test_v12_p4_fixed_python import fixture


async def execution(settings,code=None):
    code=code or "import json,os\njson.dump({'answer':42},open(os.environ['ASSISTANT_RESULT_PATH'],'w'))\n"
    root,_,manifest,source=fixture(settings,code=code)
    manifest['code_bundle']['closure_manifest_ref']=source('program/closure.json',
        {'schema_version':'python-import-closure.v1','stdlib_modules':['json','os','time'],'dependency_modules':[]})
    repo=CoreRepository(settings)
    event=await repo.ingest_message(project_id='demo',agent_key='project:demo:manager',channel='test',chat_id='fixture',
        user_id='fixture-owner',external_event_id='isolated-authority',external_message_id=None,
        content={'text':'Synthetic isolation fixture; not a human acceptance or live authorization'})
    parent=event['run']; invocation=uuid.uuid4().hex
    message=resource_ref(kind='message',identity=str(event['message']['message_id']),
        uri='db://assistant/messages/'+str(event['message']['message_id']),purpose='isolated synthetic confirmation')
    package=source('program/package.json',{'fixture':'confirmed isolated package'})
    async with repo.database.connection() as c:
        await c.execute("UPDATE assistant.runs SET status='running',request_json=request_json || %s WHERE run_id=%s",
            (Jsonb({'current_stage':'build','confirmed_turn_package_ref':package,'prompt_confirmation_message_id':message['id']}),parent['run_id']))
        await c.commit()
    await repo.start_model_invocation(invocation_id=invocation,project_id='demo',run_id=str(parent['run_id']),
        session_id=None,actor_role='pm',harness='synthetic-fixture',provider='none',model='none',reasoning_effort=None,auth_mode=None)
    manifest['ownership'].update(creating_task_ref=resource_ref(kind='receipt',identity=str(parent['run_id']),
        uri='db://assistant/runs/'+str(parent['run_id']),purpose='actual isolated owner'),
        creating_invocation_ref=resource_ref(kind='receipt',identity=invocation,uri='db://assistant/model_invocations/'+invocation,
            purpose='actual fixture invocation ledger'),lifecycle_relation='invocation_dependent',initial_authorization_ref=message)
    manifest['purpose'].update(authorization_ref=message,turn_boundary_ref=package)
    manifest['io']['business_check_refs']=[source('program/check.json',
        {'schema_version':'python-business-check.v1','json_pointer':'/answer','operator':'equals','expected':42})]
    ref=TaskDocumentService(settings).write_scoped_revision(project_id='demo',scope='project',object_id='fixture-main',
        document_kind='python-script-manifest',document_schema='python-script-manifest.v1',payload=manifest,
        status='submitted',actor_type='kernel',actor_id='fixture',reason='Isolated execution fixture',source_refs=[message])
    submission={'manifest_ref':ref,'inputs':{'n':42},'idempotency_key':uuid.uuid4().hex,'execution_kind':'auxiliary',
        'runtime_unit_ref':None,'logical_execution_id':None,'resume_owner':None}
    return repo,ProjectPython(settings,repo),submission,invocation,root


async def claimed(repo):
    owner=str(uuid.uuid4());run=await repo.claim_run('fixed-test',owner,90,queue_kind='runtime')
    assert run
    return run,owner


async def test_fixed_submit_duplicate_after_claim_and_real_contract_business_settlement(core_settings):
    repo,service,submission,invocation,root=await execution(core_settings)
    admission=await service.submit('demo',submission);run,owner=await claimed(repo)
    duplicate=await service.submit('demo',submission)
    assert duplicate['status']=='duplicate' and duplicate['run_ref']==admission['run_ref']
    await execute_python(run,core_settings,repo,owner,asyncio.Event())
    final=await repo.get_run(str(run['run_id']));receipt=final['executor_json']['python_attempt']
    assert final['status']=='succeeded' and final['result_json']=={'answer':42}
    documents=TaskDocumentService(core_settings)
    binding=documents.read('demo',receipt['binding_ref'])['payload']
    settlement=documents.read('demo',receipt['settlement_ref'])['payload']
    assert binding['creating_invocation_ref']['id']==invocation
    assert settlement['process_status']=='succeeded' and settlement['contract_status']=='passed'
    assert settlement['business_status']=='passed' and settlement['effect_status']=='none'
    assert settlement['unverified']==[]
    await repo.finish_model_invocation(invocation_id=invocation,status='succeeded')
    duplicate=await service.submit('demo',submission)
    assert duplicate['status']=='duplicate' and duplicate['run_ref']==admission['run_ref']
    with pytest.raises(ValueError,match='different fixed inputs'):
        await service.submit('demo',{**submission,'inputs':{'n':43}})


async def test_zero_exit_missing_output_fails_contract_and_queues_one_immediate_notice(core_settings):
    repo,service,submission,invocation,root=await execution(core_settings,"print('normal exit, missing output')\n")
    await service.submit('demo',submission);run,owner=await claimed(repo)
    with pytest.raises(ValueError,match='requires this attempt result'):await execute_python(run,core_settings,repo,owner,asyncio.Event())
    final=await repo.get_run(str(run['run_id']));receipt=final['executor_json']['python_attempt']
    settlement=TaskDocumentService(core_settings).read('demo',receipt['settlement_ref'])['payload']
    assert settlement['process_status']=='succeeded' and settlement['contract_status']=='failed'
    # This is before retry/reconciliation; failure delivery does not wait for terminal Run.
    async with repo.database.connection() as c:
        rows=await (await c.execute("SELECT * FROM assistant.messages WHERE idempotency_key LIKE 'python-failure:%%'")).fetchall()
    assert len(rows)==1 and rows[0]['content_json']['pending_pm_route'] and rows[0]['channel']=='internal'
    await repo.record_python_attempt(str(run['run_id']),owner,str(run['claim_token']),receipt,final['artifact_refs'])
    bindings=core_settings.data_root/'config/channel-bindings.yaml'
    bindings.parent.mkdir(parents=True,exist_ok=True)
    bindings.write_text('bindings:\n  - project_id: demo\n    channel: discord\n    chat_id: "fixture-pm"\n    agent_key: project:demo:manager\n')
    await RuntimeNotifications(core_settings,repo).route_pending()
    async with repo.database.connection() as c:
        rows=await (await c.execute("SELECT * FROM assistant.messages WHERE idempotency_key LIKE 'python-failure:%%'")).fetchall()
    assert len(rows)==1 and rows[0]['chat_id']=='fixture-pm' and not rows[0]['content_json']['pending_pm_route']


async def test_invocation_end_cancels_queued_and_running_auxiliary_without_detaching(core_settings):
    repo,service,submission,invocation,root=await execution(core_settings,"import time\ntime.sleep(15)\n")
    await service.submit('demo',submission);run,owner=await claimed(repo)
    pending=await service.submit('demo',{**submission,'idempotency_key':uuid.uuid4().hex})
    executing=asyncio.create_task(execute_python(run,core_settings,repo,owner,asyncio.Event()))
    attempt=root/'runs'/str(run['run_id'])/'attempts/1/execution.json'
    for _ in range(200):
        if executing.done():await executing
        if attempt.is_file() and json.loads(attempt.read_text()).get('process_identity'):break
        await asyncio.sleep(.02)
    else:pytest.fail('Actual fixed process did not launch')
    await repo.finish_model_invocation(invocation_id=invocation,status='succeeded')
    await asyncio.wait_for(executing,8)
    final=await repo.get_run(str(run['run_id']))
    assert final['status']=='cancelled' and final['executor_json']['python_attempt']['cleanup']['status']=='verified'
    assert (await repo.get_run(pending['run_ref']['id']))['status']=='cancelled'


async def test_wrong_creating_task_and_changed_scope_refuse_launch(core_settings):
    repo,service,submission,invocation,root=await execution(core_settings)
    async with repo.database.connection() as c:
        await c.execute("UPDATE assistant.model_invocations SET status='succeeded' WHERE invocation_id=%s",(invocation,));await c.commit()
    with pytest.raises(PermissionError,match='no longer an active'):await service.submit('demo',submission)


async def test_parallel_submissions_of_same_logical_work_do_not_create_two_runs(core_settings):
    repo,service,submission,_,_=await execution(core_settings)
    results=await asyncio.gather(*(service.submit('demo',{**submission,'logical_execution_id':'one-business-work',
        'idempotency_key':'competing-'+str(i)}) for i in range(2)),return_exceptions=True)
    assert sum(isinstance(x,dict) for x in results)==1
    assert sum(isinstance(x,PermissionError) for x in results)==1
    async with repo.database.connection() as c:
        assert (await (await c.execute("SELECT count(*) AS n FROM assistant.runs WHERE request_json->>'logical_execution_id'='one-business-work'")).fetchone())['n']==1


async def test_execution_documents_and_map_commit_with_the_actual_attempt(core_settings):
    from test_v12_p3_map_generations import baseline as map_baseline
    _,legacy,maps,changes,roots,_=await map_baseline(core_settings)
    repo,service,submission,_,_=await execution(core_settings)
    await service.submit('demo',submission);run,owner=await claimed(repo)
    await execute_python(run,core_settings,repo,owner,asyncio.Event())
    receipt=(await repo.get_run(str(run['run_id'])))['executor_json']['python_attempt']
    maps.refresh('demo',roots=roots)
    documents=[item['document_ref'] for item in legacy.load('demo')['files'] if item.get('document_ref')]
    assert receipt['binding_ref'] in documents and receipt['settlement_ref'] in documents
    async with repo.database.connection() as c:
        records=await (await c.execute("SELECT * FROM assistant.project_file_changes WHERE batch_id=%s",
            (f"python-attempt:{run['run_id']}:1",))).fetchall()
    assert len(records)==2 and all(x['map_applied_generation'] for x in records)


async def test_actual_scoped_mcp_discovers_and_submits_fixed_python(core_settings):
    import os,sys
    from mcp import ClientSession,StdioServerParameters
    from mcp.client.stdio import stdio_client
    from assistant_kernel.services.workspace_scope import WorkspaceScopeService
    repo,service,submission,invocation,_=await execution(core_settings)
    async with repo.database.connection() as c:
        parent=await (await c.execute('SELECT r.* FROM assistant.runs r JOIN assistant.model_invocations i ON i.run_id=r.run_id WHERE i.invocation_id=%s',(invocation,))).fetchone()
        owner=str(uuid.uuid4());token=str(uuid.uuid4())
        await c.execute("UPDATE assistant.runs SET worker_instance_id=%s,claim_token=%s,lease_until=now()+interval '90 seconds' WHERE run_id=%s",
            (owner,token,parent['run_id']));await c.commit()
    uri,sha=WorkspaceScopeService(core_settings).snapshot('demo',str(parent['run_id']),'build')
    environment={**os.environ,'AI_HOME':str(core_settings.workspace_root),'ASSISTANT_DATA_ROOT':str(core_settings.data_root),
        'ASSISTANT_POSTGRES_HOST':core_settings.postgres_host,'ASSISTANT_POSTGRES_PORT':str(core_settings.postgres_port),
        'POSTGRES_DB':core_settings.postgres_database,'POSTGRES_USER':core_settings.postgres_user,
        'POSTGRES_PASSWORD':core_settings.postgres_password.get_secret_value(),'ASSISTANT_PROVIDER_SECRETS':str(core_settings.provider_secrets_path),
        'KERNEL_SCOPE_PROJECT_ID':'demo','KERNEL_SCOPE_RUN_ID':str(parent['run_id']),'KERNEL_SCOPE_CLAIM_TOKEN':token,
        'KERNEL_SCOPE_WORKER_INSTANCE_ID':owner,'KERNEL_SCOPE_ALLOWED_TOOLS':'get_authorized_workspace,current_conversation_route,submit_project_python',
        'KERNEL_SCOPE_STAGE':'build','KERNEL_SCOPE_SNAPSHOT_URI':uri,'KERNEL_SCOPE_SNAPSHOT_SHA256':sha,
        'PYTHONPATH':str(Path(__file__).parents[1]/'src')}
    async with stdio_client(StdioServerParameters(command=sys.executable,args=['-m','assistant_kernel.mcp_server.scoped_tools'],env=environment)) as (read,write):
        async with ClientSession(read,write) as session:
            await session.initialize()
            assert 'submit_project_python' in {x.name for x in (await session.list_tools()).tools}
            assert not (await session.call_tool('get_authorized_workspace',{})).is_error
            context=await session.call_tool('current_conversation_route',{})
            assert json.loads(context.content[0].text)['python_execution_context']['creating_invocation_ref']['id']==invocation
            result=await session.call_tool('submit_project_python',submission)
    assert not result.is_error
    admission=json.loads(result.content[0].text)
    assert admission['status']=='queued' and (await repo.get_run(admission['run_ref']['id']))['run_type']=='python'
