"""C26 active removal, immutable history and manual operation compatibility."""
import json,os
from pathlib import Path
import pytest
from psycopg.types.json import Jsonb
from assistant_kernel.services.contracts import contracts,ContractError,ContractRegistry
from assistant_kernel.services.template_bundle import TemplateBundle
from assistant_kernel.services.document_runtime import TaskDocumentService
from assistant_kernel.services.scheduling_scope import active_configuration,RETIRED_CONFIG_PATHS,SCHEDULING_NOTICE
from assistant_kernel.services.system_config import SystemConfigService,STATE_KEY
from assistant_kernel.config.system_config import changed_config,validate_system_config
from test_core4 import core_settings
from test_v12_foundations import example_config,source


def test_no_active_schedule_writers_or_template_loads():
    registry=contracts();policy=registry.library['x-contract-policy']
    active=policy['active_roots']+policy['active_dependencies']
    assert not any('n8n' in name.lower() for name in active)
    for name in ('python-schedule-definition.v1','schedule-trigger.v1','n8n-readiness-gate.v1'):
        with pytest.raises(ContractError,match='no active writer'):registry.document_definition(name)
    bundle=TemplateBundle()
    assert not (bundle.root/'skills/assistant-schedule-design/SKILL.md').exists()
    assert all(x['skill_name']!='assistant-schedule-design' for p in bundle.profiles['profiles'].values() for x in p['conditional_skill_rules'])
    assert 'C26' in bundle.text('roles/common-control.md') and 'Kernel owns' in SCHEDULING_NOTICE
    assert registry.document_definition('kernel-schedule-definition.v1')=='KernelScheduleDefinition'


def test_current_config_cannot_reenable_external_scheduler():
    cfg=example_config();assert validate_system_config(cfg)['valid']
    assert active_configuration(cfg)==cfg
    for path in RETIRED_CONFIG_PATHS:
        with pytest.raises(ValueError,match='not a published field'):changed_config(cfg,[{'path':path,'value':{}}])
    cfg['n8n']={}
    assert not validate_system_config(cfg)['valid']


async def test_actual_previous_reader_and_config_references_remain_readable(core_settings,monkeypatch):
    # The runner supplies the actual sealed 0.28.7 contract bundle. No invented
    # historical schema is substituted for this upgrade compatibility check.
    old_path=os.getenv('C26_OLD_CONTRACT_ROOT')
    if not old_path:pytest.skip('Actual previous release reader is required for upgrade verification')
    old=ContractRegistry(Path(old_path));old_payload=json.loads((old.root/'kernel-system.example.json').read_text())
    assert old.implementation_revision==4 and 'n8n' in old_payload
    documents=TaskDocumentService(core_settings)
    with monkeypatch.context() as patch:
        patch.setattr('assistant_kernel.services.contracts.contracts',lambda:old)
        ref=documents.write_scoped_revision(project_id='kernel',scope='project',object_id='system-config',document_kind='kernel-system-config',
            document_schema='kernel-system-config.v1',payload=old_payload,status='submitted',actor_type='kernel',actor_id='fixture',
            reason='Actual prior contract isolated compatibility fixture',source_refs=[source()])
    path=documents.runtime.resolve_vault_file('kernel',ref['uri'].removeprefix('vault://kernel/'));before=path.read_bytes()
    assert documents.read('kernel',ref)['payload']==old_payload
    service=SystemConfigService(core_settings)
    active=service._config(ref)
    assert 'n8n' not in active and active['resource_limits']==old_payload['resource_limits']
    async with service.database.connection() as c:
        state={'revision':ref['revision_id'],'desired_ref':ref,'groups':[
            {'group':'n8n:external_adapter','effective_ref':None,'status':'pending_boundary','apply_mode':'external_adapter',
                'previous_ref':None,'receipt_refs':[],'reason':'Old scheduler configuration'},
            {'group':'system:next_invocation','effective_ref':ref,'status':'applied','apply_mode':'next_invocation',
                'previous_ref':None,'receipt_refs':[],'reason':'Actual isolated prior config'}]}
        await c.execute('INSERT INTO assistant.kernel_meta(key,value) VALUES (%s,%s)',(STATE_KEY,Jsonb(state)));await c.commit()
    view=await service.view(view='desired',paths=[],management=True)
    assert not any('/n8n' in name for name in view['values'])
    assert not any(g['group'].startswith('n8n:') for g in view['group_results'])
    receipt=await service.accept(candidate=active,expected_revision=ref['revision_id'],reason='C26 isolated retirement',
        source_kind='external_file',source_refs=[source()],write_input=True)
    assert documents.read('kernel',receipt['desired_ref'])['schema_library_ref']==contracts().library_ref()
    assert documents.read('kernel',ref)['payload']==old_payload and path.read_bytes()==before


def test_new_schedule_assets_do_not_pass_candidate_validation(core_settings,tmp_path):
    from assistant_kernel.services.validation_runner import ValidationRunner
    path=tmp_path/'schedule.yaml';path.write_text('schedule_id: historical-example\ntimezone: Asia/Shanghai\ntrigger: daily\ntarget: script\n')
    issues=[];ValidationRunner(core_settings)._validate_schedule(path,'schedules/example.yaml',issues)
    assert [x['code'] for x in issues]==['schedule_invalid']


def test_public_template_refresh_preserves_local_role_model_tools_and_limits(core_settings):
    from dataclasses import replace
    from test_v12_packages import candidate
    from scripts.adopt_c26_templates import refresh_config
    _,base,_=candidate(core_settings)
    old_path=os.getenv('C26_OLD_CONTRACT_ROOT')
    if not old_path:pytest.skip('Actual prior template required')
    old=TemplateBundle(Path(old_path).parent/'prompts/v12');new=TemplateBundle()
    prior,_=old.assemble(base,actor='pm',phase='design',conditions={'schedule_work'},condition_source_refs=[source()],
        local_role_append='Keep this exact Project-specific line.',local_role_source_refs=[source()])
    updated=refresh_config(prior,old,new)
    assert 'Keep this exact Project-specific line.' in updated.role_text
    assert 'n8n' not in updated.role_text.lower()
    assert not any(x['name']=='assistant-schedule-design' for x in updated.skill_bindings)
    for field in ('model','provider','reasoning_effort','timeout_seconds','allowed_tools','config_bindings','pinned_mcp_servers'):
        assert getattr(updated,field)==getattr(prior,field)


def test_package_adoption_keeps_explicit_supervisor_budget_and_timeout(core_settings):
    from dataclasses import replace
    from types import SimpleNamespace
    from scripts.adopt_c26_templates import adopt
    from assistant_kernel.services.published_agents import PublishedAgentService
    from assistant_kernel.services.goal_supervisor import GoalSupervisorService
    from assistant_kernel.services.supervisor_config import legacy_bindings
    from test_v12_packages import candidate
    _,base,_=candidate(core_settings)
    old_path=os.getenv('C26_OLD_CONTRACT_ROOT')
    if not old_path:pytest.skip('Actual prior template required')
    old=TemplateBundle(Path(old_path).parent/'prompts/v12').preserve(core_settings.data_root)
    base=replace(base,agent_key='project:demo:supervisor',agent_id='supervisor',kind='supervisor',phase_mode=None)
    config,_=old.assemble(base,actor='supervisor',phase=None,conditions=set(),condition_source_refs=[],local_role_append='',local_role_source_refs=[])
    bindings=legacy_bindings(GoalSupervisorService(core_settings,SimpleNamespace()).configuration('demo'))
    values={'/supervisor_defaults/daily_input_tokens':500000,'/supervisor_defaults/daily_output_tokens':100000,
        '/supervisor_defaults/normal/timeout_seconds':600}
    bindings=[{**x,'mode':'override','override_value':values[x['path']]} if x['path'] in values else x for x in bindings]
    service=PublishedAgentService(core_settings)
    ref=service.prepare('demo',configurations={config.agent_key+'/default':config},template_ref=old.ref('profiles.json'),
        adoption_scope={'project_ids':['demo'],'existing_task_ids':[],'new_tasks':True,'next_turn_binding_confirmation':True},
        source_refs=[source()],supervisor_binding_specs=bindings)
    service.adopt('demo',package_ref=ref,expected_previous_ref=None,approval_refs=[source()])
    decision=service.loader.runtime.ensure('kernel').vault_root/'operations/upgrade-decisions/C26.json'
    decision.parent.mkdir(parents=True,exist_ok=True);decision.write_text('{"fixture":"synthetic isolated adoption, not human approval"}')
    migrated=adopt(core_settings,apply=True)
    current=service.read_package('demo',migrated[0]['package_ref'])
    assert current['supervisor_config_bindings']==bindings
