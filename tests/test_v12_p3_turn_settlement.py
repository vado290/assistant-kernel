import uuid
import pytest
from psycopg.types.json import Jsonb

from assistant_kernel.services.pm_turns_v12 import PMTurnsV12
from test_core4 import core_settings
from test_v12_p3_prompt_protocol import preparation,confirm
from test_v12_p3_results import active_result
from test_v12_foundations import requirement
from assistant_kernel.services.document_runtime import resource_ref


@pytest.mark.parametrize('prior_failure',[False,True])
async def test_typed_turn_settles_with_durable_supervision_and_no_automatic_next_pm(core_settings,prior_failure):
    repo,compiler,task,display,_=await preparation(core_settings)
    async with repo.database.connection() as c:
        await c.execute("UPDATE assistant.messages SET delivery_status='sent' WHERE message_id=%s",(display['message_id'],));await c.commit()
    await confirm(compiler,display,event='one-real-confirmation')
    original=await repo.get_message(str(task['trigger_message_id']))
    async with repo.database.connection() as c:
        previous_reply=await repo._create_outbound_locked(c,project_id='demo',session_id=None,run_id=str(task['run_id']),
            channel=original['channel'],chat_id=original['chat_id'],user_id=original['user_id'],message_type='reply',
            content={'text':'A preserved earlier reply'},reply_to_message_id=original['message_id'],idempotency_key='prior-turn-reply')
        await c.commit()
    worker=str(uuid.uuid4());claim=str(uuid.uuid4())
    previous_error={'type':'PermissionError','message':'Historical failure, not this successful invocation'} if prior_failure else None
    failure_ref=compiler.write_blob('demo',str(task['run_id']),'historical-failure',previous_error) if prior_failure else None
    async with repo.database.connection() as c:
        task=await (await c.execute("UPDATE assistant.runs SET status='running',worker_instance_id=%s,claim_token=%s,lease_until=now()+interval '90 seconds',error_json=%s,request_json=request_json || %s WHERE run_id=%s RETURNING *",
            (worker,claim,Jsonb(previous_error) if previous_error else None,Jsonb({'supervision_failure':'old failure' if prior_failure else None,'historical_failure_ref':failure_ref}),task['run_id']))).fetchone();await c.commit()
    result=active_result();result['summary']='Actual bounded progress, no phase completion claim'
    settled=await PMTurnsV12(core_settings,repo).settle(run=task,worker_instance_id=worker,public_result=result,usage_snapshot={})
    after=await repo.get_run(str(task['run_id']))
    assert settled['status']=='waiting_supervisor' and after['status']=='waiting_supervisor'
    assert after['request_json']['task_turn_count']==1 and after['worker_instance_id'] is None
    assert after['error_json'] is None and after['request_json']['supervision_failure'] is None
    await repo.reconcile_pending_supervision()
    reconciled=await repo.get_run(str(task['run_id']))
    assert reconciled['request_json']['task_turn_count']==1
    assert not reconciled['request_json']['accepted_documents'].get('failed_turn')
    if prior_failure:
        assert compiler.read_blob('demo',failure_ref)==previous_error
        assert reconciled['request_json']['historical_failure_ref']==failure_ref
    receipt=settled['settlement'];supervisor=await repo.get_run(receipt['supervisor_run_ref']['id'])
    assert supervisor['status']=='queued' and supervisor['agent_key']=='project:demo:supervisor'
    assert receipt['pending_supervision'] and receipt['raw_result_ref']
    turn=compiler.documents.read('demo',receipt['turn_ref'])
    assert turn['payload']['prompt_ref']==display['content_json']['prompt_ref']
    assert turn['payload']['turn_number']==1 and turn['payload']['outcome']=='continue'
    reply=await repo.get_message(after['request_json']['pending_supervision_source_id'])
    assert str(reply['reply_to_message_id'])==after['request_json']['prompt_confirmation_message_id']
    assert (await repo.get_message(str(previous_reply['message_id'])))['content_json']['text']=='A preserved earlier reply'


async def test_completed_requirement_turn_still_creates_specific_approval_and_supervision(core_settings):
    repo,compiler,task,display,_=await preparation(core_settings)
    async with repo.database.connection() as c:
        await c.execute("UPDATE assistant.messages SET delivery_status='sent' WHERE message_id=%s",(display['message_id'],));await c.commit()
    await confirm(compiler,display,event='complete-turn-confirmation')
    worker=str(uuid.uuid4());claim=str(uuid.uuid4())
    async with repo.database.connection() as c:
        task=await (await c.execute("UPDATE assistant.runs SET status='running',worker_instance_id=%s,claim_token=%s,lease_until=now()+interval '90 seconds' WHERE run_id=%s RETURNING *",
            (worker,claim,task['run_id']))).fetchone();await c.commit()
    result=active_result();result['outcome']='completed';report=requirement()
    source=resource_ref(kind='message',identity=str(task['trigger_message_id']),uri='db://assistant/messages/'+str(task['trigger_message_id']),purpose='original fixture input')
    report['requirements'][0]['source_refs']=[source];result['payload']['report_payload']=report
    settled=await PMTurnsV12(core_settings,repo).settle(run=task,worker_instance_id=worker,public_result=result,usage_snapshot={})
    after=await repo.get_run(str(task['run_id']))
    assert settled['status']=='waiting_user' and after['request_json']['approval_kind']=='requirements'
    assert not after['request_json'].get('requirements_approval')
    card=await repo.get_message(str(after['waiting_question_message_id']))
    assert card['content_json']['report_ref']==after['request_json']['accepted_documents']['task_contract']
    assert settled['settlement']['supervisor_run_ref']
    from assistant_kernel.services.supervisor_v12 import SupervisorV12
    supervisor_worker=str(uuid.uuid4())
    control=await repo.claim_run('after-requirement',supervisor_worker,90)
    assert str(control['run_id'])==settled['settlement']['supervisor_run_ref']['id']
    service=SupervisorV12(core_settings,repo)
    context=await service.prepare_context(control)
    visible=service.model_context(control,context)
    phase=visible['prompt_compilation_context']['phase_state']
    assert phase['current_mode']=='requirements'
    assert phase['last_settled_turn']['outcome']=='completed'
    assert phase['pending_document_approval_ref']==card['content_json']['report_ref']
    assert phase['pending_document_payload']==report
    assert 'task_contract' not in visible['accepted_document_contents']
    assert 'last_turn' not in visible['accepted_document_contents']


@pytest.mark.parametrize('prior_feedback,decision', [(False, 'revise'), (True, 'revise'), (True, 'approve')])
async def test_approval_feedback_keeps_same_task_and_requires_new_prompt(core_settings, prior_feedback, decision):
    repo, compiler, task, display, _ = await preparation(core_settings, project_policy={
        'layout_version':1, 'task_protocol':'single-agent.v2', 'phase_protocol':'five-stage.v1', 'supervisor':{'enabled':True}})
    async with repo.database.connection() as c:
        await c.execute("UPDATE assistant.messages SET delivery_status='sent' WHERE message_id=%s", (display['message_id'],))
        await c.commit()
    await confirm(compiler, display, event='feedback-first-confirmation')
    worker = str(uuid.uuid4())
    async with repo.database.connection() as c:
        task = await (await c.execute("""UPDATE assistant.runs SET status='running',worker_instance_id=%s,
            claim_token=%s,lease_until=now()+interval '90 seconds',mutex_key='engineering:demo',request_json=request_json || %s
            WHERE run_id=%s RETURNING *""",
            (worker, str(uuid.uuid4()), Jsonb({'task_protocol':'single-agent.v2','phase_protocol':'five-stage.v1'}), task['run_id']))).fetchone()
        await c.commit()
    result = active_result(); result['outcome'] = 'completed'
    report = requirement()
    report['requirements'][0]['source_refs'] = [resource_ref(kind='message', identity=str(task['trigger_message_id']),
        uri='db://assistant/messages/'+str(task['trigger_message_id']), purpose='original fixture input')]
    result['payload']['report_payload'] = report
    settled = await PMTurnsV12(core_settings, repo).settle(run=task, worker_instance_id=worker,
        public_result=result, usage_snapshot={})
    after = await repo.get_run(str(task['run_id']))
    card = await repo.get_message(str(after['waiting_question_message_id']))
    old_confirmation = after['request_json']['prompt_confirmation_message_id']
    async with repo.database.connection() as c:
        await c.execute("UPDATE assistant.runs SET status='succeeded' WHERE run_id=%s",
            (settled['settlement']['supervisor_run_ref']['id'],))
        await c.commit()
    if prior_feedback:
        supplemental = await repo.ingest_message(project_id='demo', agent_key=task['agent_key'],
            channel=card['channel'], chat_id=card['chat_id'], user_id=card['user_id'],
            external_event_id='feedback-supplement', external_message_id=None, content={'text':'Correct the attribution only'})
        assert supplemental['active_task']['run_id'] == task['run_id']
    response = await repo.ingest_message(project_id='demo', agent_key=task['agent_key'],
        channel=card['channel'], chat_id=card['chat_id'], user_id=card['user_id'],
        external_event_id='feedback-card-action', external_message_id=None, reply_to_message_id=str(card['message_id']),
        content={'text':'Correct the attribution only' if decision == 'revise' else 'approve', 'decision':decision})
    assert not response['correction_run']
    assert response['run']['run_id'] == task['run_id']
    after = await repo.get_run(str(task['run_id']))
    assert after['status'] == 'waiting_supervisor'
    assert after['request_json']['task_turn_count'] == 1
    assert after['request_json']['prompt_confirmation_message_id'] == old_confirmation
    assert after['request_json']['accepted_documents']['task_contract'] == card['content_json']['report_ref']
    assert not after['request_json'].get('requirements_approval')
    accepted = await repo.get_message(str(response['message']['message_id']))
    assert accepted['content_json']['task_input_status'] == 'pending'
    claimed = await repo.claim_run('feedback-supervision', str(uuid.uuid4()), 90)
    assert claimed['agent_key'] == 'project:demo:supervisor'
    assert claimed['request_json']['task_id'] == str(task['run_id'])
    assert claimed['request_json']['source_context_epoch'] == after['request_json']['supervisor_context_epoch']
    assert await repo.claim_run('no-pm-replay', str(uuid.uuid4()), 90) is None
