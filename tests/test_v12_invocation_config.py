from copy import deepcopy
import json
import uuid

from psycopg.types.json import Jsonb
from assistant_kernel.services.invocation_config import InvocationConfiguration
from assistant_kernel.services.system_config import STATE_KEY
from assistant_kernel.services.directory_assessment import configured_parameters,assess_directory,record_configuration_use
from test_core4 import core_settings
from test_v12_foundations import seed,source
from test_v12_packages import candidate


async def test_invocation_keeps_explicit_overrides_and_pinned_resume_values(core_settings):
    global_service=await seed(core_settings)
    _,config,_=candidate(core_settings)
    resolver=InvocationConfiguration(core_settings);run_id=str(uuid.uuid4())
    first,values,ref=await resolver.resolve(config,project_id='demo',run_id=run_id,invocation_id='one')
    assert values['timeout_seconds']==config.timeout_seconds
    assert first.model=='gpt-5.6-sol'
    budget=resolver.check_role_budget(first,values)
    assert budget['estimated_role_tokens']>0 and budget['truncated'] is False
    old=await global_service.state();updated=global_service._config(old['desired_ref'])
    updated['agent_defaults']['model']='gpt-6-sol'
    updated['agent_defaults']['timeout_seconds']=999
    await global_service.accept(candidate=updated,expected_revision=old['revision'],reason='isolated global change',
        source_kind='external_file',source_refs=[],write_input=True)
    second,values2,ref2=await resolver.resolve(config,project_id='demo',run_id=str(uuid.uuid4()),invocation_id='two')
    assert second.model=='gpt-6-sol'
    assert values2['timeout_seconds']==config.timeout_seconds
    resumed,old_values,same_ref=await resolver.resolve(first,project_id='demo',run_id=run_id,invocation_id='retry',pinned_ref=ref)
    assert same_ref==ref and resumed.model=='gpt-5.6-sol' and old_values==values
    current=await global_service.state();updated['logging']['backup_files']+=1
    await global_service.accept(candidate=updated,expected_revision=current['revision'],reason='unrelated log change',
        source_kind='external_file',source_refs=[],write_input=True)
    third,_,_=await resolver.resolve(config,project_id='demo',run_id=str(uuid.uuid4()),invocation_id='three')
    assert second.config_sha256==third.config_sha256


async def test_group_migration_preserves_values_and_scan_marks_only_used_subgroups(core_settings,tmp_path):
    service=await seed(core_settings);old=await service.state();file_bytes=service.path.read_bytes()
    legacy=deepcopy(old);map_group=next(x for x in legacy['groups'] if x['group'].startswith('map/'))
    legacy['groups']=[x for x in legacy['groups'] if not x['group'].startswith('map/')]+[{**map_group,'group':'map:dynamic_admission'}]
    async with service.database.connection() as c:
        await c.execute('UPDATE assistant.kernel_meta SET value=%s WHERE key=%s',(Jsonb(legacy),STATE_KEY));await c.commit()
    migrated=await service.repartition_application_groups()
    assert migrated['desired_ref']==old['desired_ref'] and service.path.read_bytes()==file_bytes
    path=tmp_path/'external';path.mkdir();(path/'one').write_text('metadata only')
    params=await configured_parameters(core_settings)
    assessment=assess_directory(project_id='demo',root_key='external',path=path,context='expand',source_refs=[source()],**params)
    await record_configuration_use(core_settings,[assessment])
    state=await service.state()
    applied={g['group'] for g in state['groups'] if g['status']=='applied'}
    assert applied=={'map/scan:dynamic_admission','map/external_directory_warning:dynamic_admission'}
    view=await service.view(view='effective',paths=['/map/scan/max_files','/map/read_max_bytes'],management=False)
    assert view['values']['/map/scan/max_files']==10000
    assert view['values']['/map/read_max_bytes'] is None


async def test_supervisor_defaults_preserve_legacy_overrides_and_resolve_inherited_tiers(core_settings):
    from types import SimpleNamespace
    from assistant_kernel.services.goal_supervisor import GoalSupervisorService
    from assistant_kernel.services.supervisor_config import legacy_bindings, resolve_supervisor_configuration
    service = await seed(core_settings)
    candidate(core_settings)
    base = GoalSupervisorService(core_settings, SimpleNamespace()).configuration('demo')
    bindings = legacy_bindings(base)
    inherited = '/supervisor_defaults/normal/model'
    bindings = [dict(item, mode='inherit', override_value=None) if item['path'] == inherited else item
        for item in bindings]
    old = await service.state(); updated = service._config(old['desired_ref'])
    updated['supervisor_defaults']['normal']['model'] = 'gpt-6-sol'
    updated['supervisor_defaults']['intent']['model'] = 'gpt-6-luna'
    await service.accept(candidate=updated, expected_revision=old['revision'], reason='isolated Supervisor defaults',
        source_kind='external_file', source_refs=[], write_input=True)
    resolved = await resolve_supervisor_configuration(core_settings, project_id='demo', run_id=str(uuid.uuid4()),
        base=base, bindings=bindings)
    assert resolved['models']['main_evaluation']['model'] == 'gpt-6-sol'
    assert resolved['models']['intent_panel']['model'] == base['models']['intent_panel']['model']
    assert resolved['retry_delays_seconds'] == [30, 120, 600]
    # Resolving a value is not a claim that recovery has consumed it.
    status = await service.state()
    assert all(g['status'] != 'applied' for g in status['groups'] if g['group'].startswith('supervisor_defaults:'))
