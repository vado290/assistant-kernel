"""Isolated DB/controller tests; supplied model data is not live business proof."""
from copy import deepcopy
import hashlib
import json
import uuid

import pytest
from psycopg.types.json import Jsonb

from assistant_kernel.repositories import CoreRepository
from assistant_kernel.services.supervisor_v12 import SupervisorV12,empty_intent
from assistant_kernel.services.task_queue import ProjectTaskQueue
from assistant_kernel.services.document_runtime import resource_ref
from test_core4 import core_settings
from test_v12_p3_admission import complete_package


@pytest.mark.parametrize('case',['wrong_hash','parent','symlink'])
async def test_prompt_artifact_binding_keeps_scope_and_existing_pins(core_settings,case):
    repo,service,_,control,worker,context=await setup(core_settings,False)
    escaped=case!='wrong_hash'
    identity=str(control['run_id']);relative='runs/'+identity+'/work/'+('../outside.json' if case=='parent' else 'pinned.json')
    path=service.documents.runtime.describe('demo').vault_root/relative
    path.parent.mkdir(parents=True,exist_ok=True)
    if case=='symlink':
        outside=path.parent.parent/'outside.json';outside.write_text(json.dumps(prompt_result(context)));path.symlink_to(outside)
    else:path.write_text(json.dumps(prompt_result(context)))
    ref=resource_ref(kind='source',identity='actual-draft',uri='vault://demo/'+relative,
        schema_version='prompt-assembly-result.v1',sha256=None if escaped else '0'*64)
    output=decision();output['public_result']['artifact_refs']=[ref]
    with pytest.raises((PermissionError,ValueError) if escaped else ValueError,match='escapes|relative path' if escaped else 'hash differs'):
        await service.receive_parent_result(control=control,worker_instance_id=worker,decision=output)


async def setup(settings,active):
    complete_package(settings);repo=CoreRepository(settings);main=None
    if active:
        created=await repo.ingest_message(project_id='demo',agent_key='project:demo:manager',channel='test',chat_id='main-A',
            user_id='owner',external_event_id='main-A',external_message_id=None,content={'text':'Existing task A'})
        async with repo.database.connection() as c:
            main=await (await c.execute("UPDATE assistant.runs SET status='waiting_user',request_json=request_json || %s WHERE run_id=%s RETURNING *",
                (Jsonb({'approval_kind':'requirements'}),created['run']['run_id']))).fetchone();await c.commit()
    source=(await repo.record_channel_command(project_id='demo',agent_key='project:demo:supervisor',channel='test',chat_id='consult-B',
        user_id='owner',external_event_id='consult-B',external_message_id=None,content={'text':'另做离线分析报告 B','context_visibility':'semantic'}))['message']
    worker=str(uuid.uuid4());claim=str(uuid.uuid4());identity=str(uuid.uuid4())
    request={'interaction_kind':'supervisor_consultation','mainline_context_only':True,'task_id':str(main['run_id']) if main else None,
        'source_message_id':str(source['message_id']),'consultation_context_epoch':0}
    async with repo.database.connection() as c:
        control=await (await c.execute("""INSERT INTO assistant.runs(run_id,project_id,agent_key,trigger_message_id,
            run_type,status,conversation_key,request_json,executor_json,worker_instance_id,claim_token,lease_until)
            VALUES (%s,'demo','project:demo:supervisor',%s,'chat','running','isolated-consultation',%s,'{}',%s,%s,now()+interval '90 seconds') RETURNING *""",
            (identity,source['message_id'],Jsonb(request),worker,claim))).fetchone();await c.commit()
    service=SupervisorV12(settings,repo);context=await service.prepare_context(control)
    authority=service.prompts.write_blob('demo',identity,'parent-scope-fixture',{'roots':[]})
    assembly=service.prompts.write_blob('demo',identity,'parent-assembly-fixture',{'workspace_scope_ref':authority})
    async with repo.database.connection() as c:
        await c.execute('UPDATE assistant.runs SET executor_json=executor_json || %s WHERE run_id=%s',(Jsonb({'latest_assembly_ref':assembly}),identity));await c.commit()
    return repo,service,main,control,worker,context


def prompt_result(context):
    refs=context['user_message_refs'];goal='另做离线分析报告 B'
    draft={'title':'B','objective':goal,'proposed_work_mode':'requirements','background':[],
        'work_items':[{'step_id':'analyse','description':goal,'depends_on':[]}],'constraints':[],
        'deliverables':[{'description':'分析报告','artifact_kind':'report'}],
        'acceptance':[{'criterion_id':'report','criterion':'报告有结论','verification':'检查报告','requires_user_feedback':False}],
        'authorization_boundary':{'permitted_work':[goal],'excluded_work':['外部动作'],'required_approval_subjects':[]},'source_refs':refs}
    return {'schema_version':'prompt-assembly-result.v1','outcome':'ready_for_review','understood_goal':goal,
        'source_alignment':[{'text':goal,'classification':'user_original','original_quote':goal,'source_refs':refs}],
        'prompt_draft':draft,'suggestions':[],'questions':[],'assumptions':[],'context_gaps':[],'source_refs':refs,
        'reported_checks':{'intent_preserved':True,'latest_sources_used':True,'no_unconfirmed_scope_added':True,'no_authority_expanded':True}}


def decision(proposal=None):
    return {'public_result':{'fixture':'model output'},'questions':[],'summary':'本轮只读咨询结果','prompt_proposal_ref':proposal,
        'insufficient_evidence':False,'user_intent_context':empty_intent(),
        'project_assessment':{'status':'facts_only','assessed_at':None,'evaluated_source_batch_id':None,
            'covered_task_versions':[],'covered_source_refs':[],'evidence_refs':[],'summary':'Partial current facts','blockers':[]}}


async def test_quote_typography_keeps_raw_result_and_binds_real_source(core_settings):
    repo,service,_,control,worker,context=await setup(core_settings,False)
    result=prompt_result(context)
    result['source_alignment'][0]['original_quote']='另做离线\n分析报告B。'
    submitted=deepcopy(result)
    await service.accept_prompt_result(control_id=str(control['run_id']),worker_instance_id=worker,
        claim_token=str(control['claim_token']),result=result,new_task=True)
    assert result==submitted
    records=list(service.documents.runtime.describe('demo').vault_root.glob('runs/*/prompt-protocol/source-quote-bindings/*.json'))
    assert len(records)==1
    evidence=json.loads(records[0].read_text())
    assert evidence['bindings'][0]['submitted_quote']=='另做离线\n分析报告B。'
    assert evidence['bindings'][0]['actual_quote']=='另做离线分析报告 B'


async def test_consultation_has_own_input_and_still_delivers_when_mainline_advances(core_settings):
    repo,service,main,control,worker,context=await setup(core_settings,True)
    head=service.prompts.read_blob('demo',context['input_head_ref'])
    assert any(item['source']['text']=='另做离线分析报告 B' for item in head['inputs'])
    assert not any(item['source'].get('text')=='Existing task A' for item in head['inputs'])
    async with repo.database.connection() as c:
        await c.execute('UPDATE assistant.runs SET request_json=request_json || %s WHERE run_id=%s',(Jsonb({'state_revision':10}),main['run_id']));await c.commit()
    result=await service.settle(control=control,worker_instance_id=worker,decision=decision())
    assert not result['stale'] and result['mainline_facts_changed']
    assert result['assessment_commit']['status']=='stale' and not result['pm_dispatched']
    async with repo.database.connection() as c:
        reply=await (await c.execute("SELECT content_json FROM assistant.messages WHERE run_id=%s AND direction='outbound' ORDER BY created_at DESC LIMIT 1",(control['run_id'],))).fetchone()
    assert reply and '主线状态已更新' in reply['content_json']['text']
    assert (await repo.get_run(str(main['run_id'])))['status']=='waiting_user'


@pytest.mark.parametrize('active',[False,True])
async def test_new_work_waits_for_exact_package_and_preserves_existing_task(core_settings,monkeypatch,active):
    repo,service,main,control,worker,context=await setup(core_settings,active)
    before=deepcopy(main['request_json']) if main else None
    submitted=await service.accept_prompt_result(control_id=str(control['run_id']),worker_instance_id=worker,
        claim_token=str(control['claim_token']),result=prompt_result(context),new_task=True)
    new_id=service.documents.read('demo',submitted['proposal_ref'])['task_id']
    proposed=await repo.get_run(new_id)
    assert proposed['request_json']['task_activation']=='queued' and not proposed['request_json'].get('confirmed_turn_package_ref')
    async def probe(**kwargs):
        identity=kwargs['context']['task_id'];assert identity==new_id
        cap=service.prompts.write_blob('demo',identity,'capability-fixture',{'model_profile':{'model':'unit'},'business_verified':False})
        authority=service.prompts.write_blob('demo',identity,'authority-fixture',{'roots':[]})
        return cap,authority
    monkeypatch.setattr(service,'probe_pm_capabilities',probe)
    result=await service.settle(control=control,worker_instance_id=worker,decision=decision(submitted['proposal_ref']))
    display=await repo.get_message(result['prompt_preparation']['display_ref']['id'])
    assert (await repo.get_run(new_id))['status']=='waiting_user'
    if main:assert (await repo.get_run(str(main['run_id'])))['request_json']==before
    async with repo.database.connection() as c:
        await c.execute("UPDATE assistant.messages SET delivery_status='sent' WHERE message_id=%s",(display['message_id'],));await c.commit()
    await service.prompts.record_confirmation(display_message_id=str(display['message_id']),project_id='demo',channel='test',
        chat_id='consult-B',user_id='owner',external_event_id='actual-new-work-confirmation',action='confirm',
        expected_package_sha256=display['content_json']['package_ref']['sha256'])
    accepted=await repo.get_run(new_id)
    assert accepted['request_json']['task_scope_proposal']['confirmed']
    if main:
        assert accepted['status']=='waiting_supervisor' and accepted['request_json']['task_activation']=='queued'
        assert (await repo.get_run(str(main['run_id'])))['request_json']==before
        async with repo.database.connection() as c:
            await c.execute("UPDATE assistant.runs SET status='succeeded',request_json=request_json || %s WHERE run_id=%s",
                (Jsonb({'task_activation':'closed'}),main['run_id']));await c.commit()
        activated=await ProjectTaskQueue(core_settings,repo).activate_next('demo')
        assert str(activated['task']['run_id'])==new_id
    else:assert accepted['status']=='queued' and accepted['request_json']['task_activation']=='active'
    async with repo.database.connection() as c:
        current=await repo.get_run(new_id)
        _,actual_draft=await service.prompts.validate_dispatch_locked(c,current)
    assert actual_draft==prompt_result(context)['prompt_draft']

    # A subsequent confirmed Turn must retain the first work-scope receipt and
    # the active Task, rather than creating/queuing that Task a second time.
    scope_before=deepcopy(current['request_json']['task_scope_proposal'])
    follow_id=str(uuid.uuid4());follow_claim=str(uuid.uuid4())
    async with repo.database.connection() as c:
        current=await (await c.execute("UPDATE assistant.runs SET status='waiting_supervisor' WHERE run_id=%s RETURNING *",(new_id,))).fetchone()
        follow=await (await c.execute("""INSERT INTO assistant.runs(run_id,project_id,agent_key,trigger_message_id,
            run_type,status,conversation_key,request_json,executor_json,worker_instance_id,claim_token,lease_until)
            VALUES (%s,'demo','project:demo:supervisor',%s,'chat','running',%s,%s,'{}',%s,%s,now()+interval '90 seconds') RETURNING *""",
            (follow_id,current['trigger_message_id'],'follow-'+follow_id,Jsonb({'task_id':new_id,'source_message_id':str(current['trigger_message_id'])}),worker,follow_claim))).fetchone();await c.commit()
    follow_context=await service.prepare_context(follow)
    authority=service.prompts.write_blob('demo',follow_id,'follow-scope-fixture',{'roots':[]})
    assembly=service.prompts.write_blob('demo',follow_id,'follow-assembly-fixture',{'workspace_scope_ref':authority})
    async with repo.database.connection() as c:
        await c.execute('UPDATE assistant.runs SET executor_json=executor_json || %s WHERE run_id=%s',(Jsonb({'latest_assembly_ref':assembly}),follow_id));await c.commit()
    next_proposal=await service.accept_prompt_result(control_id=follow_id,worker_instance_id=worker,
        claim_token=follow_claim,result=prompt_result(follow_context))
    following=await service.settle(control=follow,worker_instance_id=worker,decision=decision(next_proposal['proposal_ref']))
    next_display=await repo.get_message(following['prompt_preparation']['display_ref']['id'])
    async with repo.database.connection() as c:
        await c.execute("UPDATE assistant.messages SET delivery_status='sent' WHERE message_id=%s",(next_display['message_id'],));await c.commit()
    await service.prompts.record_confirmation(display_message_id=str(next_display['message_id']),project_id='demo',channel='test',
        chat_id='consult-B',user_id='owner',external_event_id='next-turn-confirmation',action='confirm',
        expected_package_sha256=next_display['content_json']['package_ref']['sha256'])
    continued=await repo.get_run(new_id)
    assert continued['status']=='queued' and continued['request_json']['task_activation']=='active'
    assert continued['request_json']['task_scope_proposal']==scope_before
    assert str((await repo.current_project_task('demo'))['run_id'])==new_id
    assert '确认后创建并启动此新任务' not in next_display['content_json']['text']
    assert '确认后将此新任务加入待办' not in next_display['content_json']['text']


@pytest.mark.parametrize('active',[False,True])
@pytest.mark.parametrize('model_hash',[False,True])
async def test_parent_adopts_skill_artifact_without_a_submission_mcp(core_settings,monkeypatch,active,model_hash):
    repo,service,main,control,worker,context=await setup(core_settings,active)
    before=deepcopy(main['request_json']) if main else None;identity=str(control['run_id'])
    relative='runs/'+identity+'/work/accepted-prompt-result.json'
    path=service.documents.runtime.ensure('demo').vault_root/relative
    path.parent.mkdir(parents=True,exist_ok=True)
    encoded=json.dumps(prompt_result(context),ensure_ascii=False).encode();path.write_bytes(encoded)
    ref=resource_ref(kind='source',identity='parent-adopted-result',uri='vault://demo/'+relative,
        schema_version='prompt-assembly-result.v1',sha256=hashlib.sha256(encoded).hexdigest() if model_hash else None,
        purpose='Supervisor adopted Skill result in this invocation')
    output=decision();output['public_result']['artifact_refs']=[ref]
    output['proposed_actions']=[{'kind':'queue_task','goal':prompt_result(context)['prompt_draft']['objective'],
        'source_message_id':context['user_message_refs'][0]['id'],'task_id':None,'priority':'normal'}] if active else []
    async def probe(**kwargs):
        target=kwargs['context']['task_id']
        return (service.prompts.write_blob('demo',target,'test-capability',{'model_profile':{'model':'unit'}}),
            service.prompts.write_blob('demo',target,'test-authority',{'roots':[]}))
    monkeypatch.setattr(service,'probe_pm_capabilities',probe)
    result=await service.settle(control=control,worker_instance_id=worker,decision=output)
    assert result['prompt_preparation'] and not result['pm_dispatched']
    if main:assert (await repo.get_run(str(main['run_id'])))['request_json']==before
    display=await repo.get_message(result['prompt_preparation']['display_ref']['id'])
    assert display['content_json']['action_status']=='open'
    task=await repo.get_run(str(display['run_id']))
    assert task['status']=='waiting_user' and not task['request_json']['task_scope_proposal']['confirmed']
    assert not task['request_json'].get('confirmed_turn_package_ref')


async def test_existing_task_consultation_does_not_implicitly_create_or_retarget_work(core_settings):
    repo,service,main,control,worker,context=await setup(core_settings,True)
    before=deepcopy(main['request_json']);identity=str(control['run_id'])
    relative='runs/'+identity+'/work/prompt.json';path=service.documents.runtime.ensure('demo').vault_root/relative
    path.parent.mkdir(parents=True,exist_ok=True);encoded=json.dumps(prompt_result(context)).encode();path.write_bytes(encoded)
    ref=resource_ref(kind='source',identity='adopted',uri='vault://demo/'+relative,
        schema_version='prompt-assembly-result.v1',sha256=hashlib.sha256(encoded).hexdigest(),purpose='parent accepted')
    output=decision();output['public_result']['artifact_refs']=[ref];output['proposed_actions']=[]
    with pytest.raises(PermissionError,match='read-only consultation'):
        await service.settle(control=control,worker_instance_id=worker,decision=output)
    assert (await repo.get_run(str(main['run_id'])))['request_json']==before
    async with repo.database.connection() as c:
        rows=await (await c.execute("SELECT run_id FROM assistant.runs WHERE agent_key='project:demo:manager'")).fetchall()
    assert len(rows)==1 and str(rows[0]['run_id'])==str(main['run_id'])


async def test_parent_prompt_result_rejects_other_invocation_and_modified_bytes(core_settings):
    repo,service,main,control,worker,context=await setup(core_settings,False)
    identity=str(control['run_id']);relative='runs/'+identity+'/work/prompt.json'
    path=service.documents.runtime.ensure('demo').vault_root/relative;path.parent.mkdir(parents=True,exist_ok=True)
    encoded=json.dumps(prompt_result(context)).encode();path.write_bytes(encoded)
    ref=resource_ref(kind='source',identity='adopted',uri='vault://demo/'+relative,
        schema_version='prompt-assembly-result.v1',sha256=hashlib.sha256(encoded).hexdigest(),purpose='parent accepted')
    output=decision();output['public_result']['artifact_refs']=[ref]
    path.write_bytes(encoded+b' ')
    with pytest.raises(ValueError,match='hash differs'):
        await service.receive_parent_result(control=control,worker_instance_id=worker,decision=output)
    output['public_result']['artifact_refs']=[{**ref,'uri':'vault://demo/runs/'+str(uuid.uuid4())+'/work/prompt.json'}]
    with pytest.raises(PermissionError,match='this Supervisor invocation'):
        await service.receive_parent_result(control=control,worker_instance_id=worker,decision=output)
