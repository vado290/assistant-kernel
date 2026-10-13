from copy import deepcopy
import uuid

import pytest
from psycopg.types.json import Jsonb
from assistant_kernel.repositories import CoreRepository
from assistant_kernel.services.contracts import contracts
from assistant_kernel.services.document_runtime import resource_ref
from assistant_kernel.services.session_boundaries import SessionBoundaryService
from test_core4 import core_settings
from test_v12_foundations import source


async def setup(settings):
    repo=CoreRepository(settings)
    incoming=await repo.ingest_message(project_id='demo',agent_key='project:demo:manager',channel='test',chat_id='boundary',user_id='owner',external_event_id=str(uuid.uuid4()),external_message_id=None,content={'text':'isolated phase boundary'})
    task=incoming['run'];task_id=str(task['run_id'])
    scope={'agent_key':task['agent_key'],'channel':'test','chat_id':'boundary:task:'+task_id}
    session=await repo.ensure_session('demo',**scope)
    async with repo.database.connection() as c:
        await c.execute('UPDATE assistant.runs SET session_id=%s,request_json=request_json || %s WHERE run_id=%s',
            (session['session_id'],Jsonb({'current_stage':'requirements','work_mode':'requirements','state_revision':2,'context_epoch':0}),task_id))
        await c.commit()
    ref=resource_ref(kind='receipt',identity=task_id,uri='db://assistant/runs/'+task_id,purpose='actual isolated Task')
    policy=resource_ref(kind='source',identity='fixed-policy',uri='vault://demo/policy.json',sha256='0'*64,purpose='isolated fixed policy')
    request={'task_ref':ref,'from_mode':'requirements','target_mode':'design',
        'expected_state_revision':2,'expected_context_epoch':0,'action':'fresh','reason':'phase_changed',
        'source_refs':[source()],'mandatory_child_refs':[],'context_policy_ref':policy}
    return repo,task_id,session,scope,request


async def test_atomic_boundary_calls_stage_gate_once_and_retries_do_not_create_sessions(core_settings):
    repo,task_id,previous,scope,request=await setup(core_settings)
    service=SessionBoundaryService(core_settings);called=[]
    async def gate(connection,task,body):
        assert task['request_json']['current_stage']=='requirements'
        called.append(str(task['run_id']))
    with pytest.raises(PermissionError):await service.commit('demo',request,session_scope=scope)
    receipt=await service.commit('demo',request,session_scope=scope,validate_transition=gate)
    contracts().validate('SessionBoundaryReceipt',receipt)
    repeat=await service.commit('demo',request,session_scope=scope,validate_transition=gate)
    assert repeat['status']=='duplicate' and repeat['next_session_ref']==receipt['next_session_ref']
    assert called==[task_id]
    assert (await repo.get_session(str(previous['session_id'])))['status']=='sealed'
    next_session=await repo.get_session(receipt['next_session_ref']['id'])
    assert next_session['native_session_id'] is None
    assert next_session['parent_session_id']==previous['session_id']
    task=await repo.get_run(task_id)
    assert task['request_json']['current_stage']=='design'
    assert task['request_json']['state_revision']==3 and task['request_json']['context_epoch']==1


async def test_boundary_gate_failure_rolls_back_and_keep_does_not_advance_context_epoch(core_settings):
    repo,task_id,previous,scope,request=await setup(core_settings)
    service=SessionBoundaryService(core_settings)
    async def denied(connection,task,body):raise PermissionError('required business result missing')
    with pytest.raises(PermissionError):
        await service.commit('demo',request,session_scope=scope,validate_transition=denied)
    assert (await repo.get_session(str(previous['session_id'])))['status']=='active'
    assert (await repo.get_run(task_id))['request_json']['state_revision']==2
    keep={**request,'target_mode':'requirements','action':'keep','reason':'none'}
    async def keep_gate(connection,task,body):
        assert body['action']=='keep' and body['target_mode']==task['request_json']['current_stage']
    result=await service.commit('demo',keep,session_scope=scope,validate_transition=keep_gate)
    assert result['context_epoch']==0
    assert result['previous_session_ref']==result['next_session_ref']
    stale={**request,'source_refs':[source()]}
    with pytest.raises(ValueError,match='stale'):
        await service.commit('demo',stale,session_scope=scope,validate_transition=denied)
