import asyncio
from copy import deepcopy
import uuid

import pytest
from psycopg.types.json import Jsonb

from assistant_kernel.repositories import CoreRepository
from assistant_kernel.services.document_runtime import resource_ref
from assistant_kernel.services.prompt_protocol import PromptProtocol
from assistant_kernel.services.published_agents import PublishedAgentService
from test_core4 import core_settings
from test_v12_foundations import source
from test_v12_packages import candidate


async def preparation(settings, *, project_policy=None):
    bundle,config,_=candidate(settings);repo=CoreRepository(settings);compiler=PromptProtocol(settings,repo)
    incoming=await repo.ingest_message(project_id='demo',agent_key=config.agent_key,channel='test',chat_id='prompt-ui',
        user_id='owner',external_event_id='user-task',external_message_id=None,content={'text':'Analyse the example offline'})
    scope={'project_ids':['demo'],'existing_task_ids':[],'new_tasks':True,'next_turn_binding_confirmation':True}
    published=PublishedAgentService(settings)
    ref=published.prepare('demo',configurations={config.agent_key+'/requirements':config},
        template_ref=bundle.ref('profiles.json'),adoption_scope=scope,source_refs=[source()],project_policy=project_policy)
    published.adopt('demo',package_ref=ref,expected_previous_ref=None,approval_refs=[source()])
    async with repo.database.connection() as c:
        task=await (await c.execute("UPDATE assistant.runs SET status='waiting_supervisor',request_json=request_json || %s WHERE run_id=%s RETURNING *",
            (Jsonb({'state_revision':0,'supervisor_context_epoch':0,'current_stage':'requirements'}),incoming['run']['run_id']))).fetchone()
        head=await compiler.input_head_locked(c,task)
        cap=compiler.write_blob('demo',str(task['run_id']),'capability',{'actor':config.agent_key,'status':'fixture','model_execution':'not_verified'})
        authority=compiler.write_blob('demo',str(task['run_id']),'file-authority',{'roots':[],'status':'fixture'})
        original=resource_ref(kind='message',identity=str(incoming['message']['message_id']),
            uri='db://assistant/messages/'+str(incoming['message']['message_id']),purpose='actual original instruction')
        owner=resource_ref(kind='receipt',identity=str(task['run_id']),uri='db://assistant/runs/'+str(task['run_id']),purpose='Task owner')
        draft={'title':'Offline analysis','objective':'Analyse the example offline','proposed_work_mode':'requirements',
            'background':[],'work_items':[{'step_id':'analyse','description':'Analyse the example offline','depends_on':[]}],
            'constraints':[{'text':'offline','source_refs':[original]}],
            'deliverables':[{'description':'Analysis result','artifact_kind':'report'}],
            'acceptance':[{'criterion_id':'offline','criterion':'No external action','verification':'Inspect receipts','requires_user_feedback':False}],
            'authorization_boundary':{'permitted_work':['Offline analysis'],'excluded_work':['External actions'],
                'required_approval_subjects':[]},'source_refs':[original]}
        proposal={'owner_ref':owner,'user_message_refs':[original],'snapshot_ref':head,'basis_state_revision':0,
            'basis_context_epoch':0,'file_authority_ref':authority,'capability_profile_ref':cap,'draft':draft,
            'blocking_question_ids':[],'child_result_refs':[],'clarity':'ready_for_confirmation','scope_change':'none',
            'confirmation_source_candidate_refs':[]}
        proposal_ref=compiler.documents.write_scoped_revision(project_id='demo',scope='task',object_id='prompt',task_id=str(task['run_id']),
            run_id=str(task['run_id']),document_kind='prompt-proposal',document_schema='prompt-proposal.v1',payload=proposal,status='proposed',
            actor_type='kernel',actor_id='kernel',reason='Compile model proposal without dispatch',source_refs=[original])
        prepared=await compiler.prepare_locked(c,task=task,proposal_ref=proposal_ref,runtime_package_ref=ref,
            input_head_ref=head,capability_ref=cap)
        await c.commit()
    task=await repo.get_run(str(task['run_id']));display=await repo.get_message(prepared['display_ref']['id'])
    return repo,compiler,task,display,draft


async def confirm(compiler,display,event='confirm',*,user='owner',action='confirm'):
    return await compiler.record_confirmation(display_message_id=str(display['message_id']),project_id='demo',channel='test',
        chat_id='prompt-ui',user_id=user,external_event_id=event,action=action,
        expected_package_sha256=display['content_json']['package_ref']['sha256'])


async def test_exact_displayed_package_is_confirmed_once_and_is_the_dispatched_draft(core_settings):
    repo,compiler,task,display,draft=await preparation(core_settings)
    assert task['status']=='waiting_user' and 'confirmed_turn_package_ref' not in task['request_json']
    with pytest.raises(PermissionError,match='delivered'): await confirm(compiler,display,event='before-delivery')
    async with repo.database.connection() as c:
        await c.execute("UPDATE assistant.messages SET delivery_status='sent',external_message_id='fixture-card' WHERE message_id=%s",(display['message_id'],));await c.commit()
    results=await asyncio.gather(confirm(compiler,display,event='left'),confirm(compiler,display,event='right'),return_exceptions=True)
    assert sum(isinstance(x,dict) and x['status']=='accepted' for x in results)==1
    event='left' if isinstance(results[0],dict) else 'right'
    repeated=await confirm(compiler,display,event=event)
    assert repeated['status']=='duplicate'
    task=await repo.get_run(str(task['run_id']));assert task['status']=='queued'
    assert task['request_json']['confirmed_turn_package_ref']==display['content_json']['package_ref']
    async with repo.database.connection() as c:
        package,actual=await compiler.validate_dispatch_locked(c,task)
    assert actual==draft and package['prompt_ref']==display['content_json']['prompt_ref']
    async with repo.database.connection() as c:
        advanced={**task,'request_json':{**task['request_json'],'task_turn_count':1}}
        with pytest.raises(PermissionError,match='previous confirmation'):
            await compiler.validate_dispatch_locked(c,advanced)
        with pytest.raises(PermissionError,match='Stopped'):
            await compiler.validate_dispatch_locked(c,{**task,'cancel_requested_at':'actual stop marker'})


async def test_new_semantic_input_invalidates_old_prompt_without_consuming_it(core_settings):
    repo,compiler,task,display,draft=await preparation(core_settings)
    extra=str(uuid.uuid4())
    async with repo.database.connection() as c:
        await c.execute("UPDATE assistant.messages SET delivery_status='sent' WHERE message_id=%s",(display['message_id'],))
        await c.execute("INSERT INTO assistant.messages(message_id,project_id,run_id,channel,chat_id,user_id,direction,message_type,content_json,delivery_status) VALUES (%s,'demo',%s,'test','prompt-ui','owner','inbound','text',%s,'received')",
            (extra,task['run_id'],Jsonb({'text':'Also add a new output','context_visibility':'semantic'})));await c.commit()
    with pytest.raises(PermissionError,match='changed'):await confirm(compiler,display,event='stale-confirm')
    unchanged=await repo.get_run(str(task['run_id']))
    assert unchanged['status']=='waiting_user' and 'confirmed_turn_package_ref' not in unchanged['request_json']
    async with repo.database.connection() as c:
        event=await (await c.execute("SELECT content_json FROM assistant.messages WHERE external_event_id='stale-confirm'")).fetchone()
    assert event['content_json']['acceptance']['status']=='rejected'


async def test_wrong_owner_is_preserved_and_cancellation_does_not_dispatch(core_settings):
    repo,compiler,task,display,draft=await preparation(core_settings)
    async with repo.database.connection() as c:
        await c.execute("UPDATE assistant.messages SET delivery_status='sent' WHERE message_id=%s",(display['message_id'],));await c.commit()
    with pytest.raises(PermissionError):await confirm(compiler,display,event='foreign-confirm',user='another')
    receipt=await confirm(compiler,display,event='cancel-package',action='cancel')
    assert receipt['confirmation_ref'] is None
    assert (await confirm(compiler,display,event='cancel-package',action='cancel'))['status']=='duplicate'
    assert (await repo.get_run(str(task['run_id'])))['status']=='waiting_supervisor'


async def test_phase_gates_reject_missing_approval_and_unsettled_results(core_settings):
    repo,compiler,task,display,draft=await preparation(core_settings)
    async with repo.database.connection() as c:
        with pytest.raises(PermissionError,match='approval'):await compiler.gates_locked(c,task,'design')
        reconciling={**task,'status':'reconciling'}
        with pytest.raises(PermissionError,match='settled'):await compiler.gates_locked(c,reconciling,'requirements')


async def test_invocation_rechecks_model_tools_and_shared_authority_without_task_scratch_false_conflicts(core_settings):
    _,compiler,task,_,_=await preparation(core_settings)
    expected=compiler.write_blob('demo',str(task['run_id']),'authority-preflight',{'roots':[
        {'key':'project','path':'/test/project','access':'write'}, {'key':'run_work','path':'/test/supervisor/work'}]})
    actual=compiler.write_blob('demo',str(task['run_id']),'authority-invocation',{'roots':[
        {'key':'project','path':'/test/project','access':'write'}, {'key':'run_work','path':'/test/pm/work'}, {'key':'candidate','path':'/test/candidate'}]})
    profile={'provider':'openai','model':'fixed','context_policy':'catalog'}
    tools=[{'server_id':'kernel_scoped','transport':'stdio','loaded_tools':['read'], 'missing_tools':[],
        'status':'ready','tool_schema_sha256':{'read':'0'*64}}]
    capability={'model_profile':profile,'mcp_discovery':tools,'file_authority_ref':expected}
    compiler.recheck_capabilities('demo',capability,model_profile=profile,mcp_discovery=tools,authority_ref=actual)
    with pytest.raises(PermissionError,match='model'):compiler.recheck_capabilities('demo',capability,
        model_profile={**profile,'model':'new'},mcp_discovery=tools,authority_ref=actual)
    changed=deepcopy(tools);changed[0]['tool_schema_sha256']['read']='1'*64
    with pytest.raises(PermissionError,match='MCP'):compiler.recheck_capabilities('demo',capability,
        model_profile=profile,mcp_discovery=changed,authority_ref=actual)
    revoked=compiler.write_blob('demo',str(task['run_id']),'authority-revoked',{'roots':[]})
    with pytest.raises(PermissionError,match='agreement'):compiler.recheck_capabilities('demo',capability,
        model_profile=profile,mcp_discovery=tools,authority_ref=revoked)
    expanded=compiler.write_blob('demo',str(task['run_id']),'authority-expanded',{'roots':[
        {'key':'project','path':'/test/project','access':'write','description':'Updated navigation label'},
        {'key':'new-agreement','path':'/test/new-agreement','access':'write'}]})
    inherited={**capability,'config_bindings':[{'path':'/agent_defaults/model','mode':'inherit','override_value':None}]}
    compiler.recheck_capabilities('demo',inherited,model_profile={**profile,'model':'new-supported-model'},
        mcp_discovery=tools,authority_ref=expanded)
    explicit={**capability,'config_bindings':[{'path':'/agent_defaults/model','mode':'override','override_value':'fixed'}]}
    with pytest.raises(PermissionError,match='model'):compiler.recheck_capabilities('demo',explicit,
        model_profile={**profile,'model':'new-supported-model'},mcp_discovery=tools,authority_ref=expanded)


@pytest.mark.parametrize('expanded',[False,True])
async def test_complete_original_json_instruction_binds_only_identical_specific_prompt(core_settings,expanded):
    repo,compiler,task,display,draft=await preparation(core_settings)
    old=compiler.read_blob('demo',display['content_json']['package_ref'])
    async with repo.database.connection() as c:
        await c.execute('UPDATE assistant.messages SET content_json=content_json || %s WHERE message_id=%s',
            (Jsonb({'execution_instruction':{'action':'execute','draft':draft}}),task['trigger_message_id']))
        task=await (await c.execute('SELECT * FROM assistant.runs WHERE run_id=%s FOR UPDATE',(task['run_id'],))).fetchone()
        head=await compiler.input_head_locked(c,task)
        payload=compiler.documents.read('demo',old['prompt_ref'])['payload'];payload['snapshot_ref']=head
        if expanded:payload['draft']['deliverables'].append({'description':'Unrequested extra output','artifact_kind':'report'})
        proposal=compiler.documents.write_scoped_revision(project_id='demo',scope='task',object_id='prompt',task_id=str(task['run_id']),
            run_id=str(task['run_id']),document_kind='prompt-proposal',document_schema='prompt-proposal.v1',payload=payload,
            status='proposed',actor_type='kernel',actor_id='kernel',reason='isolated explicit original instruction',source_refs=payload['user_message_refs'])
        preparation2=await compiler.prepare_locked(c,task=task,proposal_ref=proposal,runtime_package_ref=old['runtime_package_ref'],
            input_head_ref=head,capability_ref=old['capability_ref']);await c.commit()
    current=await repo.get_run(str(task['run_id']))
    if expanded:
        assert current['status']=='waiting_user' and preparation2['confirmation_state']=='awaiting_confirmation'
        assert not current['request_json'].get('prompt_confirmation_ref')
    else:
        assert current['status']=='queued' and preparation2['confirmation_state']=='confirmed'
        assert current['request_json']['prompt_confirmation_message_id']==str(task['trigger_message_id'])
        original=await repo.get_message(str(task['trigger_message_id']))
        assert original['content_json']['direct_instruction_binding']['prompt_ref']==proposal
        async with repo.database.connection() as c:
            _,actual=await compiler.validate_dispatch_locked(c,current)
        assert actual==draft


async def test_literal_natural_instruction_binds_real_original_source_without_extra_confirmation(core_settings):
    repo,compiler,task,display,draft=await preparation(core_settings)
    old=compiler.read_blob('demo',display['content_json']['package_ref'])
    text='请离线生成分析报告，验收：覆盖三点。'
    original=compiler.documents.read('demo',old['prompt_ref'])['payload']['user_message_refs']
    draft.update(objective=text,background=[],work_items=[{'step_id':'literal','description':text,'depends_on':[]}],
        constraints=[{'text':'离线','source_refs':original}],deliverables=[{'description':'分析报告','artifact_kind':'report'}],
        acceptance=[{'criterion_id':'coverage','criterion':'覆盖三点','verification':'检查报告','requires_user_feedback':False}],
        authorization_boundary={'permitted_work':[text],'excluded_work':['外部操作'],'required_approval_subjects':[]})
    async with repo.database.connection() as c:
        await c.execute('UPDATE assistant.messages SET content_json=content_json || %s WHERE message_id=%s',(Jsonb({'text':text}),task['trigger_message_id']))
        task=await (await c.execute('SELECT * FROM assistant.runs WHERE run_id=%s FOR UPDATE',(task['run_id'],))).fetchone()
        head=await compiler.input_head_locked(c,task)
        payload=compiler.documents.read('demo',old['prompt_ref'])['payload'];payload.update(draft=draft,snapshot_ref=head)
        ref=compiler.documents.write_scoped_revision(project_id='demo',scope='task',object_id='prompt',task_id=str(task['run_id']),
            run_id=str(task['run_id']),document_kind='prompt-proposal',document_schema='prompt-proposal.v1',payload=payload,
            status='proposed',actor_type='kernel',actor_id='kernel',reason='literal original instruction case',source_refs=original)
        prepared=await compiler.prepare_locked(c,task=task,proposal_ref=ref,runtime_package_ref=old['runtime_package_ref'],input_head_ref=head,capability_ref=old['capability_ref'])
        await c.commit()
    assert prepared['confirmation_state']=='confirmed'
    current=await repo.get_run(str(task['run_id']))
    assert current['status']=='queued' and current['request_json']['prompt_confirmation_message_id']==str(task['trigger_message_id'])
    async with repo.database.connection() as c:
        _,actual=await compiler.validate_dispatch_locked(c,current)
    assert actual==draft
