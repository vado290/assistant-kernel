"""Regression cases from the 0.31.1 audit. All authority is isolated synthetic test data."""
import uuid
from copy import deepcopy
from datetime import datetime,timedelta,timezone
from pathlib import Path
import pytest
from psycopg.types.json import Jsonb
from test_core4 import core_settings
from test_v12_scheduler import fixture,scan
from test_v12_p5_services import service_fixture
from assistant_kernel.services.schedules import Schedules
from assistant_kernel.services.scheduler import Scheduler
from assistant_kernel.services.schedule_time import preview


@pytest.mark.parametrize('expression',['R * * * *','r(1-10) * * * *','0 R * * *'])
def test_random_cron_is_rejected(expression):
    with pytest.raises(ValueError):preview({'kind':'cron','expression':expression},after='2026-10-13T00:00:00Z')


def test_named_month_weekday_stay_deterministic():
    rule={'kind':'cron','expression':'0 9 * MAR FRI'}
    assert preview(rule,after='2026-10-13T00:00:00Z')==preview(rule,after='2026-10-13T00:00:00Z')


@pytest.mark.parametrize('change',['updated','retired','definition_missing'])
async def test_old_schedule_can_cancel_pause_retire_after_target_changes(core_settings,change):
    repo,schedules,scheduler,task,control,due=await fixture(core_settings)
    await scan(repo,scheduler,due)
    view=await schedules.status('demo','clock-test');ref=view['definition']['runtime_unit_ref']
    async with repo.database.connection() as c:
        if change=='updated':
            await c.execute("UPDATE assistant.project_runtime_units SET effective_definition_ref=%s",(Jsonb({**ref,'revision_id':'synthetic-next-version'}),))
        elif change=='retired':await c.execute("UPDATE assistant.project_runtime_units SET desired_state='retired'")
        await c.commit()
    if change=='definition_missing':
        path=schedules.documents.runtime.resolve_vault_file('demo',view['registration']['definition_ref']['uri'].removeprefix('vault://demo/'))
        path.rename(path.with_suffix('.unavailable'))
    await schedules.control('demo',{**control,'action':'cancel_current','expected_control_revision':1,'idempotency_key':'cancel'},task_id=task)
    await schedules.control('demo',{**control,'action':'pause','expected_control_revision':1,'idempotency_key':'pause'},task_id=task)
    await schedules.control('demo',{**control,'action':'retire','expected_control_revision':2,'idempotency_key':'retire'},task_id=task)
    async with repo.database.connection() as c:
        row=await(await c.execute('SELECT state FROM assistant.project_schedules')).fetchone()
        run=await(await c.execute("SELECT status FROM assistant.runs WHERE request_json ? 'schedule_binding'")).fetchone()
    assert row['state']=='retired' and run['status']=='cancelled'
    await scan(repo,scheduler,due+timedelta(seconds=1))
    settled=(await schedules.ticks('demo','clock-test'))['items'][0]
    assert settled['settlement_status']=='cancelled' and settled['notification_id']


async def service_clock(settings):
    repo,service,instance,command=await service_fixture(settings)
    async with repo.database.connection() as c:
        await c.execute((Path(__file__).parents[1]/'migrations/upgrade_core5_v11.sql').read_text());await c.commit()
    unit=service.units.read_definition('demo',command['runtime_unit_ref'])
    task=service.documents.read('demo',unit['design_ref'])['task_id'];source=command['authorization_source_refs'][0]
    async with repo.database.connection() as c:
        await c.execute("UPDATE assistant.runs SET status='running',request_json=request_json || %s WHERE run_id=%s",
            (Jsonb({'current_stage':'operate','confirmed_turn_package_ref':unit['design_ref'],'prompt_confirmation_message_id':source['id']}),task));await c.commit()
    due=datetime.now(timezone.utc)+timedelta(seconds=20)
    body={'project_id':'demo','schedule_id':'service-clock','name':'Synthetic pause boundary','runtime_unit_ref':command['runtime_unit_ref'],
        'action':'restart','inputs':{},'rule':{'kind':'interval','anchor':due.isoformat(),'seconds':10},'timezone':'Asia/Shanghai',
        'misfire_policy':'skip','overlap_policy':'skip','notification_policy':'every_occurrence','source_refs':[source]}
    ref=service.documents.write_scoped_revision(project_id='demo',scope='project',object_id='schedule-test',document_kind='kernel-schedule-definition',
        document_schema='kernel-schedule-definition.v1',payload=body,status='submitted',actor_type='kernel',actor_id='isolated-fixture',reason='Synthetic test authority',source_refs=[source])
    schedules=Schedules(settings,repo);scheduler=Scheduler(settings,repo)
    await schedules.register('demo',ref,task_id=task,authorization_source_refs=[source],idempotency_key='register')
    control={'schedule_id':'service-clock','action':'enable','expected_control_revision':0,'idempotency_key':'enable','authorization_source_refs':[source],'target_definition_ref':None}
    await schedules.control('demo',control,task_id=task)
    await scan(repo,scheduler,due)
    async with repo.database.connection() as c:row=await(await c.execute('SELECT * FROM assistant.project_runtime_units')).fetchone()
    return repo,service,schedules,scheduler,task,control,due,row


async def test_pause_wins_service_claim_and_notification_settles_once(core_settings):
    repo,service,schedules,scheduler,task,control,due,row=await service_clock(core_settings)
    await schedules.control('demo',{**control,'action':'pause','expected_control_revision':1,'idempotency_key':'pause'},task_id=task)
    assert await service.claim_scheduled(row) is None
    await scan(repo,scheduler,due+timedelta(seconds=1));await scan(repo,scheduler,due+timedelta(seconds=2))
    tick=(await schedules.ticks('demo','service-clock'))['items'][0]
    assert tick['settlement_status']=='cancelled' and tick['started_at'] is None and tick['notification_id']
    async with repo.database.connection() as c:
        actual=await(await c.execute('SELECT * FROM assistant.project_runtime_units')).fetchone()
        assert 'pending_control' not in actual['control_json'] and actual['desired_state']=='running'
        assert (await(await c.execute("SELECT count(*) n FROM assistant.messages WHERE idempotency_key LIKE 'schedule-result:%%'")).fetchone())['n']==1


async def test_service_claim_wins_pause_and_retains_start_time(core_settings):
    repo,service,schedules,scheduler,task,control,due,row=await service_clock(core_settings)
    permitted=await service.claim_scheduled(row)
    assert permitted['control_json']['commands'][row['control_json']['pending_control']['idempotency_key']]['schedule_started_at']
    await schedules.control('demo',{**control,'action':'pause','expected_control_revision':1,'idempotency_key':'pause'},task_id=task)
    assert await service.claim_scheduled(permitted) is not None
    assert (await schedules.ticks('demo','service-clock'))['items'][0]['started_at']


async def test_cancel_current_service_keeps_future_rule_enabled(core_settings):
    repo,service,schedules,scheduler,task,control,due,row=await service_clock(core_settings)
    await schedules.control('demo',{**control,'action':'cancel_current','expected_control_revision':1,'idempotency_key':'cancel'},task_id=task)
    view=await schedules.status('demo','service-clock')
    assert view['registration']['state']=='enabled' and view['registration']['next_fire_at']
    assert view['recent_ticks'][0]['reason']=='cancel_requested'


async def test_notification_waits_for_route_without_replaying_business(core_settings):
    repo,schedules,scheduler,task,control,due=await fixture(core_settings)
    await scan(repo,scheduler,due)
    await schedules.control('demo',{**control,'action':'cancel_current','expected_control_revision':1,'idempotency_key':'cancel'},task_id=task)
    await scan(repo,scheduler,due+timedelta(seconds=1))
    tick=(await schedules.ticks('demo','clock-test'))['items'][0]
    before=await repo.get_message(tick['notification_id']);assert before['channel']=='internal' and before['delivery_status']=='pending'
    async with repo.database.connection() as c:
        await c.execute("INSERT INTO assistant.messages(message_id,project_id,channel,chat_id,user_id,direction,message_type,content_json,delivery_status) VALUES(%s,'demo','discord','synthetic-channel','synthetic-user','system','route_binding',%s,'received')",
            (uuid.uuid4(),Jsonb({'status':'active','agent_key':'project:demo:manager'})));await c.commit()
    from assistant_kernel.services.runtime_notifications import RuntimeNotifications
    await RuntimeNotifications(core_settings,repo).route_pending();await scan(repo,scheduler,due+timedelta(seconds=2))
    after=await repo.get_message(tick['notification_id']);assert after['channel']=='discord' and after['chat_id']=='synthetic-channel'
    assert (await schedules.ticks('demo','clock-test'))['items'][0]['notification_id']==tick['notification_id']
    async with repo.database.connection() as c:
        assert (await(await c.execute("SELECT count(*) n FROM assistant.runs WHERE request_json ? 'schedule_binding'")).fetchone())['n']==1


async def test_rollback_checks_actual_unit_reader_and_pending_work(core_settings):
    from scripts.deploy_scheduler_release import rollback_preflight
    repo,schedules,scheduler,task,control,due=await fixture(core_settings)
    with pytest.raises(PermissionError):await rollback_preflight(core_settings)
    await schedules.control('demo',{**control,'action':'pause','expected_control_revision':1,'idempotency_key':'pause'},task_id=task)
    assert (await rollback_preflight(core_settings))['legacy_readable_units']==1
    async with repo.database.connection() as c:
        await c.execute("UPDATE assistant.runs SET request_json=request_json || %s WHERE run_id=%s",(Jsonb({'schedule_actions':[{'synthetic':True}]}),task));await c.commit()
    with pytest.raises(PermissionError):await rollback_preflight(core_settings)
    async with repo.database.connection() as c:
        await c.execute("UPDATE assistant.runs SET request_json=request_json - 'schedule_actions' WHERE run_id=%s",(task,))
        await c.execute('DROP TABLE assistant.schedule_ticks')
        await c.execute('DROP TABLE assistant.project_schedules');await c.commit()
    assert (await rollback_preflight(core_settings))['legacy_readable_units']==1
