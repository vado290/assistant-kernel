"""Real isolated PostgreSQL scheduler tests; all authority fixtures are synthetic."""
import asyncio
from datetime import datetime,timedelta,timezone
from pathlib import Path
import uuid
import pytest
from psycopg.types.json import Jsonb

from test_core4 import core_settings
from test_v12_p4_units import unit
from assistant_kernel.services.schedule_time import next_time,preview
from assistant_kernel.services.schedules import Schedules,DEFAULTS
from assistant_kernel.services.scheduler import Scheduler
from assistant_kernel.worker.main import execute_python

UTC=timezone.utc


def test_interval_anchor_and_once_are_exact():
    anchor='2026-10-13T09:00:00+08:00';rule={'kind':'interval','anchor':anchor,'seconds':7}
    assert next_time(rule,'Asia/Shanghai','2026-10-13T01:00:08Z').isoformat()=='2026-10-13T01:00:14+00:00'
    assert next_time({'kind':'once','at':anchor},'Asia/Shanghai','2026-10-13T01:00:00Z') is None
    with pytest.raises(ValueError,match='offset'):next_time({'kind':'once','at':'2026-10-13T09:00:00'},'Asia/Shanghai',datetime.now(UTC))


@pytest.mark.parametrize('expression,after,expected',[
    ('0 9 L * *','2026-02-01T00:00:00Z','2026-02-28T01:00:00+00:00'),
    ('0 9 29 2 *','2026-03-01T00:00:00Z','2028-02-29T01:00:00+00:00'),
    ('0 9 * * mon','2026-10-13T00:00:00Z','2026-10-19T01:00:00+00:00')])
def test_calendar_dates(expression,after,expected):
    assert next_time({'kind':'cron','expression':expression},'Asia/Shanghai',after).isoformat()==expected


def test_dst_gap_skipped_and_fold_only_first():
    rule={'kind':'cron','expression':'30 2 * * *'}
    assert next_time(rule,'America/New_York','2026-03-08T05:00:00Z').isoformat()=='2026-03-09T06:30:00+00:00'
    fold={'kind':'cron','expression':'30 1 * * *'}
    assert next_time(fold,'America/New_York','2026-11-01T04:00:00Z').isoformat()=='2026-11-01T05:30:00+00:00'
    assert next_time(fold,'America/New_York','2026-11-01T05:31:00Z').isoformat()=='2026-11-02T06:30:00+00:00'


@pytest.mark.parametrize('rule',[{'kind':'interval','anchor':'2026-01-01T00:00:00Z','seconds':0},
    {'kind':'cron','expression':'0 0 31 2 *'},{'kind':'cron','expression':'* * * * * *'},
    {'kind':'once','at':'2000-01-01T00:00:00Z'}])
def test_invalid_rules(rule):
    with pytest.raises(ValueError):preview(rule,after='2026-10-13T00:00:00Z')


async def fixture(settings,*,seconds=10,once=False):
    repo,units,registered,command,submission=await unit(settings)
    async with repo.database.connection() as c:
        for name in ('upgrade_core5_v10.sql','upgrade_core5_v11.sql'):
            await c.execute((Path(__file__).parents[1]/'migrations'/name).read_text())
        await c.execute("INSERT INTO assistant.kernel_meta(key,value) VALUES('project-file-ledger:v2','{\"write_enabled\":true,\"backfill_complete\":true}') ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value")
        await c.commit()
    await units.control_finite('demo',command,authenticated_operator=True)
    definition=units.read_definition('demo',command['runtime_unit_ref']);task_id=units.documents.read('demo',definition['design_ref'])['task_id']
    source=command['authorization_source_refs'][0];task=await repo.get_run(task_id)
    async with repo.database.connection() as c:
        await c.execute("UPDATE assistant.runs SET status='running',request_json=request_json || %s WHERE run_id=%s",(Jsonb({
            'current_stage':'operate','confirmed_turn_package_ref':definition['design_ref'],'prompt_confirmation_message_id':source['id']}),task_id));await c.commit()
    due=datetime.now(UTC)+timedelta(seconds=30)
    value={'project_id':'demo','schedule_id':'clock-test','name':'Isolated fixture','runtime_unit_ref':command['runtime_unit_ref'],
        'action':'run','inputs':{'n':42},'rule':{'kind':'once','at':due.isoformat()} if once else {'kind':'interval','anchor':due.isoformat(),'seconds':seconds},
        'timezone':'Asia/Shanghai','misfire_policy':'skip','overlap_policy':'skip','notification_policy':'every_occurrence','source_refs':[source]}
    ref=units.documents.write_scoped_revision(project_id='demo',scope='project',object_id='schedule-test',document_kind='kernel-schedule-definition',
        document_schema='kernel-schedule-definition.v1',payload=value,status='submitted',actor_type='kernel',actor_id='isolated-fixture',
        reason='Synthetic isolated fixture, not real user authorization',source_refs=[source])
    schedules=Schedules(settings,repo)
    row=await schedules.register('demo',ref,task_id=task_id,authorization_source_refs=[source],idempotency_key='register')
    control={'schedule_id':'clock-test','action':'enable','expected_control_revision':0,'idempotency_key':'enable',
        'authorization_source_refs':[source],'target_definition_ref':None}
    await schedules.control('demo',control,task_id=task_id)
    return repo,schedules,Scheduler(settings,repo),task_id,control,due


async def scan(repo,scheduler,now):
    async with repo.database.connection() as c:
        assert await scheduler.acquire(c)
        await scheduler.scan(c,now=now,configuration={**DEFAULTS,'enabled':True})


async def test_register_dispatch_once_real_execute_settle_and_notify(core_settings):
    repo,schedules,scheduler,task,control,due=await fixture(core_settings,once=True)
    await scan(repo,scheduler,due);await scan(repo,scheduler,due)
    view=await schedules.status('demo','clock-test');assert view['registration']['state']=='exhausted'
    assert len(view['recent_ticks'])==1 and view['recent_ticks'][0]['settlement_status']=='pending'
    owner=str(uuid.uuid4());run=await repo.claim_run('schedule-test',owner,90,queue_kind='runtime')
    assert run['request_json']['schedule_dispatch_permit']['tick_id']==view['recent_ticks'][0]['tick_id']
    await execute_python(run,core_settings,repo,owner,asyncio.Event())
    await scan(repo,scheduler,due+timedelta(seconds=1));await scan(repo,scheduler,due+timedelta(seconds=2))
    final=(await schedules.status('demo','clock-test'))['recent_ticks'][0]
    assert final['settlement_status']=='succeeded' and final['notification_id']
    async with repo.database.connection() as c:
        assert (await (await c.execute("SELECT count(*) n FROM assistant.messages WHERE idempotency_key LIKE 'schedule-result:%%'")).fetchone())['n']==1


async def test_overlap_pause_and_stale_control(core_settings):
    repo,schedules,scheduler,task,control,due=await fixture(core_settings)
    await scan(repo,scheduler,due);await scan(repo,scheduler,due+timedelta(seconds=10))
    ticks=(await schedules.ticks('demo','clock-test'))['items']
    assert sorted(x['disposition'] for x in ticks)==['skipped','submitted']
    assert any(x['reason']=='previous_occurrence_unsettled' for x in ticks)
    pause={**control,'action':'pause','expected_control_revision':1,'idempotency_key':'pause'}
    receipt=await schedules.control('demo',pause,task_id=task)
    assert receipt==await schedules.control('demo',pause,task_id=task)
    assert await repo.claim_run('schedule-test',str(uuid.uuid4()),90,queue_kind='runtime') is None
    with pytest.raises(PermissionError,match='stale'):await schedules.control('demo',{**control,'idempotency_key':'stale'},task_id=task)


async def test_claimed_occurrence_drains_after_pause(core_settings):
    repo,schedules,scheduler,task,control,due=await fixture(core_settings)
    await scan(repo,scheduler,due)
    owner=str(uuid.uuid4());run=await repo.claim_run('schedule-test',owner,90,queue_kind='runtime')
    await schedules.control('demo',{**control,'action':'pause','expected_control_revision':1,'idempotency_key':'pause'},task_id=task)
    assert (await repo.get_run(str(run['run_id'])))['cancel_requested_at'] is None
    await execute_python(run,core_settings,repo,owner,asyncio.Event())
    assert (await repo.get_run(str(run['run_id'])))['status']=='succeeded'


async def test_restart_skips_all_missed_and_preserves_future(core_settings):
    repo,schedules,scheduler,task,control,due=await fixture(core_settings)
    cutoff=due+timedelta(seconds=25)
    async with repo.database.connection() as c:
        assert await scheduler.acquire(c)
        await scheduler.recover(c,cutoff)
        await scheduler.scan(c,now=cutoff,configuration={**DEFAULTS,'enabled':True})
    status=await schedules.status('demo','clock-test')
    assert status['registration']['next_fire_at']==(due+timedelta(seconds=30)).isoformat()
    assert len(status['recent_ticks'])==3 and all(x['reason']=='missed_time' and not x['run_ref'] for x in status['recent_ticks'])
    await scan(repo,scheduler,due+timedelta(seconds=30))
    assert any(x['run_ref'] for x in (await schedules.ticks('demo','clock-test'))['items'])


async def test_session_lock_and_run_creation_rollback(core_settings,monkeypatch):
    repo,schedules,scheduler,task,control,due=await fixture(core_settings)
    async with repo.database.connection() as a,repo.database.connection() as b:
        assert await scheduler.acquire(a)
        assert not await scheduler.acquire(b)
        original=repo.create_run_locked
        async def fail(*args,**kwargs):
            await original(*args,**kwargs)
            raise RuntimeError('crash after Run insert')
        monkeypatch.setattr(repo,'create_run_locked',fail)
        with pytest.raises(RuntimeError,match='crash'):
            await scheduler.scan(a,now=due,configuration={**DEFAULTS,'enabled':True})
    async with repo.database.connection() as c:
        assert (await (await c.execute('SELECT count(*) n FROM assistant.schedule_ticks')).fetchone())['n']==0
        assert (await (await c.execute("SELECT count(*) n FROM assistant.runs WHERE request_json ? 'schedule_binding'")).fetchone())['n']==0
    assert (await schedules.status('demo','clock-test'))['registration']['next_fire_at']==due.isoformat()


async def test_manual_run_does_not_enable_paused_or_move_clock(core_settings):
    repo,schedules,scheduler,task,control,due=await fixture(core_settings)
    await schedules.control('demo',{**control,'action':'pause','expected_control_revision':1,'idempotency_key':'pause'},task_id=task)
    run_now={**control,'action':'run_now','expected_control_revision':2,'idempotency_key':'manual'}
    receipt=await schedules.control('demo',run_now,task_id=task)
    assert receipt==await schedules.control('demo',run_now,task_id=task)
    status=await schedules.status('demo','clock-test')
    assert status['registration']['state']=='paused' and status['registration']['next_fire_at'] is None
    assert len(status['recent_ticks'])==1
    assert await repo.claim_run('manual-test',str(uuid.uuid4()),90,queue_kind='runtime')


async def test_scope_validation_and_manual_unit_pause_propagation(core_settings):
    repo,schedules,scheduler,task,control,due=await fixture(core_settings)
    with pytest.raises(PermissionError):await schedules.control('unrelated',control,task_id=task)
    with pytest.raises((PermissionError,ValueError)):await schedules.control('demo',{**control,'authorization_source_refs':[]},task_id=task)
    state=await schedules.status('demo','clock-test');unit_ref=state['definition']['runtime_unit_ref']
    await schedules.units.control_finite('demo',{'runtime_unit_ref':unit_ref,'action':'pause','expected_control_revision':1,
        'idempotency_key':'manual-unit-pause','authorization_source_refs':control['authorization_source_refs'],'target_definition_ref':None,
        'stop_policy':'drain','cause':'manual','tick_ref':None,'expected_instance_ref':None,'expires_at':None},authenticated_operator=True)
    assert (await schedules.status('demo','clock-test'))['registration']['state']=='paused'


async def test_service_busy_skip_and_manual_override(core_settings):
    from test_v12_p5_services import service_fixture
    from assistant_kernel.services.service_work import ServiceWork
    repo,service,instance,command=await service_fixture(core_settings)
    async with repo.database.connection() as c:
        await c.execute((Path(__file__).parents[1]/'migrations/upgrade_core5_v11.sql').read_text());await c.commit()
    unit_definition=service.units.read_definition('demo',command['runtime_unit_ref'])
    task=service.documents.read('demo',unit_definition['design_ref'])['task_id'];source=command['authorization_source_refs'][0]
    async with repo.database.connection() as c:
        await c.execute("UPDATE assistant.runs SET status='running',request_json=request_json || %s WHERE run_id=%s",
            (Jsonb({'current_stage':'operate','confirmed_turn_package_ref':unit_definition['design_ref'],'prompt_confirmation_message_id':source['id']}),task));await c.commit()
    due=datetime.now(UTC)+timedelta(seconds=20)
    definition={'project_id':'demo','schedule_id':'service-clock','name':'Fixture maintenance','runtime_unit_ref':command['runtime_unit_ref'],
        'action':'restart','inputs':{},'rule':{'kind':'interval','anchor':due.isoformat(),'seconds':10},'timezone':'Asia/Shanghai',
        'misfire_policy':'skip','overlap_policy':'skip','notification_policy':'every_occurrence','source_refs':[source]}
    ref=service.documents.write_scoped_revision(project_id='demo',scope='project',object_id='service-schedule',document_kind='kernel-schedule-definition',
        document_schema='kernel-schedule-definition.v1',payload=definition,status='submitted',actor_type='kernel',actor_id='fixture',reason='Isolated synthetic authority',source_refs=[source])
    schedules=Schedules(core_settings,repo);scheduler=Scheduler(core_settings,repo)
    await schedules.register('demo',ref,task_id=task,authorization_source_refs=[source],idempotency_key='register')
    await schedules.control('demo',{'schedule_id':'service-clock','action':'enable','expected_control_revision':0,'idempotency_key':'enable',
        'authorization_source_refs':[source],'target_definition_ref':None},task_id=task)
    await ServiceWork(core_settings,repo).action(instance,'work-begin',{'logical_execution_id':'busy','input':{'n':1}})
    await scan(repo,scheduler,due)
    assert (await schedules.ticks('demo','service-clock'))['items'][0]['reason']=='service_busy_or_waiting'
    await ServiceWork(core_settings,repo).action(instance,'work-end',{'logical_execution_id':'busy','result':{'answer':1}})
    await scan(repo,scheduler,due+timedelta(seconds=10))
    assert (await schedules.ticks('demo','service-clock'))['items'][0]['settlement_status']=='pending'
    with pytest.raises(PermissionError,match='manual Kernel authority'):
        await service.control('demo',{**command,'cause':'scheduled'},authenticated_operator=True)
    await service.control('demo',{**command,'action':'stop','expected_control_revision':2,'idempotency_key':'manual-stop'},authenticated_operator=True)
    assert (await schedules.status('demo','service-clock'))['registration']['state']=='paused'
    ticks=(await schedules.ticks('demo','service-clock'))['items']
    assert ticks[0]['reason']=='superseded_by_manual_control'
    async with repo.database.connection() as c:
        current=await (await c.execute('SELECT * FROM assistant.project_runtime_units')).fetchone()
    await service.settle_control(current,current['control_json']['pending_control'],await service.get(instance['instance_id']),status='failed')
    await scan(repo,scheduler,due+timedelta(seconds=11))
    assert (await schedules.ticks('demo','service-clock'))['items'][0]['settlement_status']=='pending'
    async with repo.database.connection() as c:
        await c.execute("UPDATE assistant.service_instances SET cleanup_status='verified' WHERE instance_id=%s",(instance['instance_id'],));await c.commit()
    await scan(repo,scheduler,due+timedelta(seconds=12))
    assert (await schedules.ticks('demo','service-clock'))['items'][0]['settlement_status']=='failed'


async def test_actual_activation_rejects_generated_only_and_manual_substitute(core_settings):
    from assistant_kernel.services.schedule_delivery import verify_activation_locked
    repo,schedules,scheduler,task,control,due=await fixture(core_settings)
    status=await schedules.status('demo','clock-test');definition=status['definition'];ref=status['registration']['definition_ref']
    plan={k:definition[k] for k in ('schedule_id','action','inputs','rule','timezone')};plan.update(runtime_unit_id='formal-test',acceptance_ids=['A2'],requires_first_run=True)
    implementation={'schedule_handoffs':[ref]};design={'schedule_plans':[plan]}
    activation={'action_receipt_refs':[status['registration']['last_control_receipt_ref']]}
    async with repo.database.connection() as c:
        with pytest.raises(PermissionError,match='actual successful timed'):await verify_activation_locked(c,core_settings,repo,'demo',design,implementation,activation)
    await scan(repo,scheduler,due);owner=str(uuid.uuid4());run=await repo.claim_run('schedule-test',owner,90,queue_kind='runtime')
    await execute_python(run,core_settings,repo,owner,asyncio.Event());await scan(repo,scheduler,due+timedelta(seconds=1))
    from assistant_kernel.services.schedules import tick_ref
    activation['action_receipt_refs'].append(tick_ref((await schedules.ticks('demo','clock-test'))['items'][0]['tick_id']))
    async with repo.database.connection() as c:await verify_activation_locked(c,core_settings,repo,'demo',design,implementation,activation)


async def test_migration_idempotence_and_rollback_fences(core_settings):
    from scripts.migrate_core5_v11 import migrate
    repo,schedules,scheduler,task,control,due=await fixture(core_settings)
    assert (await migrate(core_settings,apply=True))['target_schema']=='core5.v11'
    with pytest.raises(PermissionError,match='Disable schedules'):await migrate(core_settings,rollback=True)
    await schedules.control('demo',{**control,'action':'retire','expected_control_revision':1,'idempotency_key':'retire'},task_id=task)
    assert (await migrate(core_settings,apply=True,rollback=True))['ledger_retained']
    async with repo.database.connection() as c:
        assert (await (await c.execute('SELECT count(*) n FROM assistant.project_schedules')).fetchone())['n']==1


async def test_prior_0302_document_stays_readable(core_settings,monkeypatch):
    import os
    from assistant_kernel.services.contracts import ContractRegistry
    from assistant_kernel.services.document_runtime import TaskDocumentService
    from test_v12_foundations import source
    old_path=os.getenv('SCHEDULER_OLD_CONTRACT_ROOT')
    if not old_path:pytest.skip('Actual sealed 0.30.2 contract reader required')
    old=ContractRegistry(Path(old_path));assert old.implementation_revision==8
    import json
    payload=json.loads((old.root/'kernel-system.example.json').read_text());assert 'scheduler' not in payload['kernel']
    documents=TaskDocumentService(core_settings)
    with monkeypatch.context() as patch:
        patch.setattr('assistant_kernel.services.contracts.contracts',lambda:old)
        ref=documents.write_scoped_revision(project_id='kernel',scope='project',object_id='system-config',document_kind='kernel-system-config',
            document_schema='kernel-system-config.v1',payload=payload,status='submitted',actor_type='kernel',actor_id='fixture',reason='Actual 0.30.2 compatibility fixture',source_refs=[source()])
    assert documents.read('kernel',ref)['payload']==payload


async def test_waiting_work_unknown_cleanup_and_manual_cancel(core_settings):
    repo,schedules,scheduler,task,control,due=await fixture(core_settings)
    await scan(repo,scheduler,due);run=(await schedules.ticks('demo','clock-test'))['items'][0]['run_ref']['id']
    async with repo.database.connection() as c:
        await c.execute("UPDATE assistant.runs SET status='waiting_user' WHERE run_id=%s",(run,));await c.commit()
    await scan(repo,scheduler,due+timedelta(seconds=10))
    assert (await schedules.ticks('demo','clock-test'))['items'][0]['reason']=='previous_occurrence_unsettled'
    await schedules.control('demo',{**control,'action':'cancel_current','expected_control_revision':1,'idempotency_key':'cancel'},task_id=task)
    await scan(repo,scheduler,due+timedelta(seconds=11))
    ticks=(await schedules.ticks('demo','clock-test'))['items'];assert any(x['settlement_status']=='cancelled' for x in ticks)


async def test_committed_once_command_replays_after_preview_expires(core_settings,monkeypatch):
    repo,schedules,scheduler,task,control,due=await fixture(core_settings,once=True)
    status=await schedules.status('demo','clock-test')
    expected=schedules.documents.read('demo',status['registration']['last_control_receipt_ref'])['payload']
    def expired(*args,**kwargs):raise ValueError('Once time has passed')
    monkeypatch.setattr('assistant_kernel.services.schedules.preview',expired)
    assert await schedules.control('demo',control,task_id=task)==expected
    assert (await schedules.register('demo',status['registration']['definition_ref'],task_id=task,
        authorization_source_refs=control['authorization_source_refs'],idempotency_key='register'))['state']=='enabled'
