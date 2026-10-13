"""Public API, published phase capability and exact schema integration."""
import httpx
from fastapi import FastAPI
import pytest
from test_core4 import core_settings
from test_v12_scheduler import fixture
from assistant_kernel.api.schedules import schedules_router
from assistant_kernel.services.template_bundle import TemplateBundle
from assistant_kernel.services.published_agents import PublishedAgentService
from test_v12_packages import candidate
from test_v12_foundations import source


async def test_http_uses_same_receipts_and_rejects_cross_project(core_settings):
    repo,schedules,scheduler,task,control,due=await fixture(core_settings)
    app=FastAPI();app.include_router(schedules_router(core_settings,repo,lambda:None))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://isolated') as client:
        base='/v1/projects/demo/schedules'
        assert (await client.get(base)).json()['items'][0]['schedule_id']=='clock-test'
        preview=await client.post(base+'/preview',json={'rule':{'kind':'interval','anchor':due.isoformat(),'seconds':10}})
        assert preview.status_code==200 and len(preview.json()['times'])==5
        body={k:v for k,v in control.items() if k!='schedule_id'};body.update(task_id=task,action='pause',expected_control_revision=1,idempotency_key='api-pause')
        result=await client.post(base+'/clock-test/control',json=body)
        assert result.status_code==200 and result.json()['state']=='paused'
        assert (await client.post(base+'/clock-test/control',json=body)).json()==result.json()
        assert (await client.post('/v1/projects/unrelated/schedules/clock-test/control',json=body)).status_code==403
        assert (await client.post(base+'/preview',json={'rule':{'kind':'once','at':'2000-01-01T00:00:00Z'}})).status_code==422
    from assistant_kernel.services.operation_records import OperationRecords
    listing=await OperationRecords(core_settings,repo).query('demo',{'kind':'schedules','limit':20,'cursor':None,'lookback_days':7,'object_id':None})
    assert listing['coverage']['status']=='complete' and len(listing['items'])==1


def test_fixed_phase_skills_and_control_permissions(core_settings):
    bundle,base,_=candidate(core_settings)
    for phase in ('requirements','design','build','audit','operate'):
        config,receipt=bundle.assemble(base,actor='pm',phase=phase,conditions={'schedule_work'},condition_source_refs=[source()],
            local_role_append='',local_role_source_refs=[])
        assert 'assistant-schedule-management' in receipt['loaded_skills']
        assert {'preview_schedule','list_schedules','schedule_status','list_schedule_ticks'}<=set(config.allowed_tools)
        if phase=='operate':assert {'register_schedule','control_schedule'}<=set(config.allowed_tools)
        else:assert not {'register_schedule','control_schedule'}&set(config.allowed_tools)


async def test_new_rule_loads_schedule_skill_from_accepted_structured_requirement(core_settings):
    from test_v12_p3_phase_control import baseline
    from psycopg.types.json import Jsonb
    repo,prompts,task,phases,write=await baseline(core_settings)
    request=task['request_json'];accepted=request['accepted_documents'];contract=prompts.documents.read('demo',accepted['task_contract'])['payload']
    contract['schedule_requirements']=[{'schedule_id':'daily','runtime_unit_id':'existing','action':'run','inputs':{},
        'rule':{'kind':'cron','expression':'0 9 * * *'},'timezone':'Asia/Shanghai','acceptance_ids':['A2'],'requires_first_run':False}]
    accepted['task_contract']=write('task-contract','task-contract.v1',contract)
    task['request_json']={**request,'accepted_documents':accepted}
    service=PublishedAgentService(core_settings)
    with service.connection() as c:head=c.execute('SELECT value FROM assistant.kernel_meta WHERE key=%s',(service.key('demo'),)).fetchone()['value']
    package=service.read_package('demo',head['package_ref']);pinned=next(iter(package['configurations'].values()))
    base=service.loader.load_snapshot('demo',pinned['uri'],pinned['sha256'])
    from pathlib import Path
    bundle=TemplateBundle(Path(base.skill_bindings[0]['source_root']).parents[1])
    config,_=bundle.assemble(base,actor='pm',phase='build',conditions=set(),condition_source_refs=[],local_role_append='',local_role_source_refs=[])
    selected,sources=service.runtime_skills('demo',config,package,task)
    assert any(x['name']=='assistant-schedule-management' and x.get('published_ref') for x in selected.skill_bindings)
    assert accepted['task_contract'] in sources


async def test_future_configuration_acceptance_does_not_require_early_business_execution(core_settings):
    from copy import deepcopy
    from assistant_kernel.services.runtime_handoffs import RuntimeHandoffs
    from assistant_kernel.services.runtime_documents import write_runtime_document
    repo,schedules,scheduler,task_id,control,due=await fixture(core_settings,once=True)
    task=await repo.get_run(task_id);accepted=deepcopy(task['request_json']['accepted_documents'])
    status=await schedules.status('demo','clock-test');definition=status['definition'];unit_ref=definition['runtime_unit_ref']
    unit=schedules.units.read_definition('demo',unit_ref)
    plan={k:definition[k] for k in ('schedule_id','action','inputs','rule','timezone')};plan.update(runtime_unit_id=unit['runtime_unit_id'],acceptance_ids=['A2'],requires_first_run=False)
    def write(kind,value):
        return schedules.documents.write_scoped_revision(project_id='demo',scope='task',object_id=kind,task_id=task_id,run_id=task_id,
            document_kind=kind,document_schema=kind+'.v1',payload=value,status='submitted',actor_type='kernel',actor_id='isolated-fixture',
            reason='Synthetic configured-only obligation; never business execution evidence',source_refs=control['authorization_source_refs'])
    design=schedules.documents.read('demo',accepted['solution_design'])['payload']
    design['runtime_designs']=[{'runtime_unit_id':unit['runtime_unit_id'],'lifecycle_kind':'finite','trigger_mode':'on_demand','execution_mode':'on_demand',
        'entrypoint':'program/main.py','workflow_framework':'none','planned_agents':[],'role_asset_paths':[],'skill_asset_paths':[],'mcp_server_tools':[],
        'dependency_refs':[],'chosen_reason':'Future configured-only fixture','activation_and_recovery_plan':['arm schedule'],
        'test_obligations':['prove registration'],'unverified':[],'config_bindings':[]}]
    design['schedule_plans']=[plan];accepted['solution_design']=write('solution-design',design)
    implementation=schedules.documents.read('demo',accepted['implementation_report'])['payload']
    implementation['schedule_handoffs']=[status['registration']['definition_ref']];accepted['implementation_report']=write('implementation-report',implementation)
    task['request_json']['accepted_documents']=accepted
    manifest=schedules.documents.read('demo',unit['implementation_manifest_ref'])['payload'];proof=status['registration']['last_control_receipt_ref']
    report={'publication_ref':accepted['publication'],'actual_version_refs':[unit_ref], 'environment_ref':manifest['environment']['environment_ref'],
        'action_receipt_refs':[proof],'per_item_checks':[{'check_id':'schedule-armed','criterion_ids':['A2'],'result':'passed',
            'actual_result':'Registered and armed; no business execution claimed','evidence_refs':[proof],'reason':None}],'unverified':[]}
    activation=write_runtime_document(core_settings,task,'activation-report',report)
    handoff={'definition_ref':unit_ref,'activation_refs':[activation]}
    async with repo.database.connection() as c:
        assert await RuntimeHandoffs(core_settings,repo).accept_activation_locked(c,task=task,handoffs=[handoff])==activation
        assert (await (await c.execute("SELECT count(*) n FROM assistant.runs WHERE request_json ? 'schedule_binding'")).fetchone())['n']==0
    design['schedule_plans'][0]['requires_first_run']=True;accepted['solution_design']=write('solution-design',design)
    async with repo.database.connection() as c:
        with pytest.raises(PermissionError,match='actual successful timed'):
            await RuntimeHandoffs(core_settings,repo).accept_activation_locked(c,task=task,handoffs=[handoff])
