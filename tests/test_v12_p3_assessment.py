from copy import deepcopy
from datetime import datetime,timedelta,timezone
import uuid

import pytest
from psycopg.types.json import Jsonb

from assistant_kernel.repositories import CoreRepository
from assistant_kernel.services.supervisor_v12 import SupervisorV12,empty_intent
from assistant_kernel.services.supervisor_assessment_v12 import SupervisorAssessmentV12
from test_core4 import core_settings
from test_v12_p3_admission import complete_package


async def captured(settings,text='Prepare an offline report'):
    complete_package(settings);repo=CoreRepository(settings)
    incoming=await repo.ingest_message(project_id='demo',agent_key='project:demo:manager',channel='test',chat_id='assessment',
        user_id='owner',external_event_id='assessment-source',external_message_id=None,content={'text':text})
    worker=str(uuid.uuid4());control=await repo.claim_run('assessment-test',worker,90)
    service=SupervisorV12(settings,repo);context=await service.prepare_context(control)
    snapshot=service.documents.read('demo',context['snapshot_ref'])['payload']
    assessment={'status':'evaluated','assessed_at':datetime.now(timezone.utc).isoformat(),
        'evaluated_source_batch_id':snapshot['facts_basis']['source_batch_id'],
        'covered_task_versions':snapshot['facts_basis']['task_versions'],'covered_source_refs':snapshot['facts_basis']['source_refs'],
        'evidence_refs':context['user_message_refs'],'summary':'Current Task facts assessed; wider runtime coverage is partial','blockers':[]}
    decision={'questions':[],'summary':'需要进一步准备具体执行包。','prompt_proposal_ref':None,
        'public_result':{'fixture':'Supervisor output supplied to test settlement'},'insufficient_evidence':False,
        'project_assessment':assessment,'user_intent_context':empty_intent()}
    return repo,service,incoming['run'],control,worker,context,decision


async def test_assessment_keeps_partial_coverage_and_never_dispatches_without_prompt(core_settings):
    repo,service,task,control,worker,context,decision=await captured(core_settings)
    result=await service.settle(control=control,worker_instance_id=worker,decision=decision)
    assert not result['pm_dispatched'] and result['assessment_commit']['status']=='accepted'
    snapshot=service.documents.read('demo',result['assessment_commit']['assessment_ref'])['payload']
    assert snapshot['project_assessment']['status']=='partial' and snapshot['coverage']['status']=='partial'
    assert (await repo.get_run(str(task['run_id'])))['status']=='waiting_supervisor'


async def test_supervisor_reads_committed_validation_without_turning_it_into_user_input(core_settings):
    repo,service,task,control,worker,_,_=await captured(core_settings)
    evidence=service.prompts.write_blob('demo',str(task['run_id']),'validation-test',{'scope':'structure','business_acceptance':'pending_review'})
    async with repo.database.connection() as c:
        await c.execute('UPDATE assistant.runs SET request_json=request_json || %s WHERE run_id=%s',
            (Jsonb({'validation_gate_status':'pass','validation_artifact':evidence,'audit_complete':False}),task['run_id']))
        await c.commit()
    context=await service.prepare_context(control)
    assert context['phase_state']['validation_status']=='pass'
    assert context['phase_state']['audit_complete'] is False
    assert all(ref['kind']=='message' for ref in context['user_message_refs'])
    snapshot=service.documents.read('demo',context['snapshot_ref'])['payload']
    assert context['phase_state']['validation_ref'] in snapshot['facts_basis']['source_refs']
    assert snapshot['project_assessment']['status']=='facts_only'


async def test_reference_description_does_not_change_captured_source_identity(core_settings):
    repo,service,task,control,worker,context,decision=await captured(core_settings)
    original=decision['project_assessment']['covered_source_refs'][0]
    decision['project_assessment']['covered_source_refs'][0]={**original,'purpose':'Primary source used for this assessment'}
    result=await service.settle(control=control,worker_instance_id=worker,decision=decision)
    accepted=service.documents.read('demo',result['assessment_commit']['assessment_ref'])['payload']
    assert accepted['project_assessment']['covered_source_refs'][0]==original
    assert accepted['project_assessment']['status']=='partial' and not result['pm_dispatched']


@pytest.mark.parametrize('legacy_context',[False,True])
async def test_actual_outbound_trigger_is_captured_without_becoming_user_authorization(core_settings,legacy_context):
    repo,service,task,control,worker,context,decision=await captured(core_settings)
    async with repo.database.connection() as c:
        trigger=await repo._create_outbound_locked(c,project_id='demo',session_id=None,run_id=str(task['run_id']),
            channel='test',chat_id='assessment',user_id='owner',message_type='reply',content={'text':'Actual prior PM result'},
            reply_to_message_id=task['trigger_message_id'],idempotency_key='actual-pm-result-trigger')
        control=await (await c.execute('UPDATE assistant.runs SET trigger_message_id=%s WHERE run_id=%s RETURNING *',
            (trigger['message_id'],control['run_id']))).fetchone();await c.commit()
    context=await service.prepare_context(control)
    snapshot=service.documents.read('demo',context['snapshot_ref'])['payload']
    ref=context['trigger_message_ref']
    assert ref in snapshot['facts_basis']['source_refs']
    assert ref not in context['user_message_refs']
    assert service.model_context(control,context,trigger)['trigger_message']['message_id']==ref['id']
    decision['project_assessment']['covered_source_refs']=[ref]
    if legacy_context:
        snapshot['facts_basis']['source_refs']=[x for x in snapshot['facts_basis']['source_refs'] if x!=ref]
        altered=service.documents.write_scoped_revision(project_id='demo',scope='run',object_id='legacy-trigger-snapshot',
            run_id=str(control['run_id']),task_id=str(task['run_id']),document_kind='project-snapshot',document_schema='project-snapshot.v1',
            payload=snapshot,status='draft',actor_type='kernel',actor_id='kernel',reason='Pre-patch fixture omitted trigger Ref',source_refs=[])
        context={**context,'snapshot_ref':altered};context.pop('trigger_message_ref')
    raw=service.prompts.write_blob('demo',str(task['run_id']),'actual-test-result',{'scope':'fixture'})
    async with repo.database.connection() as c:
        result=await SupervisorAssessmentV12(core_settings,repo).commit_locked(c,control=control,context=context,decision=decision,raw_ref=raw,stale=False)
        await c.commit()
    assert result['status']=='accepted'
    intent=empty_intent();intent['explicit_goals']=[{'statement':'Invented user approval from an outbound reply','source_refs':[ref],'observed_at':datetime.now(timezone.utc).isoformat()}]
    with pytest.raises(PermissionError,match='original same-Project human'):
        async with repo.database.connection() as c:
            await SupervisorAssessmentV12(core_settings,repo).intent_locked(c,'demo',intent,None)


async def test_invalid_auxiliary_intent_does_not_block_valid_evaluation_or_become_authority(core_settings):
    repo,service,task,control,worker,context,decision=await captured(core_settings)
    intent=empty_intent();intent['current_concerns']=[{'statement':'An inferred concern from a work document',
        'source_refs':[context['snapshot_ref']],'observed_at':datetime.now(timezone.utc).isoformat()}]
    decision['user_intent_context']=intent
    decision['public_result']={'fixture':'raw model assertion retained','user_intent_context':intent}
    async with repo.database.connection() as c:
        with pytest.raises(PermissionError,match='original user messages'):
            await SupervisorAssessmentV12(core_settings,repo).intent_locked(c,'demo',intent,None)
    result=await service.settle(control=control,worker_instance_id=worker,decision=decision)
    saved=service.documents.read('demo',result['assessment_commit']['assessment_ref'])['payload']
    assert saved['user_intent_context']==empty_intent()
    assert any('User-intent update rejected' in note for note in saved['coverage']['notes'])
    assert service.prompts.read_blob('demo',result['raw_result_ref'])['user_intent_context']==intent
    assert result['outcome']=='settled' and not result['pm_dispatched']
    assert not (await repo.get_run(str(task['run_id'])))['request_json'].get('prompt_confirmation_ref')


async def test_completion_blocker_is_reported_without_advancing_or_replaying_work(core_settings,monkeypatch):
    repo,service,task,control,worker,context,decision=await captured(core_settings)
    async with repo.database.connection() as c:
        await c.execute('UPDATE assistant.runs SET request_json=request_json || %s WHERE run_id=%s',
            (Jsonb({'audit_complete':True}),task['run_id']));await c.commit()
    async def blocked(*args,**kwargs):raise PermissionError('Required runtime activation has no accepted actual receipt')
    monkeypatch.setattr('assistant_kernel.services.phase_control_v12.PhaseControlV12.complete_locked',blocked)
    result=await service.settle(control=control,worker_instance_id=worker,decision=decision)
    async with repo.database.connection() as c:
        rows=await (await c.execute("SELECT content_json FROM assistant.messages WHERE run_id=%s AND message_type='kernel_notice'",(control['run_id'],))).fetchall()
    notices=[row['content_json'] for row in rows if row['content_json'].get('blocking_reason')]
    assert len(notices)==1 and notices[0]['blocking_reason']==result['capability_gap']
    assert '任务尚未完成' in notices[0]['text']
    assert (await repo.get_run(str(task['run_id'])))['status']=='waiting_supervisor'
    assert result['pm_dispatched'] is False


async def test_captured_facts_container_is_valid_coverage_but_not_unread_project_wide_evidence(core_settings):
    repo,service,task,control,worker,context,decision=await captured(core_settings)
    decision['project_assessment']['covered_source_refs']=[context['facts_basis_ref']]
    result=await service.settle(control=control,worker_instance_id=worker,decision=decision)
    saved=service.documents.read('demo',result['assessment_commit']['assessment_ref'])['payload']
    assert saved['project_assessment']['covered_source_refs']==[context['facts_basis_ref']]
    assert saved['coverage']['status']=='partial' and saved['project_assessment']['status']=='partial'


async def test_stale_assessment_is_history_only_and_does_not_replace_project_head(core_settings):
    repo,service,task,control,worker,context,decision=await captured(core_settings)
    async with repo.database.connection() as c:
        await c.execute('UPDATE assistant.runs SET request_json=request_json || %s WHERE run_id=%s',(Jsonb({'state_revision':11}),task['run_id']));await c.commit()
    result=await service.settle(control=control,worker_instance_id=worker,decision=decision)
    assert result['stale'] and result['assessment_commit']['status']=='stale' and not result['pm_dispatched']
    async with repo.database.connection() as c:
        assert await SupervisorAssessmentV12(core_settings,repo).previous_locked(c,'demo') is None


async def test_hypothesis_expiry_comes_from_original_expression_and_cannot_be_renewed(core_settings):
    repo,service,task,control,worker,context,decision=await captured(core_settings)
    now=datetime.now(timezone.utc);observed=now-timedelta(days=5);ref=context['user_message_refs'][0]
    hypothesis={'hypothesis_id':'bounded-report','statement':'User may prefer a concise report','confidence':'low',
        'source_refs':[ref],'observed_at':now.isoformat(),'expires_at':(now+timedelta(days=365)).isoformat(),'is_authorization':False}
    intent=empty_intent();intent['hypotheses']=[hypothesis];service2=SupervisorAssessmentV12(core_settings,repo)
    async with repo.database.connection() as c:
        await c.execute('UPDATE assistant.messages SET created_at=%s WHERE message_id=%s',(observed,ref['id']))
        first=await service2.intent_locked(c,'demo',intent,None)
        assert datetime.fromisoformat(first['hypotheses'][0]['expires_at'])==observed+timedelta(days=7)
        previous={'user_intent_context':first};previous['user_intent_context']['hypotheses'][0]['expires_at']=(now+timedelta(hours=1)).isoformat()
        again=await service2.intent_locked(c,'demo',intent,previous)
        assert again['hypotheses'][0]['expires_at']==previous['user_intent_context']['hypotheses'][0]['expires_at']
    assert service2.visible_intent(first,now+timedelta(days=3))['hypotheses']==[]


async def test_ui_event_is_not_user_intent_or_authorization(core_settings):
    repo,service,task,control,worker,context,decision=await captured(core_settings)
    ref=context['user_message_refs'][0];intent=empty_intent()
    intent['explicit_goals']=[{'statement':'invented business approval','source_refs':[ref],'observed_at':datetime.now(timezone.utc).isoformat()}]
    async with repo.database.connection() as c:
        await c.execute('UPDATE assistant.messages SET content_json=content_json || %s WHERE message_id=%s',(Jsonb({'context_visibility':'ui_only'}),ref['id']))
        with pytest.raises(PermissionError,match='Navigation'):await SupervisorAssessmentV12(core_settings,repo).intent_locked(c,'demo',intent,None)


async def test_mainline_questions_keep_task_owner_after_short_supervisor_ends(core_settings):
    import yaml
    from assistant_kernel.services.question_groups import QuestionGroupService
    repo,service,task,control,worker,context,decision=await captured(core_settings)
    folder=core_settings.data_root/'config';folder.mkdir(exist_ok=True)
    (folder/'channel-bindings.yaml').write_text(yaml.safe_dump({'bindings':[
        {'project_id':'demo','channel':'test','chat_id':'assessment','agent_key':'project:demo:manager'},
        {'project_id':'demo','channel':'test','chat_id':'assessment-supervisor','agent_key':'project:demo:supervisor'}]}))
    decision['questions']=[{'question_id':name,'question_text':'Specify '+name,'topic':name,'required':True,'options':[],
        'recommended_option_id':None,'recommendation_reason':'','source_refs':context['user_message_refs']} for name in ('scope','output')]
    settled=await service.settle(control=control,worker_instance_id=worker,decision=decision)
    assert (await repo.get_run(str(control['run_id'])))['status']=='succeeded'
    canonical=await repo.get_message(settled['question_group_ref']['id']);wrapper=canonical['content_json']
    assert wrapper['group']['owner_ref']['id']==str(task['run_id'])
    projections=[await repo.get_message(identity) for identity in wrapper['projection_ids']]
    questions=QuestionGroupService(core_settings,repo)
    first=projections[0]
    await questions.record_action(message_id=str(first['message_id']),project_id='demo',channel='test',chat_id=first['chat_id'],
        user_id='owner',external_event_id='partial-mainline',revision=0,page=0,action='explicit_text',value={'scope':'offline'},
        content_version=wrapper['group']['content_version'],original_text='scope: offline')
    newer_worker=str(uuid.uuid4());newer=await repo.claim_run('next-supervisor',newer_worker,90)
    await service.prepare_context(newer)
    decision2={**decision,'questions':[],'project_assessment':{'status':'facts_only','assessed_at':None,
        'evaluated_source_batch_id':None,'covered_task_versions':[],'covered_source_refs':[],'evidence_refs':[],
        'summary':'A required question remains unanswered','blockers':[]}}
    second_settlement=await service.settle(control=newer,worker_instance_id=newer_worker,decision=decision2)
    assert second_settlement['phase_settlement']['waiting_for']=='required_answers'
    assert (await repo.get_run(str(newer['run_id'])))['status']=='succeeded'
    assert (await repo.get_run(str(task['run_id'])))['status']=='waiting_user'
    second=projections[1]
    await questions.record_action(message_id=str(second['message_id']),project_id='demo',channel='test',chat_id=second['chat_id'],
        user_id='owner',external_event_id='remaining-mainline',revision=1,page=0,action='explicit_text',value={'output':'report'},
        content_version=wrapper['group']['content_version'],original_text='output: report')
    canonical=await repo.get_message(str(canonical['message_id']))
    assert canonical['content_json']['group']['status']=='answered' and len(canonical['content_json']['group']['accepted_answers'])==2


async def test_confirmed_preference_survives_partial_refresh_and_keeps_source_time(core_settings):
    repo,service,task,control,worker,context,decision=await captured(core_settings)
    intent=empty_intent();intent['confirmed_preferences']=[{'statement':'offline reports','status':'active',
        'source_refs':context['user_message_refs'],'confirmed_at':(datetime.now(timezone.utc)+timedelta(days=30)).isoformat()}]
    helper=SupervisorAssessmentV12(core_settings,repo)
    async with repo.database.connection() as c:
        first=await helper.intent_locked(c,'demo',intent,None)
        source=await repo.get_message(context['user_message_refs'][0]['id'])
        assert first['confirmed_preferences'][0]['confirmed_at']==source['created_at'].isoformat()
        retained=await helper.intent_locked(c,'demo',empty_intent(),{'user_intent_context':first})
        assert retained['confirmed_preferences']==first['confirmed_preferences']
        changed=deepcopy(first);changed['confirmed_preferences'][0]['status']='withdrawn'
        with pytest.raises(PermissionError,match='new actual user source'):await helper.intent_locked(c,'demo',changed,{'user_intent_context':first})
