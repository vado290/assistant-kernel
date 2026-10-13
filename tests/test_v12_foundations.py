"""Contract, immutable artifact and actual isolated DB configuration tests."""
import asyncio
from copy import deepcopy
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import uuid

import pytest
from psycopg.types.json import Jsonb

from assistant_kernel.config.system_config import (
    changed_config, parse_system_config, resolve_config_bindings, validate_system_config,
)
from assistant_kernel.repositories import CoreRepository
from assistant_kernel.services.contracts import ContractError, ContractRegistry, contracts
from assistant_kernel.services.document_runtime import TaskDocumentService, resource_ref
from assistant_kernel.services.system_config import SystemConfigService, STATE_KEY
from test_core4 import core_settings


def example_config():
    return json.loads((contracts().root/'kernel-system.example.json').read_text())


def source():
    return resource_ref(kind='message',identity=str(uuid.uuid4()),uri='db://assistant/messages/fixture',purpose='isolated test source')


def requirement():
    return {'goal':'Count three lines','scope':{'included':['line count'],'excluded':[]},
            'requirements':[{'requirement_id':'R1','statement':'Count three lines','priority':'must','source_refs':[source()],'acceptance_ids':['A1']}],
            'acceptance_criteria':[{'criterion_id':'A1','requirement_ids':['R1'],'applicability':'artifact','method':'count fixture lines','expected_result':'3','required':True,'requires_user_feedback':False}],
            'constraints':[],'open_questions':[],'delivery_obligations':['artifact'],'runtime_requirements':[]}


def write_requirement(settings, *, strict=True, payload=None, **changes):
    kwargs=dict(project_id='demo',task_id=str(uuid.uuid4()),document_kind='task-contract',
                document_schema='task-contract.v1',payload=payload if payload is not None else requirement(),
                status='proposed',actor_type='pm',actor_id='project:demo:manager',run_id=None,
                reason='isolated requirement',source_refs=[source()])
    kwargs.update(changes)
    svc=TaskDocumentService(settings)
    return svc.write_scoped_revision(scope='task',object_id='task-contract',**kwargs) if strict else svc.write_revision(**kwargs)


def test_server_registry_rejects_downgrade_and_tampered_package(tmp_path):
    registry=contracts();payload=requirement();payload.pop('runtime_requirements')
    with pytest.raises(ContractError):registry.validate('V12_Active_Contract',payload)
    import shutil
    shutil.copytree(registry.root,tmp_path/'contracts')
    (tmp_path/'contracts/schema-library-v12.json').write_text('{}')
    with pytest.raises(ContractError,match='hash differs'):ContractRegistry(tmp_path/'contracts')


def test_published_mcp_dtos_are_not_classified_as_legacy_only():
    registry=contracts()
    policy=registry.library['x-contract-policy']
    active=set(policy['active_roots']) | set(policy['active_dependencies'])
    for interface in registry.registry['public_interfaces'].values():
        assert set(interface.values()) <= active
        assert not set(interface.values()) & set(policy['compatibility_only'])
    assert set(registry.registry['internal_interfaces'].values()) <= active


def test_strict_config_rejects_duplicates_unknown_fields_and_secret_echo():
    with pytest.raises(ValueError,match='duplicate'):parse_system_config('{"x":1,"x":2}')
    with pytest.raises(ValueError,match='non-finite'):parse_system_config('{"x":NaN}')
    cfg=example_config();cfg['api_key']='do-not-echo-test-secret'
    result=validate_system_config(cfg)
    assert result['valid'] is False
    assert 'do-not-echo-test-secret' not in json.dumps(result)
    cfg=example_config();cfg['$schema']='https://example.invalid/evil.json'
    assert not validate_system_config(cfg)['valid']


def test_config_relations_and_explicit_zero_null():
    cfg=example_config();assert validate_system_config(cfg)['valid']
    assert cfg['postgres']['settings']['statement_timeout_ms']==0
    assert cfg['resource_limits']['max_declared_memory_mb'] is None
    cfg['python_defaults']['cpu_pool_size']=9
    assert not validate_system_config(cfg)['valid']
    cfg=example_config();cfg['paths']['data_directory']='../other'
    assert not validate_system_config(cfg)['valid']
    with pytest.raises(ValueError):changed_config(example_config(),[{'path':'/unknown/value','value':2}])
    with pytest.raises(ValueError):changed_config(example_config(),[{'path':'/python_defaults','value':{}},{'path':'/python_defaults/io_workers','value':2}])


def test_binding_hash_uses_selected_values_and_preserves_override():
    cfg=example_config();ref=resource_ref(kind='document',identity='config',uri='vault://kernel/config.json',schema_version='kernel-system-config.v1',revision_id='r1',sha256='0'*64)
    bindings=[{'path':'/python_defaults/cpu_pool_size','mode':'override','override_value':4},
              {'path':'/python_defaults/io_workers','mode':'inherit','override_value':None}]
    kwargs=dict(source_ref=ref,bindings=bindings,owner_ref=source(),captured_at='2026-10-08T00:00:00Z',effective_group_refs=[ref])
    a=resolve_config_bindings(cfg,**kwargs)
    cfg['logging']['backup_files']=99;cfg['python_defaults']['cpu_pool_size']=3
    b=resolve_config_bindings(cfg,**kwargs)
    assert a['subset_sha256']==b['subset_sha256']
    assert b['selected_values']['/python_defaults/cpu_pool_size']==4
    bindings[0]['override_value']=99
    with pytest.raises(ValueError,match='relationships'):resolve_config_bindings(cfg,**kwargs)


def test_old_bytes_preserved_strict_reader_checks_identity_and_active_selector(core_settings):
    svc=TaskDocumentService(core_settings)
    legacy=write_requirement(core_settings,strict=False)
    path=svc.runtime.resolve_vault_file('demo',legacy['uri'].split('vault://demo/')[1]);original=path.read_bytes()
    assert 'envelope_version' not in svc.read('demo',legacy)
    modern=write_requirement(core_settings)
    doc=svc.read('demo',modern)
    assert doc['envelope_version']=='doc-envelope.v3'
    assert doc['payload_definition']=='V12_Active_Contract'
    assert path.read_bytes()==original
    forged={**modern,'id':'another-owner'}
    with pytest.raises(ValueError,match='identity'):svc.read('demo',forged)
    payload=requirement();payload.pop('runtime_requirements')
    with pytest.raises(ContractError):write_requirement(core_settings,payload=payload)


def test_parallel_identical_document_writes_are_one_immutable_revision(core_settings):
    task=str(uuid.uuid4());payload=requirement();refs=[source()]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(lambda _:write_requirement(core_settings,task_id=task,payload=payload,source_refs=refs),range(4)))
    assert all(x==results[0] for x in results)
    changed=write_requirement(core_settings,task_id=task,payload=payload,source_refs=refs,reason='different actual reason')
    assert changed['revision_id']!=results[0]['revision_id']


def test_old_strict_documents_keep_their_pinned_reader_after_library_upgrade(core_settings, tmp_path, monkeypatch):
    import shutil
    import assistant_kernel.services.contracts as module
    previous=contracts()
    ref=write_requirement(core_settings)
    updated=tmp_path/'updated-contracts'
    shutil.copytree(previous.root,updated)
    library=json.loads((updated/'schema-library-v12.json').read_text())
    library['$id']='urn:isolated:test:next-contract-revision'
    (updated/'schema-library-v12.json').write_text(json.dumps(library))
    manifest=json.loads((updated/'manifest.json').read_text())
    manifest['files']['schema-library-v12.json']=hashlib.sha256((updated/'schema-library-v12.json').read_bytes()).hexdigest()
    (updated/'manifest.json').write_text(json.dumps(manifest))
    current=ContractRegistry(updated)
    monkeypatch.setattr(module,'contracts',lambda:current)
    service=TaskDocumentService(core_settings)
    assert service.read('demo',ref)['schema_library_ref']==previous.library_ref()
    new=write_requirement(core_settings)
    assert service.read('demo',new)['schema_library_ref']==current.library_ref()
    archive=core_settings.data_root/'runtime/contract-libraries'/previous.sha256
    (archive/'schema-library-v12.json').write_text('{}')
    with pytest.raises(ContractError,match='hash differs'):
        service.read('demo',ref)


async def seed(settings):
    svc=SystemConfigService(settings)
    await svc.accept(candidate=example_config(),expected_revision='uninitialized',reason='explicit isolated import',
                     source_kind='external_file',source_refs=[],write_input=True)
    return svc


async def user_input(settings,project='kernel'):
    result=await CoreRepository(settings).ingest_message(project_id=project,agent_key=f'project:{project}:intake',
        channel='test',chat_id='fixture',user_id='test-owner',external_event_id=str(uuid.uuid4()),
        external_message_id=None,content={'text':'Explicitly update the test configuration'})
    return str(result['message']['message_id'])


async def test_configuration_acceptance_does_not_claim_effective_and_cas_is_atomic(core_settings):
    svc=await seed(core_settings);state=await svc.state()
    effective=await svc.view(view='effective',paths=['/python_defaults/cpu_pool_size'],management=True)
    assert effective['values']['/python_defaults/cpu_pool_size'] is None
    one,two=await user_input(core_settings),await user_input(core_settings)
    result=await asyncio.gather(
        svc.update(changes=[{'path':'/python_defaults/cpu_pool_size','value':3}],expected_revision=state['revision'],reason='request one',source_message_id=one),
        svc.update(changes=[{'path':'/python_defaults/cpu_pool_size','value':4}],expected_revision=state['revision'],reason='request two',source_message_id=two),return_exceptions=True)
    assert sum(isinstance(x,ValueError) for x in result)==1
    assert sum(isinstance(x,dict) for x in result)==1
    state=await svc.state()
    assert json.loads(svc.path.read_text())==svc._config(state['desired_ref'])
    assert all(g['effective_ref'] is None for g in state['groups'])


async def test_consumer_receipt_and_group_failure_preserve_actual_value(core_settings):
    svc=await seed(core_settings);state=await svc.state();evidence=source()
    await svc.settle_group(expected_revision=state['revision'],group='python_defaults:next_invocation',evidence_ref=evidence,applied=True,reason='isolated consumer loaded the exact snapshot')
    source_id=await user_input(core_settings)
    await svc.update(changes=[{'path':'/python_defaults/cpu_pool_size','value':4}],expected_revision=state['revision'],reason='new default',source_message_id=source_id)
    current=await svc.state()
    await svc.settle_group(expected_revision=current['revision'],group='python_defaults:next_invocation',evidence_ref=evidence,applied=False,reason='consumer unavailable')
    view=await svc.view(view='effective',paths=['/python_defaults'],management=True)
    assert view['values']['/python_defaults']['cpu_pool_size']==2
    assert any(g['status']=='failed' for g in view['group_results'])
    assert svc.documents.read('kernel',(await svc.state())['receipt_ref'])['payload']['runtime_verified'] is False


async def test_external_half_write_preserves_head_and_restoration_is_new_revision(core_settings):
    svc=await seed(core_settings);old=await svc.state()
    svc.path.write_text('{"half":')
    assert (await svc.poll(settle_seconds=0))['status']=='settling'
    assert (await svc.poll(settle_seconds=0))['status']=='invalid'
    assert (await svc.state())['revision']==old['revision']
    cfg=example_config();cfg['python_defaults']['cpu_pool_size']=3;svc.path.write_text(json.dumps(cfg))
    await svc.poll(settle_seconds=0);assert (await svc.poll(settle_seconds=0))['status']=='accepted'
    current=await svc.state();source_id=await user_input(core_settings)
    result=await svc.restore(revision=old['revision'],expected_revision=current['revision'],source_message_id=source_id)
    assert result['desired_ref']['revision_id'] not in [old['revision'],current['revision']]
    assert json.loads(svc.path.read_text())['python_defaults']['cpu_pool_size']==2


async def test_projection_recovery_after_file_rename_before_db_marker(core_settings):
    svc=await seed(core_settings);state=await svc.state();original=svc.path.read_bytes()
    state.update(input_projection_pending=True,input_sha256='old-pre-rename-hash')
    async with svc.database.connection() as c:
        await c.execute('UPDATE assistant.kernel_meta SET value=%s WHERE key=%s',(Jsonb(state),STATE_KEY));await c.commit()
    assert await svc.reconcile_input_projection()
    assert svc.path.read_bytes()==original
    assert not (await svc.state())['input_projection_pending']


async def test_ordinary_project_cannot_modify_or_read_infrastructure(core_settings):
    svc=await seed(core_settings);state=await svc.state();source_id=await user_input(core_settings,'demo')
    with pytest.raises(PermissionError):
        await svc.update(changes=[{'path':'/python_defaults/cpu_pool_size','value':3}],expected_revision=state['revision'],reason='not authorized',source_message_id=source_id)
    with pytest.raises(PermissionError):await svc.view(view='desired',paths=['/postgres'],management=False)
    public=await svc.view(view='desired',paths=[],management=False)
    assert not any(k.startswith('/providers') or k.startswith('/channels') or k.startswith('/postgres') for k in public['values'])


async def test_file_watcher_accepts_stable_input_but_only_marks_its_own_consumer_applied(core_settings):
    from assistant_kernel.services.config_watcher import watch_system_config
    svc=SystemConfigService(core_settings);cfg=example_config();cfg['config_updates']['poll_seconds']=0.05;cfg['config_updates']['settle_seconds']=1
    await svc.accept(candidate=cfg,expected_revision='uninitialized',reason='isolated watcher setup',
                     source_kind='external_file',source_refs=[],write_input=True)
    initial=await svc.state();stop=asyncio.Event();watch=asyncio.create_task(watch_system_config(core_settings,stop))
    try:
        for _ in range(100):
            state=await svc.state()
            if any(g['group']=='config_updates:dynamic_admission' and g['status']=='applied' for g in state['groups']):break
            await asyncio.sleep(.05)
        else:pytest.fail('watcher did not report actual adoption')
        cfg['python_defaults']['cpu_pool_size']=3;svc.path.write_text(json.dumps(cfg))
        for _ in range(100):
            state=await svc.state()
            if state['revision']!=initial['revision']:break
            await asyncio.sleep(.05)
        else:pytest.fail('stable configuration edit was not accepted')
        assert all(g['status']!='applied' for g in state['groups'] if g['group']!='config_updates:dynamic_admission')
        assert svc._config(state['desired_ref'])['python_defaults']['cpu_pool_size']==3
    finally:
        stop.set();await asyncio.wait_for(watch,5)


async def test_new_tool_error_preserves_unknown_effect_and_matches_contract():
    from assistant_kernel.mcp_server.error_protocol import v12_tool_errors
    @v12_tool_errors(mutating=True)
    async def update_system_config():
        raise ValueError('configuration file changed after acceptance')
    result=await update_system_config()
    contracts().validate('ToolError',result)
    assert result['effect_status']=='unknown'
    assert '不要重放' in result['hint']
    @v12_tool_errors()
    async def read_project_document():
        raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):await read_project_document()


def test_missing_tick_and_unknown_process_identity_have_truthful_contracts():
    r=contracts()
    rejected={'decision':'rejected','request_receipt_ref':source(),'project_id':None,'schedule_id':None,
              'reason_code':'untrusted_due','evidence_refs':[],'received_at':'2026-10-08T00:00:00Z'}
    r.validate('ScheduleTriggerResult',rejected)
    process={'launch_state':'identity_unknown','run_ref':source(),'attempt':1,'pid':123,
             'process_group_id':123,'start_ticks':None,'exit_code':0,'signal':None,
             'status':'unknown','evidence_refs':[]}
    r.validate('V12_ProcessReceipt',process)
    process['launch_state']='started'
    with pytest.raises(ContractError):r.validate('V12_ProcessReceipt',process)
