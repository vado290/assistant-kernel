from copy import deepcopy
import json
import pytest

from assistant_kernel.services.agent_results import normalize_agent_result
from assistant_kernel.services.contracts import ContractError
from assistant_kernel.services.run_processor import parse_agent_envelope
from test_v12_foundations import requirement


def active_result():
    return {'schema_version':'agent-result.v1','protocol_revision':'1.2','outcome':'continue',
        'summary':'clarifying the requirement','questions':[],'approval_requests':[],
        'artifact_refs':[],'evidence_refs':[],'proposed_actions':[],'delegation_results':[],
        'checkpoint':{'completed':[],'pending':[],'constraints':[],'decisions':[],
            'artifact_refs':[],'external_effect_refs':[],'unresolved_question_ids':[],'next_action':'clarify'},
        'payload':{'stage':'requirements','report_ref':None,'report_payload':None,
            'task_completion_proposed':False,'missing_capabilities':[],'observation_refs':[],
            'monitor_policy_refs':[],'notification_suggestions':[],'improvement_candidates':[],
            'proposal_refs':[],'python_artifacts':[],'schedule_handoff':[],'observation_summary':None,
            'runtime_handoffs':[],'runtime_status_refs':[]}}


def test_fixed_active_decoder_rejects_protocol_downgrade_and_control_injection():
    result=active_result()
    assert normalize_agent_result(result,phase='requirements',expected_revision='1.2')['protocol_revision']=='1.2'
    missing=deepcopy(result);missing.pop('protocol_revision')
    with pytest.raises(ValueError):normalize_agent_result(missing,phase='requirements',expected_revision='1.2')
    with pytest.raises(ValueError):normalize_agent_result(result,phase='requirements')
    with pytest.raises(ValueError):parse_agent_envelope('plain reply',expected_revision='1.2')
    with pytest.raises(ValueError):parse_agent_envelope(json.dumps({'outcome':'completed'}),expected_revision='1.2')
    result['claim_token']='model cannot supply Kernel identity'
    with pytest.raises(ContractError):normalize_agent_result(result,phase='requirements',expected_revision='1.2')


def test_active_report_uses_effective_contract_and_kernel_phase():
    result=active_result();result['payload']['report_payload']=requirement()
    assert normalize_agent_result(result,phase='requirements',expected_revision='1.2')['payload']['report_payload']['runtime_requirements']==[]
    result['payload']['report_payload'].pop('runtime_requirements')
    with pytest.raises(ContractError):normalize_agent_result(result,phase='requirements',expected_revision='1.2')
    with pytest.raises(ValueError):normalize_agent_result(active_result(),phase='build',expected_revision='1.2')
    # The old reader still accepts its own frozen protocol, without model choice.
    assert parse_agent_envelope('historical ordinary response')['summary']=='historical ordinary response'


def test_kernel_binds_missing_review_inputs_but_rejects_explicit_baseline_conflicts():
    from assistant_kernel.services.pm_turns_v12 import bind_review_inputs
    from assistant_kernel.services.document_runtime import resource_ref
    ref=resource_ref(kind='document',identity='implementation-report:task',uri='vault://demo/report.json',
        schema_version='implementation-report.v1',revision_id='r1',sha256='a'*64,purpose='actual fixed input')
    original={'fixed_input_refs':[],'conclusion':'pass','per_item_checks':[{'result':'passed'}]}
    result=bind_review_inputs(original,{'implementation_report':ref})
    assert result['fixed_input_refs']==[ref] and original['fixed_input_refs']==[]
    assert result['conclusion']==original['conclusion'] and result['per_item_checks']==original['per_item_checks']
    assert bind_review_inputs(result,{'implementation_report':ref})==result
    result['fixed_input_refs'][0]['sha256']='b'*64
    with pytest.raises(PermissionError,match='different implementation_report'):
        bind_review_inputs(result,{'implementation_report':ref})


def supervisor_result():
    result=active_result();result['outcome']='completed'
    result['payload']={'stage':'supervisor','decision':'consultation_reply','reason':[],
        'target_work_mode':None,'selected_skills':[],'prompt_proposal_ref':None,
        'project_assessment':{'status':'facts_only','assessed_at':None,'evaluated_source_batch_id':None,
            'covered_task_versions':[],'covered_source_refs':[],'evidence_refs':[],'summary':'Observed facts only','blockers':[]},
        'user_intent_context':{'scope':'project_only','explicit_goals':[],'confirmed_preferences':[],
            'current_concerns':[],'hypotheses':[],'pending_confirmations':[]},
        'missing_evidence':[],'insufficient_evidence':False,'delegations':[],
        'session_action':'keep','session_boundary_reason':'none','session_request':None,
        'runtime_assessment_refs':[],'directory_assessment_refs':[],'passive_iteration_mode':'notify_then_confirm'}
    return result


def test_supervisor_uses_the_configured_active_payload_without_legacy_auto_dispatch_fields():
    from assistant_kernel.services.goal_supervisor import GoalSupervisorService
    result=supervisor_result()
    actual=GoalSupervisorService.parse_decision_v2(json.dumps(result),expected_revision='1.2')
    assert actual['protocol_revision']=='1.2' and 'next_prompt' not in actual
    altered=deepcopy(result);altered['payload']['next_prompt']={'assembled_prompt':'cannot auto dispatch'}
    with pytest.raises(ContractError):GoalSupervisorService.parse_decision_v2(json.dumps(altered),expected_revision='1.2')
    old=deepcopy(result);old.pop('protocol_revision')
    with pytest.raises(ValueError):GoalSupervisorService.parse_decision_v2(json.dumps(old),expected_revision='1.2')


def test_producer_schema_exposes_complete_envelope_and_active_supervisor_fields():
    from assistant_kernel.services.contracts import contracts
    from jsonschema import Draft202012Validator
    schema=contracts().agent_result_schema('supervisor')
    validator=Draft202012Validator(schema)
    result=supervisor_result()
    validator.validate(result)
    result.pop('summary')
    assert any(error.validator=='required' for error in validator.iter_errors(result))
    result=supervisor_result();result['payload'].pop('session_request')
    assert any(error.validator=='required' for error in validator.iter_errors(result))
    with pytest.raises(ContractError,match='missing fields: session_request'):
        contracts().validate('V12_Active_AgentResult',result)
    assert 'V12_Contract' not in schema['$defs']
    result=supervisor_result();result['approval_requests']=[{'request':'confirm next phase'}]
    assert any(error.validator=='maxItems' for error in validator.iter_errors(result))


def test_pm_producer_schema_binds_phase_and_keeps_active_feedback_requirement():
    from assistant_kernel.services.contracts import contracts
    from jsonschema import Draft202012Validator
    schema=contracts().agent_result_schema('requirements');validator=Draft202012Validator(schema)
    result=active_result();result['payload']['report_payload']=requirement()
    validator.validate(result)
    result['payload']['report_payload']['acceptance_criteria'][0].pop('requires_user_feedback')
    assert list(validator.iter_errors(result))
    result=active_result();result['payload']['stage']='build'
    assert any(error.validator=='const' for error in validator.iter_errors(result))
    assert 'V12_Active_Design' not in schema['$defs']


async def test_configured_supervisor_correction_reports_exact_missing_fields(tmp_path):
    from types import SimpleNamespace
    from test_supervisor_tool_evidence import settings_for,pm_evidence_snapshot
    from assistant_kernel.services.goal_supervisor import GoalSupervisorService
    service=GoalSupervisorService(settings_for(tmp_path),SimpleNamespace())
    snapshot=pm_evidence_snapshot();config=service.configuration('demo')
    config['result_protocol_revision']='1.2';snapshot['supervisor_configuration']=config
    snapshot['current_facts']={'marker':'business-material-must-not-be-replayed-in-format-repair'}
    result=supervisor_result();invalid=deepcopy(result);invalid.pop('summary')
    replies=[invalid,result];prompts=[]
    async def call(**kwargs):
        prompts.append(kwargs['prompt'])
        return {'text':json.dumps(replies.pop(0)),'provider':'test','model':'test','request_id':'req-test',
            'usage':{},'trace_uri':'vault://demo/test-trace.jsonl','trace_sha256':'a'*64}
    service._codex_ephemeral=call
    settled=await service.run_consultation(project_id='demo',task_id='task-test',snapshot=snapshot,
        intent_panel=None,artifact_key='active-correction')
    assert settled['protocol_revision']=='1.2' and len(prompts)==2
    assert 'missing fields: summary' in prompts[1]
    assert 'Do not execute business work' in prompts[1]
    assert snapshot['current_facts']['marker'] in prompts[0]
    assert snapshot['current_facts']['marker'] not in prompts[1]
    assert len(prompts[1]) < len(prompts[0])
    directory=service._task_supervisor_dir('demo','task-test')
    assert [path.name for path in directory.glob('*-active-correction.json')]==['handoff-active-correction.json']
