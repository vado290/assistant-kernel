"""Finite Unit registration/control against actual isolated DB and immutable docs."""
import asyncio
from copy import deepcopy
from datetime import datetime,timezone
from pathlib import Path
import uuid
import pytest
from psycopg.types.json import Jsonb

from assistant_kernel.services.document_runtime import resource_ref
from assistant_kernel.services.runtime_units import RuntimeUnits
from assistant_kernel.services.project_python import ProjectPython
from assistant_kernel.worker.main import execute_python
from test_core4 import core_settings
from test_v12_p3_phase_control import baseline
from test_v12_p4_fixed_python import fixture


async def unit(settings,transform=None,runbook=False,handoff_authorized=False):
    repo,prompts,task,phases,write=await baseline(settings,runtime=handoff_authorized)
    async with repo.database.connection() as c:
        await c.execute((Path(__file__).parents[1]/'migrations/upgrade_core5_v8.sql').read_text())
        await c.execute((Path(__file__).parents[1]/'migrations/upgrade_core5_v9.sql').read_text());await c.commit()
    from assistant_kernel.services.system_config import STATE_KEY
    async with repo.database.connection() as c:
        known=await (await c.execute('SELECT 1 FROM assistant.kernel_meta WHERE key=%s',(STATE_KEY,))).fetchone()
    if not known:
        from test_v12_p4_admission import config
        await config(settings)
    root,_,manifest,source=fixture(settings,code="import json,os\njson.dump({'answer':42},open(os.environ['ASSISTANT_RESULT_PATH'],'w'))\n")
    manifest['code_bundle']['closure_manifest_ref']=source('program/closure.json',
        {'schema_version':'python-import-closure.v1','stdlib_modules':['json','os'],'dependency_modules':[]})
    original=resource_ref(kind='message',identity=str(task['trigger_message_id']),
        uri='db://assistant/messages/'+str(task['trigger_message_id']),purpose='synthetic test control authority, not user acceptance')
    manifest['purpose'].update(kind='formal',authorization_ref=original,turn_boundary_ref=None)
    manifest['ownership'].update(creating_task_ref=resource_ref(kind='receipt',identity=str(task['run_id']),
        uri='db://assistant/runs/'+str(task['run_id']),purpose='actual isolated source Task'),initial_authorization_ref=original)
    def projectdoc(kind,payload):
        return prompts.documents.write_scoped_revision(project_id='demo',scope='project',object_id='formal-test',
            document_kind=kind,document_schema=kind+'.v1',payload=payload,status='submitted',actor_type='kernel',actor_id='fixture',
            reason='Synthetic isolation fixture, not live business approval',source_refs=[original])
    manifest_ref=projectdoc('python-script-manifest',manifest)
    accepted=deepcopy(task['request_json']['accepted_documents'])
    definition={'runtime_unit_id':'formal-test','project_id':'demo','purpose':'actual isolated finite script',
        'execution_mode':'on_demand','implementation_manifest_ref':manifest_ref,'business_agents':[],
        'workflow':{'framework':'none','definition_ref':None,'graph_id':None,'state_owner':'business_program',
            'checkpoint_policy':'none','namespace':None,'interruption_policy':'not_needed','max_parallel_nodes':1},
        'project_agreement_ref':source('program/agreement.json',{'fixture':'own test root only'}),
        'design_ref':accepted['solution_design'],'source_refs':[original],'approved_execution_scope_ref':original,
        'resource_policy_ref':None,'write_set':[],'shared_resource_keys':[],'activation_required':True,'verification_refs':[],
        'lifecycle_kind':'finite','trigger_mode':'on_demand','config_bindings':[]}
    if transform:
        transform(manifest,definition,source)
        manifest_ref=projectdoc('python-script-manifest',manifest)
        definition['implementation_manifest_ref']=manifest_ref
    if runbook or handoff_authorized:
        design=prompts.documents.read('demo',accepted['solution_design'])['payload']
        if runbook:design['runbook_actions']=[{'action_id':'count','description':'Synthetic fixed finite operation','risk':'low',
                'script_manifest_ref':manifest_ref,'input_schema':manifest['io']['input_schema'],'expected_effects':manifest['expected_effects']}]
        if handoff_authorized:
            design['activation_plan']={'applicable':True,'reason_when_not_applicable':None,'steps':design['steps'],
                'verification':[{'check_id':'activate','criterion_ids':['A2'],'method':'real bounded test run','expected_result':'42','environment_ref':None}]}
            design['runtime_designs']=[{'runtime_unit_id':definition['runtime_unit_id'],'lifecycle_kind':'finite','trigger_mode':'on_demand',
                'execution_mode':'on_demand','entrypoint':'program/main.py','workflow_framework':'none','planned_agents':[],
                'role_asset_paths':[],'skill_asset_paths':[],'mcp_server_tools':[],'dependency_refs':[],
                'chosen_reason':'isolated finite validation','activation_and_recovery_plan':['enable reviewed version'],
                'test_obligations':['actual bounded run'],'unverified':[],'config_bindings':[]}]
        accepted['solution_design']=write('solution-design','solution-design.v1',design)
        definition['design_ref']=accepted['solution_design']
    ref=projectdoc('runtime-unit-definition',definition)
    impl=prompts.documents.read('demo',accepted['implementation_report'])['payload'];impl['actual_changes'].append(ref)
    accepted['implementation_report']=write('implementation-report','implementation-report.v1',impl)
    review=prompts.documents.read('demo',accepted['review_report'])['payload']
    review['fixed_input_refs']=[accepted[x] for x in ('task_contract','solution_design','implementation_report')]
    accepted['review_report']=write('review-report','review-report.v1',review)
    artifact=source('program/publication-fixture.json',{'fixture':'not actual publisher acceptance'})
    accepted['publication']=projectdoc('publication-receipt',{'project_id':'demo','creating_task_ref':manifest['ownership']['creating_task_ref'],
        'candidate_ref':artifact,'review_ref':accepted['review_report'],'authorization_ref':original,'actual_changes':[ref],
        'actual_version_refs':[ref],'published_at':datetime.now(timezone.utc).isoformat()})
    async with repo.database.connection() as c:
        await c.execute('UPDATE assistant.runs SET request_json=request_json || %s WHERE run_id=%s',
            (Jsonb({'accepted_documents':accepted}),task['run_id']));await c.commit()
        if handoff_authorized:
            await c.execute('UPDATE assistant.messages SET content_json=content_json || %s WHERE message_id=%s',
                (Jsonb({'decision':'approve','test_fixture':'synthetic specific design approval, not human acceptance'}),task['trigger_message_id']))
            await c.execute('UPDATE assistant.runs SET request_json=request_json || %s WHERE run_id=%s',
                (Jsonb({'current_stage':'operate','design_approval':{'message_id':str(task['trigger_message_id']),
                    'approved_document_sha256':accepted['solution_design']['sha256']}}),task['run_id']));await c.commit()
    service=RuntimeUnits(settings,repo)
    registration=await service.register('demo',ref,review_ref=accepted['review_report'],publication_ref=accepted['publication'])
    control={'runtime_unit_ref':ref,'action':'start','expected_control_revision':0,'idempotency_key':'start-formal',
        'authorization_source_refs':[original],'target_definition_ref':None,'stop_policy':'drain','cause':'manual',
        'tick_ref':None,'expected_instance_ref':None,'expires_at':None}
    submission={'manifest_ref':manifest_ref,'inputs':{'n':42},'idempotency_key':'formal-submit','execution_kind':'formal',
        'runtime_unit_ref':ref,'logical_execution_id':None,'resume_owner':None}
    return repo,service,registration,control,submission


async def test_registration_stopped_explicit_enable_then_real_finite_execution(core_settings):
    repo,units,registered,control,submission=await unit(core_settings)
    assert registered['desired_state']=='stopped' and registered['control_revision']==0
    service=ProjectPython(core_settings,repo)
    with pytest.raises(PermissionError,match='authorized/enabled'):await service.submit('demo',submission)
    with pytest.raises(PermissionError,match='authenticated'):await units.control_finite('demo',control)
    ref=await units.control_finite('demo',control,authenticated_operator=True)
    assert ref==await units.control_finite('demo',control,authenticated_operator=True)
    await service.submit('demo',submission)
    owner=str(uuid.uuid4());run=await repo.claim_run('formal-test',owner,90,queue_kind='runtime')
    await execute_python(run,core_settings,repo,owner,asyncio.Event())
    assert (await repo.get_run(str(run['run_id'])))['result_json']=={'answer':42}


async def test_pause_cancels_queued_and_stale_enable_cannot_reopen_it(core_settings):
    repo,units,registered,control,submission=await unit(core_settings)
    await units.control_finite('demo',control,authenticated_operator=True)
    admission=await ProjectPython(core_settings,repo).submit('demo',submission)
    await units.control_finite('demo',{**control,'action':'pause','expected_control_revision':1,'idempotency_key':'pause'},authenticated_operator=True)
    assert (await repo.get_run(admission['run_ref']['id']))['status']=='cancelled'
    assert await repo.claim_run('formal-test',str(uuid.uuid4()),90,queue_kind='runtime') is None
    with pytest.raises(PermissionError,match='stale'):
        await units.control_finite('demo',{**control,'idempotency_key':'late-start'},authenticated_operator=True)


async def test_expired_control_and_unreviewed_definition_update_are_refused(core_settings):
    repo,units,registered,control,submission=await unit(core_settings)
    with pytest.raises(PermissionError,match='expired'):
        await units.control_finite('demo',{**control,'expires_at':'2000-01-01T00:00:00Z'},authenticated_operator=True)
    definition=units.read_definition('demo',control['runtime_unit_ref']);definition['purpose']='unreviewed changed version'
    ref=units.documents.write_scoped_revision(project_id='demo',scope='project',object_id='formal-test',
        document_kind='runtime-unit-definition',document_schema='runtime-unit-definition.v1',payload=definition,status='submitted',
        actor_type='kernel',actor_id='fixture',reason='Negative unreviewed target fixture',source_refs=definition['source_refs'])
    with pytest.raises(PermissionError,match='omitted this exact'):
        await units.control_finite('demo',{**control,'action':'update','target_definition_ref':ref},authenticated_operator=True)


async def test_migration_failure_rolls_back_schema_identity(core_settings):
    from scripts.migrate_core5_v9 import migrate
    from assistant_kernel.repositories import CoreRepository
    repo=CoreRepository(core_settings)
    async with repo.database.connection() as c:
        await c.execute((Path(__file__).parents[1]/'migrations/upgrade_core5_v8.sql').read_text())
        await c.execute("UPDATE assistant.kernel_meta SET value='\"core5.v8\"'::jsonb WHERE key='schema_version'")
        await c.execute('CREATE TABLE assistant.project_runtime_units(project_id text)');await c.commit()
    with pytest.raises(ValueError,match='incompatible'):await migrate(core_settings,apply=True)
    async with repo.database.connection() as c:
        assert (await (await c.execute("SELECT value FROM assistant.kernel_meta WHERE key='schema_version'")).fetchone())['value']=='core5.v8'
