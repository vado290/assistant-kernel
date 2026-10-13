import asyncio, uuid
import pytest
from psycopg.types.json import Jsonb

from assistant_kernel.repositories import CoreRepository
from assistant_kernel.services.question_groups import QuestionGroupService
from test_core4 import core_settings


async def setup_group(settings):
    repo=CoreRepository(settings)
    incoming=await repo.ingest_message(project_id='demo',agent_key='project:demo:manager',
        channel='discord',chat_id='pm-chat',user_id='owner',external_event_id=str(uuid.uuid4()),
        external_message_id=None,content={'text':'clarify two requirements'})
    questions=[{'question_id':name,'question_text':'Complete question '+name,'topic':name,
        'required':True,'options':[],'recommended_option_id':None,'recommendation_reason':'','source_refs':[]}
        for name in ('scope','environment')]
    service=QuestionGroupService(settings,repo)
    async with repo.database.connection() as c:
        owner=await (await c.execute("UPDATE assistant.runs SET status='waiting_user',request_json=request_json || %s WHERE run_id=%s RETURNING *",
            (Jsonb({'phase_protocol':'five-stage.v1','current_stage':'requirements'}),incoming['run']['run_id']))).fetchone()
        canonical=await service.create_locked(c,owner=owner,questions=questions,
            source_message_id=str(incoming['message']['message_id']),routes=[
                {'channel':'discord','chat_id':'pm-chat','user_id':'owner','interface_role':'pm','is_origin':True},
                {'channel':'discord','chat_id':'supervisor-chat','user_id':'owner','interface_role':'supervisor'}])
        await c.commit()
    ids=canonical['content_json']['projection_ids']
    return repo,service,owner,canonical,ids


async def command(repo,owner,chat,name):
    return (await repo.record_channel_command(project_id='demo',agent_key=owner['agent_key'],channel='discord',chat_id=chat,
        user_id='owner',external_event_id=str(uuid.uuid4()),external_message_id=None,
        content={'text':'/'+name,'command':name,'context_visibility':'ui_only'}))['message']


async def action(service,identity,chat,verb,*,rev,page,value=None,event=None,user='owner'):
    return await service.record_action(message_id=identity,project_id='demo',channel='discord',
        chat_id=chat,user_id=user,external_event_id=event or str(uuid.uuid4()),revision=rev,page=page,action=verb,value=value)


async def test_new_chat_only_withdraws_current_view_and_pending_restore_keeps_shared_draft(core_settings):
    repo,service,owner,canonical,ids=await setup_group(core_settings)
    await action(service,ids[0],'pm-chat','text',rev=0,page=0,value='saved partial draft')
    async with repo.database.connection() as c:
        await c.execute('UPDATE assistant.runs SET request_json=request_json || %s WHERE run_id=%s',(Jsonb({'protocol_revision':'1.2'}),owner['run_id']));await c.commit()
    source=await command(repo,owner,'pm-chat','newchat')
    reset=await repo.reset_conversation(project_id='demo',channel='discord',chat_id='pm-chat',user_id='owner',
        source_message_id=str(source['message_id']),idempotency_key='canonical-reset')
    assert not reset['cancelled_run_ids'] and not reset['cancel_requested_run_ids']
    async with repo.database.connection() as c:
        group=await (await c.execute('SELECT content_json FROM assistant.messages WHERE message_id=%s',(canonical['message_id'],))).fetchone()
    assert group['content_json']['group']['status']=='open'
    assert group['content_json']['group']['draft']['answers']['scope']=='saved partial draft'
    assert (await repo.get_message(ids[0]))['content_json']['question_projection']['view_status']=='withdrawn'
    assert (await repo.get_message(ids[1]))['content_json']['question_projection']['view_status']=='active'
    pending=await repo.list_pending_channel_runs(project_id='demo',channel='discord',chat_id='pm-chat',user_id='owner')
    assert pending[0]['question_content']['questionnaire']['answers']['scope']=='saved partial draft'
    restore_source=await command(repo,owner,'supervisor-chat','resume')
    restored=await repo.resume_waiting_run(run_id=str(owner['run_id']),project_id='demo',channel='discord',chat_id='supervisor-chat',
        user_id='owner',source_message_id=str(restore_source['message_id']),idempotency_key='canonical-restore-secondary')
    assert str(restored['message']['message_id']) not in ids
    assert restored['message']['content_json']['questionnaire']['answers']['scope']=='saved partial draft'
    assert (await repo.get_run(str(owner['run_id'])))['status']=='waiting_user'
    with pytest.raises(ValueError,match='stale'):await action(service,ids[0],'pm-chat','next',rev=1,page=0,event='withdrawn-button')


async def test_new_chat_preserves_queued_new_protocol_task_and_cancel_closes_questions_without_answering(core_settings):
    repo,service,owner,canonical,ids=await setup_group(core_settings)
    await action(service,ids[0],'pm-chat','text',rev=0,page=0,value='unsubmitted draft')
    async with repo.database.connection() as c:
        await c.execute("UPDATE assistant.runs SET status='queued',request_json=request_json || %s WHERE run_id=%s",
            (Jsonb({'protocol_revision':'1.2'}),owner['run_id']));await c.commit()
    source=await command(repo,owner,'pm-chat','newchat')
    await repo.reset_conversation(project_id='demo',channel='discord',chat_id='pm-chat',user_id='owner',
        source_message_id=str(source['message_id']),idempotency_key='queued-canonical-reset')
    assert (await repo.get_run(str(owner['run_id'])))['status']=='queued'
    assert await repo.request_cancel(str(owner['run_id']))
    async with repo.database.connection() as c:
        group=(await (await c.execute('SELECT content_json FROM assistant.messages WHERE message_id=%s',(canonical['message_id'],))).fetchone())['content_json']['group']
    assert group['status']=='cancelled' and not group['accepted_answers']
    assert group['draft']['answers']['scope']=='unsubmitted draft'
    assert (await repo.get_run(str(owner['run_id'])))['status']=='cancelled'


async def test_two_projections_share_one_draft_and_submit_once_into_supervision(core_settings):
    repo,service,owner,canonical,ids=await setup_group(core_settings)
    first=await action(service,ids[0],'pm-chat','text',rev=0,page=0,value='first scope')
    async with repo.database.connection() as c:
        raw=await (await c.execute('SELECT * FROM assistant.messages WHERE message_id=%s',(ids[1],))).fetchone()
        assert 'questions' not in raw['content_json'] and 'questionnaire' not in raw['content_json']
        mirror=await service.hydrate(c,raw)
    assert mirror['content_json']['questionnaire']['answers']['scope']=='first scope'
    await action(service,ids[1],'supervisor-chat','next',rev=1,page=0)
    await action(service,ids[0],'pm-chat','text',rev=2,page=1,value='local container')
    await action(service,ids[1],'supervisor-chat','next',rev=3,page=1)
    results=await asyncio.gather(
        action(service,ids[0],'pm-chat','submit',rev=4,page=2),
        action(service,ids[1],'supervisor-chat','submit',rev=4,page=2),return_exceptions=True)
    assert sum(isinstance(item,dict) and item['accepted_into_supervision'] for item in results)==1
    assert all(not item['resumed'] for item in results if isinstance(item,dict))
    task=await repo.get_run(str(owner['run_id']))
    assert task['status']=='waiting_supervisor'
    async with repo.database.connection() as c:
        group=(await (await c.execute('SELECT content_json FROM assistant.messages WHERE message_id=%s',(canonical['message_id'],))).fetchone())['content_json']['group']
    assert group['status']=='answered' and len(group['accepted_answers'])==2
    assert {a['question_id'] for a in group['accepted_answers']}=={'scope','environment'}


async def test_stale_and_wrong_owner_input_are_recorded_without_accepting_answers(core_settings):
    repo,service,owner,canonical,ids=await setup_group(core_settings)
    await action(service,ids[0],'pm-chat','text',rev=0,page=0,value='scope')
    with pytest.raises(ValueError):await action(service,ids[1],'supervisor-chat','text',rev=0,page=0,value='late',event='late-card')
    with pytest.raises(PermissionError):await action(service,ids[0],'pm-chat','text',rev=1,page=0,value='foreign',event='foreign-card',user='another-owner')
    async with repo.database.connection() as c:
        rows=await (await c.execute("SELECT content_json FROM assistant.messages WHERE external_event_id=ANY(%s)",(['late-card','foreign-card'],))).fetchall()
        group=(await (await c.execute('SELECT content_json FROM assistant.messages WHERE message_id=%s',(canonical['message_id'],))).fetchone())['content_json']['group']
    assert len(rows)==2 and all(row['content_json']['acceptance']['status']=='rejected' for row in rows)
    assert group['accepted_answers']==[] and group['draft']['answers']['scope']=='scope'


async def test_repeated_same_event_does_not_advance_draft_twice(core_settings):
    repo,service,owner,canonical,ids=await setup_group(core_settings)
    first=await action(service,ids[0],'pm-chat','text',rev=0,page=0,value='scope',event='one-physical-event')
    duplicate=await action(service,ids[0],'pm-chat','text',rev=0,page=0,value='scope',event='one-physical-event')
    assert duplicate['duplicate'] and duplicate['message']['content_json']['questionnaire']['revision']==1


async def test_partial_text_correction_and_unmapped_input_preserve_formal_history(core_settings):
    repo,service,owner,canonical,ids=await setup_group(core_settings)
    projection=await repo.get_message(ids[1])
    first=await service.record_text(projection=projection,channel='discord',chat_id='supervisor-chat',
        user_id='owner',external_event_id='partial',text='scope: offline only\nAlso retain the history')
    assert first['message']['content_json']['questionnaire']['answers']=={'scope':'offline only'}
    await repo.validate_current_waiting_message(message_id=ids[0],project_id='demo',channel='discord',chat_id='pm-chat',user_id='owner')
    assert (await repo.get_run(str(owner['run_id'])))['status']=='waiting_supervisor'
    unmapped=await service.record_text(projection=projection,channel='discord',chat_id='supervisor-chat',
        user_id='owner',external_event_id='unmapped',text='I am not sure how to answer that')
    assert unmapped['unmapped']
    await service.record_text(projection=projection,channel='discord',chat_id='supervisor-chat',
        user_id='owner',external_event_id='complete',text='environment: local container')
    correction=await service.record_text(projection=projection,channel='discord',chat_id='supervisor-chat',
        user_id='owner',external_event_id='correction',text='更正 scope: read only analysis')
    assert correction['message']['content_json']['questionnaire']['answers']['scope']=='read only analysis'
    async with repo.database.connection() as c:
        body=(await (await c.execute('SELECT content_json FROM assistant.messages WHERE message_id=%s',(canonical['message_id'],))).fetchone())['content_json']['group']
        source=(await (await c.execute("SELECT content_json FROM assistant.messages WHERE external_event_id='partial'" )).fetchone())['content_json']
    assert len(body['accepted_answers'])==3 and body['accepted_answers'][0]['value']=='offline only'
    assert body['accepted_answers'][-1]['source_kind']=='correction' and body['accepted_answers'][-1]['supersedes_answer_ref']
    assert source['text']=='scope: offline only\nAlso retain the history'
    assert body['status']=='answered'


async def test_withdrawing_view_does_not_cancel_questions_and_old_buttons_reject(core_settings):
    repo,service,owner,canonical,ids=await setup_group(core_settings)
    async with repo.database.connection() as c:
        await service.withdraw_views_locked(c,owner_id=owner['run_id'],source_message_id=owner['trigger_message_id'])
        await c.commit()
    with pytest.raises(ValueError,match='stale'):
        await action(service,ids[0],'pm-chat','text',rev=0,page=0,value='old button')
    projection=await repo.get_message(ids[0])
    assert projection['content_json']['questionnaire']['status']=='submitted'
    await service.record_text(projection=projection,channel='discord',chat_id='pm-chat',
        user_id='owner',external_event_id='identified-new-text',text='scope: retained meaning')
    async with repo.database.connection() as c:
        group=(await (await c.execute('SELECT content_json FROM assistant.messages WHERE message_id=%s',(canonical['message_id'],))).fetchone())['content_json']['group']
    assert group['status']=='open' and group['accepted_answers'][0]['value']=='retained meaning'


async def test_delivery_reads_current_canonical_group_and_refresh_keeps_target_identity(core_settings):
    repo,service,owner,canonical,ids=await setup_group(core_settings)
    await repo.record_questionnaire_action(message_id=ids[0],project_id='demo',channel='discord',chat_id='pm-chat',
        user_id='owner',external_event_id='core-entry',revision=0,page=0,action='text',value='latest draft')
    claimed=await repo.claim_delivery('delivery',str(uuid.uuid4()),90,channel='discord')
    assert claimed['content_json']['questionnaire']['answers']['scope']=='latest draft'
    async with repo.database.connection() as c:
        await c.execute("UPDATE assistant.messages SET delivery_status='sent',external_message_id='100',worker_instance_id=NULL,lease_until=NULL WHERE message_id=ANY(%s::uuid[])",(ids,))
        await c.commit()
    refresh=await repo.claim_delivery('delivery',str(uuid.uuid4()),90,channel='discord')
    assert refresh['content_json']['target_message_id'] in ids
    assert refresh['content_json']['questionnaire']['answers']['scope']=='latest draft'
    assert refresh['content_json']['ui_update_only'] is True


async def test_late_correction_keeps_running_worker_lease_and_buffers_new_input(core_settings):
    repo,service,owner,canonical,ids=await setup_group(core_settings)
    projection=await repo.get_message(ids[0])
    await service.record_text(projection=projection,channel='discord',chat_id='pm-chat',user_id='owner',
        external_event_id='complete-before-execution',text='scope: offline\nenvironment: container')
    worker=str(uuid.uuid4());token=str(uuid.uuid4())
    async with repo.database.connection() as c:
        await c.execute("UPDATE assistant.runs SET status='running',worker_instance_id=%s,claim_token=%s,lease_until=now()+interval '90 seconds' WHERE run_id=%s",
            (worker,token,owner['run_id']));await c.commit()
    await service.record_text(projection=projection,channel='discord',chat_id='pm-chat',user_id='owner',
        external_event_id='late-correction',text='更正 scope: analysis only')
    after=await repo.get_run(str(owner['run_id']))
    assert after['status']=='running' and str(after['worker_instance_id'])==worker and str(after['claim_token'])==token
    assert after['request_json']['pending_supervision_source_id']
    assert after['request_json']['answer_state']['scope']=='analysis only'


async def test_answer_to_independent_consultation_keeps_pm_task_and_questions_unchanged(core_settings):
    repo,service,owner,canonical,ids=await setup_group(core_settings)
    before=await repo.get_run(str(owner['run_id']))
    async with repo.database.connection() as c:
        peer=await repo._queue_supervision_locked(c,before,str(owner['trigger_message_id']),reason='user_consultation')
        peer=await (await c.execute("UPDATE assistant.runs SET status='waiting_user' WHERE run_id=%s RETURNING *",(peer['run_id'],))).fetchone()
        questions=[{**canonical['content_json']['group']['questions'][0],'question_id':'consultation-topic'}]
        consultation=await service.create_locked(c,owner=peer,questions=questions,source_message_id=str(owner['trigger_message_id']),
            interaction_scope='supervisor_consultation',routes=[{'channel':'discord','chat_id':'supervisor-chat','user_id':'owner','interface_role':'supervisor'}])
        await c.commit()
    view=await repo.get_message(consultation['content_json']['projection_ids'][0])
    await service.record_text(projection=view,channel='discord',chat_id='supervisor-chat',user_id='owner',
        external_event_id='consultation-answer',text='A separate research topic')
    after=await repo.get_run(str(owner['run_id']))
    assert after['status']==before['status'] and after['request_json']==before['request_json']
    assert (await repo.get_run(str(peer['run_id'])))['status']=='queued'
    async with repo.database.connection() as c:
        original=(await (await c.execute('SELECT content_json FROM assistant.messages WHERE message_id=%s',(canonical['message_id'],))).fetchone())['content_json']['group']
    assert original['status']=='open' and original['accepted_answers']==[]


def test_kernel_configuration_owner_is_restricted_to_its_approved_scope():
    from copy import deepcopy
    from assistant_kernel.services.contracts import contracts,ContractError
    from assistant_kernel.services.document_runtime import resource_ref
    group={'group_id':'system-question','owner_ref':resource_ref(kind='receipt',identity='configuration-request',purpose='Kernel validation owner'),
        'owner_role':'kernel','content_version':'configuration-v1',
        'questions':[{'question_id':'model','question_text':'Choose the configured model','topic':'model','required':True,
            'options':[],'recommended_option_id':None,'recommendation_reason':'','source_refs':[]}],
        'draft':{'revision':0,'cursor':0,'answers':{},'choices':{},'additional_text':''},'accepted_answers':[],
        'status':'open','blocking':True,'interaction_scope':'system_configuration','display_policy':'kernel_management','runtime_owner':None}
    contracts().validate('V12_QuestionGroup',group)
    forbidden=deepcopy(group);forbidden.update(interaction_scope='pm_business',display_policy='pm_both')
    with pytest.raises(ContractError):contracts().validate('V12_QuestionGroup',forbidden)
    forbidden=deepcopy(group);forbidden['owner_role']='pm'
    with pytest.raises(ContractError):contracts().validate('V12_QuestionGroup',forbidden)
