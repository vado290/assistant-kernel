"""Necessary deterministic operation/config checks; no real notification delivery or model call."""
from copy import deepcopy
from datetime import datetime,timezone
import uuid
import pytest
from psycopg.types.json import Jsonb
from test_core4 import core_settings
from test_v12_p4_units import unit
from assistant_kernel.services.operation_records import OperationRecords
from assistant_kernel.services.document_runtime import resource_ref
from assistant_kernel.services.config_consumers import ConfigConsumers

async def test_operation_events_require_specific_policy_and_deduplicate_retries(core_settings):
    repo,units,_,_,submission=await unit(core_settings);records=OperationRecords(core_settings,repo)
    async with repo.database.connection() as c:
        await c.execute("INSERT INTO assistant.kernel_meta(key,value) VALUES('project-file-ledger:v2','{\"write_enabled\":true,\"backfill_complete\":true}') ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value");await c.commit()
    manifest=units.documents.read('demo',submission['manifest_ref'])['payload'];source=manifest['purpose']['authorization_ref']
    task=await repo.get_run(manifest['ownership']['creating_task_ref']['id']);task['request_json']['current_stage']='operate'
    policy={'policy_id':'failures','target_refs':[],'collector_manifest_refs':[],'schedule_refs':[],
        'notification_rules':[{'rule_id':'two','metric_id':'execution_failure','window_days':7,'min_distinct_events':2,'severity':'warning','first_match_only':True,'quiet_if_unchanged':True}],
        'route':{'primary_role':'supervisor','fallback_role':'pm','notification_channel':'discord','persist_when_unroutable':True,'same_project':True},
        'automatic_llm_calls':False,'automatic_analysis_queue':False,'no_new_evidence':'quiet','authorization_source_refs':[source],
        'source_scope_refs':[],'requested_activation':True,'notification_scope':['python_execution']}
    ref=await records.write(task,'operation-monitor-policy',policy)
    async def event(identity):
        async with repo.database.connection() as c:
            await records.record_event_locked(c,project='demo',event_key=identity,scene='python_execution',source_ref=submission['manifest_ref'],
                target_ref=submission['manifest_ref'],occurred_at=datetime.now(timezone.utc).isoformat(),summary='Synthetic isolated failure event');await c.commit()
    await event('A')
    with pytest.raises(PermissionError,match='actual approval'):await records.activate_policy('demo',ref,[source],authenticated_operator=True)
    original=await repo.get_message(source['id'])
    async def approve(target):
        recorded=await repo.record_channel_command(project_id='demo',agent_key=task['agent_key'],channel=original['channel'],chat_id=original['chat_id'],
            user_id=original['user_id'],external_event_id='fixture-approval-'+uuid.uuid4().hex,external_message_id=None,
            content={'text':'Synthetic approval fixture; not user acceptance','document_approvals':[{'decision':'approve','document_ref':target}]})
        message=recorded['message'];return resource_ref(kind='message',identity=str(message['message_id']),uri='db://assistant/messages/'+str(message['message_id']),purpose='synthetic exact policy approval')
    approval=await approve(ref);await records.activate_policy('demo',ref,[approval],authenticated_operator=True)
    await event('A')
    async with repo.database.connection() as c:assert not await (await c.execute("SELECT 1 FROM assistant.messages WHERE content_json->>'interaction_kind'='operation_notice'")).fetchone()
    await event('B');await event('B')
    newer=deepcopy(policy);newer['notification_rules'][0]['severity']='high';next_ref=await records.write(task,'operation-monitor-policy',newer)
    async with repo.database.connection() as c:
        await records.index_locked(c,'demo','operation-monitor-policy',next_ref);await c.commit()
        active=await (await c.execute("SELECT value FROM assistant.kernel_meta WHERE key LIKE 'operation-record:demo:policies:%%'")).fetchone()
        assert active['value']['active_ref']==ref
    await records.activate_policy('demo',next_ref,[await approve(next_ref)],authenticated_operator=True);await event('C')
    async with repo.database.connection() as c:
        notices=await (await c.execute("SELECT * FROM assistant.messages WHERE content_json->>'interaction_kind'='operation_notice'")).fetchall()
        assert len(notices)==1 and notices[0]['channel']=='internal'
        assert not await records.delivery_current(c,notices[0]) # Its old policy was superseded before delivery.

async def test_dynamic_configuration_consumes_actual_fields_and_does_not_claim_restart(core_settings):
    from test_v12_p4_admission import config
    await config(core_settings)
    consumers=ConfigConsumers(core_settings);state=await consumers.service.state()
    cfg=consumers.service._config(state['desired_ref']);await consumers.apply_dynamic(core_settings,'isolated-worker')
    assert core_settings.worker_poll_seconds==cfg['kernel']['queue']['poll_seconds']
    state=await consumers.service.state()
    dynamic=next(x for x in state['groups'] if x['group']=='kernel:dynamic_admission')
    restart=next(x for x in state['groups'] if x['group']=='kernel:pending_restart')
    assert dynamic['status']=='applied' and restart['status']=='pending_restart'
    scheduling=next(x for x in state['groups'] if x['group']=='kernel/scheduler:dynamic_admission')
    assert scheduling['status']=='pending_boundary'
    await consumers.record('scheduler',state['desired_ref'],{'/kernel/scheduler/'+k:v for k,v in cfg['kernel']['scheduler'].items()})
    assert next(x for x in (await consumers.service.state())['groups'] if x['group']=='kernel/scheduler:dynamic_admission')['status']=='applied'
    with pytest.raises(ValueError,match='differ'):await consumers.record('wrong',state['desired_ref'],{'/kernel/queue/poll_seconds':999})

async def test_runtime_skill_is_selected_from_fixed_bundle_only_for_structured_work(core_settings):
    from test_v12_p3_phase_control import baseline
    from assistant_kernel.services.published_agents import PublishedAgentService
    repo,_,task,_,_=await baseline(core_settings,descriptive_runtime=True)
    service=PublishedAgentService(core_settings)
    with service.connection() as c:ref=c.execute('SELECT value FROM assistant.kernel_meta WHERE key=%s',(service.key('demo'),)).fetchone()['value']['package_ref']
    package=service.read_package('demo',ref);config_ref=next(value for key,value in package['configurations'].items() if key.startswith('project:demo:manager/'))
    config=service.loader.load_snapshot('demo',config_ref['uri'],config_ref['sha256'])
    derived,sources=service.runtime_skills('demo',config,package,task)
    assert sources and 'assistant-business-runtime-design' in {x['name'] for x in derived.skill_bindings}
    assert 'assistant-service-lifecycle' not in {x['name'] for x in derived.skill_bindings}
    empty={**task,'request_json':{**task['request_json'],'accepted_documents':{}}}
    assert service.runtime_skills('demo',config,package,empty)==(config,[])
