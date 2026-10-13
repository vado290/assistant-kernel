"""Necessary isolated DB checks; no real business service, user approval or model invocation."""
import hashlib,json
from pathlib import Path
from copy import deepcopy
import pytest
from psycopg.types.json import Jsonb
from test_core4 import core_settings
from test_v12_p4_units import unit
from assistant_kernel.services.service_runtime import ServiceRuntime
from assistant_kernel.services.service_work import ServiceWork
from assistant_kernel.services.document_runtime import resource_ref

async def service_fixture(settings,real_process=False):
    repo,units,_,control,submission=await unit(settings)
    async with repo.database.connection() as c:
        await c.execute((Path(__file__).parents[1]/'migrations/upgrade_core5_v10.sql').read_text());await c.commit()
        await c.execute("INSERT INTO assistant.kernel_meta(key,value) VALUES('project-file-ledger:v2','{\"write_enabled\":true,\"backfill_complete\":true}') ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value");await c.commit()
    old=units.read_definition('demo',control['runtime_unit_ref']);finite=units.documents.read('demo',submission['manifest_ref'])['payload']
    service=ServiceRuntime(settings,repo)
    def doc(kind,payload):
        return units.documents.write_scoped_revision(project_id='demo',scope='project',object_id='synthetic-service',document_kind=kind,
            document_schema=kind+'.v1',payload=payload,status='submitted',actor_type='kernel',actor_id='isolated-fixture',
            reason='Synthetic service control fixture; never human approval or real service acceptance',source_refs=old['source_refs'])
    manifest={key:deepcopy(finite[key]) for key in ('code_bundle','environment','fixed_args','io','children','expected_effects','idempotence_statement','verification_refs')}
    manifest.update(service_id='formal-test',shared_resource_keys=[],config_binding_refs=[],
      resources={'startup_timeout_seconds':5,'stop_grace_seconds':1,'work_item_timeout_seconds':120,'cpu_pool_size':1,'start_method':'spawn',
        'io_limit':8,'numeric_threads':1,'max_inflight_model_calls':1,'max_steps_per_work_item':10,'max_model_cost_per_window_ref':None,
        'resource_policy_ref':None,'whole_process_timeout':'not_applicable_service_lifetime','log_limit_bytes':10000,
        'resolved_config_ref':finite['resolved_config_ref']},
      health={'readiness_timeout_seconds':5,'probe_kind':'runtime_events','probe_config_ref':service.blob('demo','probe-fixture',{}),
        'heartbeat_interval_seconds':1,'stale_after_seconds':5,'evidence_required':True},
      recovery={'mode':'automatic_bounded','max_restarts':0,'backoff_seconds':[],'stable_window_seconds':20,'max_restarts_per_window':0,
        'window_seconds':60,'recovery_owner':'kernel_maintenance','manager_autorestart':False,'unknown_effect':'reconcile_then_wait',
        'respect_desired_state':True,'over_limit':'pause_and_notify','authorization_ref':old['approved_execution_scope_ref']})
    manifest['io']['required_outputs']=[]
    if real_process:
        code="""import json,os,time,pathlib
root=pathlib.Path(os.environ['ASSISTANT_RUNTIME_CLIENT_PATH']).parent
while True:
    temporary=root/'ready.tmp'
    temporary.write_text(json.dumps({'ready':True,'instance_id':os.environ['ASSISTANT_RUN_ID']}))
    os.replace(temporary,root/'ready.json')
    time.sleep(0.2)
"""
        root=units.documents.runtime.describe('demo').vault_root;path=root/'program/service.py';path.write_text(code)
        entry=resource_ref(kind='source',identity='real-service-fixture',uri='vault://demo/program/service.py',sha256=hashlib.sha256(path.read_bytes()).hexdigest(),purpose='Actual no-network isolated heartbeat program')
        manifest['code_bundle']={'entrypoint_ref':entry,'module_refs':[],
            'bundle_ref':service.blob('demo','actual-bundle',{'schema_version':'python-code-bundle.v1','files':[{'relative_path':'service.py','ref':entry}]}),
            'closure_manifest_ref':service.blob('demo','actual-closure',{'schema_version':'python-import-closure.v1','stdlib_modules':['json','os','time','pathlib'],'dependency_modules':[]})}
        manifest['health'].update(probe_kind='heartbeat_file',probe_config_ref=service.blob('demo','actual-probe',{'relative_path':'ready.json'}))
        manifest['resources']['startup_timeout_seconds']=15
    manifest_ref=doc('python-service-manifest',manifest)
    definition={**old,'implementation_manifest_ref':manifest_ref,'lifecycle_kind':'service','execution_mode':'service'}
    ref=doc('runtime-unit-definition',definition)
    async with repo.database.connection() as c:
        await c.execute('UPDATE assistant.project_runtime_units SET effective_definition_ref=%s WHERE project_id=%s AND unit_id=%s',(Jsonb(ref),'demo','formal-test'));await c.commit()
    command={**control,'runtime_unit_ref':ref}
    await service.control('demo',command,authenticated_operator=True)
    async with repo.database.connection() as c:
        current=await (await c.execute('SELECT * FROM assistant.project_runtime_units')).fetchone()
    instance=await service.allocate(current,definition,manifest)
    assert instance and instance['observed_state']=='starting'
    if real_process:return repo,service,instance,command
    # Simulate the observed manager state solely for isolated transition tests.
    async with repo.database.connection() as c:
        state=deepcopy(instance['state_json']);state['work_control']['gate']='open'
        await c.execute("UPDATE assistant.service_instances SET observed_state='ready',state_json=%s WHERE instance_id=%s",(Jsonb(state),instance['instance_id']));await c.commit()
    instance=await service.get(instance['instance_id']);await service.settle_control(current,command,instance)
    return repo,service,instance,command

async def test_service_io_is_busy_and_restart_does_not_queue_a_later_restart(core_settings):
    repo,service,instance,command=await service_fixture(core_settings);work=ServiceWork(core_settings,repo)
    await work.action(instance,'work-begin',{'logical_execution_id':'io','input':{'n':21}})
    ref=await service.control('demo',{**command,'action':'restart','expected_control_revision':1,'idempotency_key':'busy-restart'},authenticated_operator=True)
    receipt=service.documents.read('demo',ref)['payload'];assert 'skipped' in receipt['next_action']
    async with repo.database.connection() as c:
        unit=await (await c.execute('SELECT * FROM assistant.project_runtime_units')).fetchone()
    assert unit['desired_state']=='running' and not unit['control_json'].get('pending_control')
    assert (await service.get(instance['instance_id']))['state_json']['work_control']['gate']=='open'
    await work.action(instance,'work-end',{'logical_execution_id':'io','result':{'answer':42}})
    duplicate=await work.action(instance,'work-begin',{'logical_execution_id':'io','input':{'n':21}})
    assert duplicate['duplicate'] and duplicate['result']=={'answer':42}

async def test_service_cpu_uses_global_capacity_and_release_is_generation_bound(core_settings):
    repo,service,instance,_=await service_fixture(core_settings);work=ServiceWork(core_settings,repo)
    await work.action(instance,'work-begin',{'logical_execution_id':'cpu','input':{'n':1}})
    request={'owner_ref':resource_ref(kind='receipt',identity=str(instance['instance_id']),uri='db://assistant/service_instances/'+str(instance['instance_id']),purpose='actual test instance'),
      'logical_execution_id':'cpu','resource_kind':'cpu_work_item','cpu_slots':1,'model_invocations':0,
      'expected_instance_generation':instance['generation'],'idempotency_key':'compute'}
    assert (await work.cpu(instance,request))['status']=='reserved'
    with pytest.raises(PermissionError,match='still owns'):await work.action(instance,'work-end',{'logical_execution_id':'cpu','result':{'answer':2}})
    assert (await work.cpu(instance,request,release=True))['status']=='released'
    await work.action(instance,'work-end',{'logical_execution_id':'cpu','result':{'answer':2}})
    with pytest.raises(PermissionError,match='generation'):await work.cpu(instance,{**request,'expected_instance_generation':99})

async def test_service_question_uses_existing_dual_entry_and_keeps_work_busy(core_settings):
    import yaml,uuid
    from assistant_kernel.services.question_groups import QuestionGroupService
    repo,service,instance,command=await service_fixture(core_settings);work=ServiceWork(core_settings,repo)
    definition=service.units.read_definition('demo',instance['definition_ref']);design=service.documents.read('demo',definition['design_ref'])
    task=await repo.get_run(design['task_id']);source=await repo.get_message(str(task['trigger_message_id']))
    config=core_settings.data_root/'config/channel-bindings.yaml';config.parent.mkdir(exist_ok=True)
    config.write_text(yaml.safe_dump({'bindings':[{'project_id':'demo','channel':source['channel'],'chat_id':chat,'user_id':source['user_id'],
        'agent_key':'project:demo:'+role} for chat,role in [('pm-service','manager'),('sup-service','supervisor')]]}))
    await work.action(instance,'work-begin',{'logical_execution_id':'waiting','input':{'n':1}})
    question={'question_id':'format','question_text':'Pick format','topic':'format','required':True,'options':[],'recommended_option_id':None,'recommendation_reason':'','source_refs':[]}
    waiting=await work.action(instance,'work-wait',{'logical_execution_id':'waiting','questions':[question],'checkpoint_ref':None})
    async with repo.database.connection() as c:
        group=await (await c.execute('SELECT * FROM assistant.messages WHERE message_id=%s',(waiting['group_id'],))).fetchone()
        projection=await (await c.execute('SELECT * FROM assistant.messages WHERE message_id=%s',(group['content_json']['projection_ids'][0],))).fetchone()
    assert group['run_id'] is None and len(group['content_json']['projection_ids'])==2
    assert (await service.get(instance['instance_id']))['state_json']['work_control']['items'][work.identity(instance,'waiting')]['state']=='waiting_input'
    await QuestionGroupService(core_settings,repo).record_action(message_id=str(projection['message_id']),project_id='demo',channel=projection['channel'],
      chat_id=projection['chat_id'],user_id=projection['user_id'],external_event_id='synthetic-'+uuid.uuid4().hex,revision=-1,page=-1,action='explicit_text',
      value={'format':'compact'},content_version=group['content_json']['group']['content_version'],original_text='Synthetic test answer, not a human acceptance')
    result=await work.action(instance,'work-answers',{'logical_execution_id':'waiting'})
    assert result=={'status':'answered','answers':{'format':'compact'}}
    next_question=await work.action(instance,'work-wait',{'logical_execution_id':'waiting','questions':[question],'checkpoint_ref':None})
    assert next_question['group_id']!=waiting['group_id']
