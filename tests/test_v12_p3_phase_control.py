"""Real isolated DB transitions; supplied review assertions are test data, not live acceptance."""
from copy import deepcopy
import uuid

import pytest
from psycopg.types.json import Jsonb

from assistant_kernel.services.document_runtime import resource_ref
from assistant_kernel.services.phase_control_v12 import PhaseControlV12
from test_core4 import core_settings
from test_v12_foundations import requirement
from test_v12_p3_prompt_protocol import preparation
from test_v12_p3_results import active_result


async def baseline(settings, *, runtime=False,descriptive_runtime=False):
    repo,prompts,task,_,_=await preparation(settings)
    identity=str(task['run_id']);source=resource_ref(kind='message',identity=str(task['trigger_message_id']),
        uri='db://assistant/messages/'+str(task['trigger_message_id']),purpose='actual isolated ingress')
    def write(kind,schema,payload):
        return prompts.documents.write_scoped_revision(project_id='demo',scope='task',object_id=kind,
            task_id=identity,run_id=identity,document_kind=kind,document_schema=schema,payload=payload,
            status='submitted',actor_type='pm',actor_id=task['agent_key'],reason='isolated phase gate fixture',source_refs=[source])
    contract=requirement();contract['requirements'][0]['source_refs']=[source]
    if descriptive_runtime:
        contract['runtime_requirements']=[{'runtime_unit_id':'inline-report','goal':'Describe finite inline counting',
            'lifecycle_kind':'finite','trigger_mode':'on_demand','execution_mode':'on_demand',
            'workflow_framework':'none','agent_required':False,'config_bindings':[],
            'input_expectations':['three lines'],'output_expectations':['inline report'],
            'activation_expectations':['No program activation in this delivery'],
            'acceptance_ids':['A1'],'source_refs':[source]}]
    if runtime:
        contract['delivery_obligations'].append('runtime')
        contract['requirements'][0]['acceptance_ids'].append('A2')
        contract['acceptance_criteria'].append({'criterion_id':'A2','requirement_ids':['R1'],
            'applicability':'runtime','method':'actual invocation','expected_result':'running result','required':True,'requires_user_feedback':False})
    req=write('task-contract','task-contract.v1',contract)
    design={'task_contract_ref':req,'task_contract_sha256':req['sha256'],
        'options':[{'option_id':'one','summary':'offline result','feasibility':'feasible','tradeoffs':[]}],
        'chosen_option_id':'one','chosen_reason':'bounded work','dependencies':[],
        'steps':[{'step_id':'build','requirement_ids':['R1'],'action':'write result','depends_on':[],'expected_outputs':['result']}],
        'test_plan':[{'check_id':'C1','criterion_ids':['A1'],'method':'count','expected_result':'3','environment_ref':None}],
        'activation_plan':{'applicable':False,'reason_when_not_applicable':'offline semantic fixture','steps':[],'verification':[]},
        'publish_targets':[],'recovery_plan':[],'release_policy':'manual','risk_class':'low','runbook_actions':[],
        'report_schedule_ref':None,'runtime_designs':[]}
    des=write('solution-design','solution-design.v1',design)
    artifact=prompts.write_blob('demo',identity,'actual-fixture-deliverable',{'lines':['one','two','three']})
    implementation={'task_contract_ref':req,'solution_design_ref':des,'candidate_ref':None,'actual_changes':[artifact],
        'self_tests':[],'effect_receipt_refs':[],'unverified':[],'checkpoint':active_result()['checkpoint'],'runtime_unit_handoffs':[]}
    impl=write('implementation-report','implementation-report.v1',implementation)
    check={'check_id':'C1','criterion_ids':['A1'],'result':'passed','actual_result':3,'evidence_refs':[artifact],'reason':None}
    review={'fixed_input_refs':[req,des,impl],'deliverable_refs':[artifact],'per_item_checks':[check],
        'issues':[],'conclusion':'pass','publication_recommendation':'not_applicable','improvement_candidate_refs':[],
        'remaining_obligations':['A2'] if runtime else [],'runtime_checks':[]}
    if runtime:review['runtime_checks']=[{'check_id':'C2','criterion_ids':['A2'],'result':'unknown',
        'actual_result':None,'evidence_refs':[],'reason':'actual activation still pending'}]
    rev=write('review-report','review-report.v1',review)
    accepted={'task_contract':req,'solution_design':des,'implementation_report':impl,'review_report':rev}
    request={**task['request_json'],'protocol_revision':'1.2','accepted_documents':accepted,'validation_gate_status':'pass',
        'validation_design_sha256':des['sha256'],'validation_requirements_sha256':req['sha256'],
        'validation_implementation_sha256':impl['sha256'],'publication_state':'not_applicable',
        'review_execution_ref':prompts.write_blob('demo',identity,'supplied-test-provenance',{'fixture':True})}
    task={**task,'request_json':request}
    phases=PhaseControlV12(settings,repo)
    request.update(phases.admit_review(task))
    async with repo.database.connection() as c:
        await c.execute('UPDATE assistant.runs SET request_json=%s WHERE run_id=%s',(Jsonb(request),identity));await c.commit()
    return repo,prompts,task,phases,write


async def test_validation_evidence_is_not_accepted_before_turn_transaction(core_settings):
    repo,prompts,task,phases,_=await baseline(core_settings)
    async with repo.database.connection() as c:head=await prompts.input_head_locked(c,task)
    result=await phases.validate_build(task=task,expected_input_head=head)
    assert result['stale'] is False and result['validation']['release_gate']=='pass'
    after=await repo.get_run(str(task['run_id']))
    assert 'validation_artifact' not in after['request_json']
    assert result['acceptance_patch']['validation_implementation_sha256']==task['request_json']['accepted_documents']['implementation_report']['sha256']
    async with repo.database.connection() as c:
        await c.execute("UPDATE assistant.runs SET request_json=request_json || %s WHERE run_id=%s",(Jsonb({'state_revision':10}),task['run_id']));await c.commit()
    assert (await phases.validate_build(task=task,expected_input_head=head))['stale']


async def test_plain_text_delivery_uses_actual_hash_without_requiring_json(core_settings):
    import hashlib
    repo,prompts,task,phases,write=await baseline(core_settings)
    relative='runs/'+str(task['run_id'])+'/work/report.md'
    path=prompts.documents.runtime.ensure('demo').vault_root/relative;path.parent.mkdir(parents=True,exist_ok=True)
    path.write_bytes('第一行：1\n第二行：1\n第三行：1\n合计：3\n'.encode())
    ref=resource_ref(kind='source',identity='plain-report',uri='vault://demo/'+relative,sha256=hashlib.sha256(path.read_bytes()).hexdigest(),purpose='actual plain text delivery')
    report=prompts.documents.read('demo',task['request_json']['accepted_documents']['implementation_report'])['payload']
    report['actual_changes']=[ref]
    task['request_json']['accepted_documents']['implementation_report']=write('implementation-report','implementation-report.v1',report)
    async with repo.database.connection() as c:head=await prompts.input_head_locked(c,task)
    assert (await phases.validate_build(task=task,expected_input_head=head))['validation']['release_gate']=='pass'
    path.write_bytes(b'changed')
    with pytest.raises(PermissionError):await phases.validate_build(task=task,expected_input_head=head)


async def test_inline_delivery_reuses_archived_output_without_inventing_file_changes(core_settings):
    repo,prompts,task,phases,write=await baseline(core_settings)
    report=prompts.documents.read('demo',task['request_json']['accepted_documents']['implementation_report'])['payload']
    report['actual_changes']=[]
    task['request_json']['accepted_documents']['implementation_report']=write('implementation-report','implementation-report.v1',report)
    async with repo.database.connection() as c:head=await prompts.input_head_locked(c,task)
    with pytest.raises(ValueError,match='deliverable'):
        await phases.validate_build(task=task,expected_input_head=head)
    result=active_result();result['summary']='甲：1；乙：1；丙：1。合计 3。'
    result['payload']['stage']='build';result['payload']['report_payload']=report
    ref=prompts.write_blob('demo',str(task['run_id']),'pm-result',result)
    task['request_json']['pending_pm_result_ref']=ref
    validated=await phases.validate_build(task=task,expected_input_head=head)
    receipt=prompts.read_blob('demo',{**validated['validation'],'kind':'source'})
    assert receipt['deliverable_refs']==[ref]
    assert receipt['scope']=='structure' and receipt['business_acceptance']=='pending_independent_review'
    assert report['actual_changes']==[] and validated['sealed_bundle'] is None
    # A different or empty response cannot stand in for a missing deliverable.
    result['summary']=''
    task['request_json']['pending_pm_result_ref']=prompts.write_blob('demo',str(task['run_id']),'pm-result',result)
    with pytest.raises(ValueError,match='deliverable'):
        await phases.validate_build(task=task,expected_input_head=head)


async def test_review_baseline_identity_ignores_only_reference_description(core_settings):
    _,prompts,task,phases,write=await baseline(core_settings)
    review=prompts.documents.read('demo',task['request_json']['accepted_documents']['review_report'])['payload']
    review['fixed_input_refs']=[{**ref,'purpose':'Reviewed current baseline'} for ref in review['fixed_input_refs']]
    task['request_json']['accepted_documents']['review_report']=write('review-report','review-report.v1',review)
    phases.review_checks(task)
    review['fixed_input_refs'][0]['sha256']='0'*64
    task['request_json']['accepted_documents']['review_report']=write('review-report','review-report.v1',review)
    with pytest.raises(PermissionError):phases.review_checks(task)


async def test_final_review_must_cover_every_required_current_baseline_item(core_settings):
    _,_,task,phases,write=await baseline(core_settings)
    refs,docs=phases.baseline(task)
    payload=deepcopy(docs['review_report']['payload']);payload['per_item_checks'][0]['criterion_ids']=[]
    task['request_json']['accepted_documents']['review_report']=write('review-report','review-report.v1',payload)
    with pytest.raises(ValueError,match='omitted'):phases.admit_review(task)
    task['request_json']['accepted_documents']['review_report']=refs['review_report']
    task['request_json']['validation_implementation_sha256']='0'*64
    with pytest.raises(PermissionError,match='unvalidated'):phases.admit_review(task)


async def test_extra_review_checks_cannot_replace_or_contradict_final_criteria(core_settings):
    _,_,task,phases,write=await baseline(core_settings)
    _,docs=phases.baseline(task);review=deepcopy(docs['review_report']['payload'])
    extra={**review['per_item_checks'][0],'check_id':'additional-observation','criterion_ids':['prompt-local-check']}
    review['per_item_checks'].append(extra)
    task['request_json']['accepted_documents']['review_report']=write('review-report','review-report.v1',review)
    assert phases.admit_review(task)['audit_complete']
    review['per_item_checks']=[extra]
    task['request_json']['accepted_documents']['review_report']=write('review-report','review-report.v1',review)
    with pytest.raises(ValueError,match='omitted'):phases.admit_review(task)
    review['per_item_checks']=[docs['review_report']['payload']['per_item_checks'][0],
        {**extra,'criterion_ids':['A1'],'result':'failed'}]
    task['request_json']['accepted_documents']['review_report']=write('review-report','review-report.v1',review)
    with pytest.raises(ValueError,match='conflicting'):phases.admit_review(task)


@pytest.mark.parametrize('runtime_required',[False,True])
async def test_runtime_inapplicability_never_waives_final_delivery_criteria(core_settings,runtime_required):
    _,_,task,phases,write=await baseline(core_settings,runtime=runtime_required)
    _,docs=phases.baseline(task);review=deepcopy(docs['review_report']['payload'])
    review['runtime_checks']=[{'check_id':'runtime-applicability','criterion_ids':['A2' if runtime_required else 'A1'],
        'result':'not_applicable','actual_result':'no runtime claimed','evidence_refs':[],'reason':'scope observation'}]
    task['request_json']['accepted_documents']['review_report']=write('review-report','review-report.v1',review)
    if runtime_required:
        with pytest.raises(ValueError,match='cannot be waived'):phases.admit_review(task)
    else:
        assert phases.admit_review(task)['audit_complete']
        review['per_item_checks'][0]['criterion_ids']=[]
        task['request_json']['accepted_documents']['review_report']=write('review-report','review-report.v1',review)
        with pytest.raises(ValueError,match='omitted'):phases.admit_review(task)


async def test_publication_never_waives_runtime_obligations(core_settings):
    _,_,task,phases,_=await baseline(core_settings,runtime=True)
    task['request_json']['publication_state']='published'
    with pytest.raises(PermissionError,match='A2'):phases.completion_checks(task)


async def test_pure_artifact_can_pass_without_invented_runtime(core_settings):
    _,_,pure,pure_phases,_=await baseline(core_settings)
    assert pure_phases.completion_checks(pure)[2]['A1']['result']=='passed'
    pure['request_json']['reviewed_requirements_sha256']='0'*64
    with pytest.raises(PermissionError,match='current baseline'):pure_phases.completion_checks(pure)


@pytest.mark.parametrize('descriptive_runtime',[False,True])
async def test_artifact_completion_closes_same_task_without_new_operational_baseline(core_settings,descriptive_runtime):
    repo,prompts,task,phases,_=await baseline(core_settings,descriptive_runtime=descriptive_runtime)
    from assistant_kernel.services.task_queue import ProjectTaskQueue
    queue=ProjectTaskQueue(core_settings,repo)
    existing=prompts.write_blob('demo',str(task['run_id']),'old-unrelated-baseline',{'fixture':'existing project operation'})
    source=await repo.get_message(str(task['trigger_message_id']))
    async with repo.database.connection() as c:
        await queue._lock(c,'demo')
        await queue._meta(c,'demo')
        await queue._patch_meta(c,'demo',{'active_task_id':str(task['run_id']),'operational_baseline_ref':existing})
        result=await phases.complete_locked(c,task=task,source=source)
        await c.commit()
    after=await repo.get_run(str(task['run_id']))
    assert result['task_completed'] and result['operational_baseline_ref'] is None
    assert after['status']=='succeeded' and after['request_json']['task_activation']=='closed'
    async with repo.database.connection() as c:metadata=await queue._meta(c,'demo')
    assert metadata['active_task_id'] is None and metadata['operational_baseline_ref']==existing


@pytest.mark.parametrize('signal',['runtime','long_term_effect','required_criterion','activation_plan'])
async def test_explicit_runtime_obligation_still_requires_actual_activation(core_settings,signal):
    _,_,task,phases,_=await baseline(core_settings)
    _,docs=phases.baseline(task)
    # Keep the same verified document identities and isolate the completion rule.
    if signal in {'runtime','long_term_effect'}:
        docs['task_contract']['payload']['delivery_obligations'].append(signal)
    elif signal=='required_criterion':
        docs['task_contract']['payload']['acceptance_criteria'][0]['applicability']='runtime'
    else:docs['solution_design']['payload']['activation_plan']['applicable']=True
    from unittest.mock import patch
    original=phases.baseline(task)[0]
    with patch.object(phases,'baseline',return_value=(original,docs)):
        with pytest.raises(PermissionError,match='activation'):
            phases.completion_checks(task)


async def test_research_review_can_close_without_design_build_publication_or_operation(core_settings):
    repo,prompts,task,phases,write=await baseline(core_settings)
    _,docs=phases.baseline(task);requirement=deepcopy(docs['task_contract']['payload'])
    requirement['delivery_obligations']=['research'];requirement['acceptance_criteria'][0]['applicability']='research'
    req=write('task-contract','task-contract.v1',requirement)
    review=deepcopy(docs['review_report']['payload']);review['fixed_input_refs']=[req]
    rev=write('review-report','review-report.v1',review)
    task['request_json']['accepted_documents']={'task_contract':req,'review_report':rev}
    task['request_json']['research_validation_artifact']=prompts.write_blob('demo',str(task['run_id']),'research-validation',
        {'task_contract_ref':req,'actual_deliverable_refs':review['deliverable_refs'],'scope':'structure'})
    task['request_json'].update(phases.admit_review(task))
    async with repo.database.connection() as c:
        source=await repo.get_message(str(task['trigger_message_id']))
        result=await phases.complete_locked(c,task=task,source=source);await c.commit()
    assert result['task_completed'] and result['operational_baseline_ref'] is None
    assert (await repo.get_run(str(task['run_id'])))['status']=='succeeded'


@pytest.mark.parametrize('reused_builder',[False,True])
async def test_independent_review_requires_kernel_recorded_cold_audit_session(core_settings,reused_builder):
    repo,prompts,task,phases,_=await baseline(core_settings)
    package=prompts.write_blob('demo',str(task['run_id']),'confirmed-audit-fixture',{'fixture':True})
    task['request_json']['confirmed_turn_package_ref']=package
    session=await repo.ensure_session('demo',task['agent_key'],'test','cold-audit')
    trace=prompts.write_blob('demo',str(task['run_id']),'native-trace-fixture',{'fixture':'native invocation records supplied by unit test'})
    if reused_builder:
        await repo.start_model_invocation(invocation_id='builder',project_id='demo',run_id=str(task['run_id']),
            session_id=str(session['session_id']),actor_role='project_agent',harness='unit',provider='unit',model='unit',
            reasoning_effort=None,auth_mode=None,metadata={'task_stage':'build','native_continuation_requested':False})
    await repo.start_model_invocation(invocation_id='audit',project_id='demo',run_id=str(task['run_id']),
        session_id=str(session['session_id']),actor_role='project_agent',harness='unit',provider='unit',model='unit',
        reasoning_effort=None,auth_mode=None,metadata={'task_stage':'audit','native_continuation_requested':False,
            'confirmed_package_sha256':package['sha256']})
    await repo.finish_model_invocation(invocation_id='audit',status='succeeded',native_session_id='unit-native-audit',
        trace_uri=trace['uri'],trace_sha256=trace['sha256'])
    if reused_builder:
        with pytest.raises(PermissionError,match='non-Audit'):await phases.review_execution(task,'audit')
    else:
        receipt=await phases.review_execution(task,'audit')
        assert prompts.read_blob('demo',receipt)['independent_from_implementation'] is True


@pytest.mark.parametrize('operation,stopped',[('create',False),('delete',False),('create',True)])
async def test_publisher_settlement_preserves_task_and_does_not_create_operate_task(core_settings,operation,stopped):
    repo,prompts,parent,phases,_=await baseline(core_settings,runtime=True)
    publish_id=str(uuid.uuid4());worker=str(uuid.uuid4());claim=str(uuid.uuid4())
    sealed=prompts.write_blob('demo',str(parent['run_id']),'sealed-fixture',{'files':[]})
    request={**parent['request_json'],'publish_run_id':publish_id,'publication_state':'queued','task_phase':'publishing',
        'sealed_candidate_bundle':{**sealed,'bundle_sha256':sealed['sha256']}}
    async with repo.database.connection() as c:
        await c.execute("UPDATE assistant.runs SET status=%s,request_json=%s,cancel_requested_at=CASE WHEN %s THEN now() ELSE NULL END WHERE run_id=%s",
            ('cancelled' if stopped else 'waiting_user',Jsonb(request),stopped,parent['run_id']))
        await c.execute("""INSERT INTO assistant.runs(run_id,project_id,agent_key,trigger_message_id,parent_run_id,
            run_type,stage,status,conversation_key,request_json,executor_json,worker_instance_id,claim_token,lease_until)
            VALUES (%s,'demo','project:demo:publisher',%s,%s,'publish','publish','running','test-publication',%s,'{}',%s,%s,now()+interval '90 seconds')""",
            (publish_id,parent['trigger_message_id'],parent['run_id'],Jsonb(request),worker,claim));await c.commit()
    result={'files':[{'operation':operation,'target_uri':'workspace://project/result.txt',
        'original_sha256':None,'candidate_sha256':sealed['sha256'] if operation=='create' else None}],
        'publication_uri':sealed['uri']}
    receipt=await repo.finish_publish_run(publish_id,worker,claim,result=result,artifact_refs=[])
    after=await repo.get_run(str(parent['run_id']))
    assert receipt['parent_status']==after['status']==('cancelled' if stopped else 'waiting_supervisor')
    assert after['request_json']['publication_state']=='published' and after['request_json']['accepted_documents']['publication']
    with pytest.raises(PermissionError,match='A2'):phases.completion_checks(after)
    async with repo.database.connection() as c:
        operations=await (await c.execute("SELECT count(*) AS n FROM assistant.runs WHERE project_id='demo' AND request_json->>'current_stage'='operate'")).fetchone()
    assert operations['n']==0
