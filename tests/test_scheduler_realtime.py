"""Real clocks and a separate Supervisor instance. No production DB or user impersonation."""
import asyncio,json,os
from datetime import datetime,timedelta,timezone
import pytest
from test_core4 import core_settings
from test_v12_scheduler import fixture,scan
from test_v12_p5_services import service_fixture
from assistant_kernel.services.scheduler import Scheduler
from assistant_kernel.services.schedules import Schedules
from assistant_kernel.services.system_config import SystemConfigService
from assistant_kernel.worker.scheduler_main import _run
from assistant_kernel.worker.main import execute_python


async def test_actual_scheduler_loop_dispatches_and_records_config_consumption(core_settings):
    repo,schedules,scheduler,task,control,due=await fixture(core_settings,once=True)
    config=SystemConfigService(core_settings);state=await config.state();value=config._config(state['desired_ref'])
    value['kernel']['scheduler']['enabled']=True
    await config.accept(candidate=value,expected_revision=state['revision'],reason='Synthetic isolated real-clock test',
        source_kind='external_file',source_refs=control['authorization_source_refs'],write_input=True)
    stop=asyncio.Event();timer=asyncio.create_task(_run(core_settings,stop))
    try:
        import uuid
        owner=str(uuid.uuid4());executed=False
        async def complete():
            nonlocal executed
            while True:
                if not executed:
                    run=await repo.claim_run('real-clock-test',owner,90,queue_kind='runtime')
                    if run:
                        await execute_python(run,core_settings,repo,owner,asyncio.Event());executed=True
                ticks=(await schedules.ticks('demo','clock-test'))['items']
                if ticks and ticks[0]['settlement_status']=='succeeded':return ticks[0]
                await asyncio.sleep(.2)
        tick=await asyncio.wait_for(complete(),45)
        assert tick['notification_id'] and datetime.fromisoformat(tick['admitted_at'])>=due
        assert next(g for g in (await config.state())['groups'] if g['group']=='kernel/scheduler:dynamic_admission')['status']=='applied'
    finally:
        stop.set();await asyncio.wait_for(timer,10)


async def test_real_service_start_scheduled_restart_and_stop(core_settings,monkeypatch):
    from pathlib import Path
    from psycopg.types.json import Jsonb
    config=core_settings.data_root/'supervisord.conf';sock=core_settings.data_root/'s.sock'
    programs=core_settings.data_root/'runtime/kernel-services/programs';programs.mkdir(parents=True)
    config.write_text(f'''[supervisord]
nodaemon=true
logfile={core_settings.data_root}/supervisor.log
pidfile={core_settings.data_root}/supervisor.pid
[unix_http_server]
file={sock}
chmod=0700
[rpcinterface:supervisor]
supervisor.rpcinterface_factory=supervisor.rpcinterface:make_main_rpcinterface
[supervisorctl]
serverurl=unix://{sock}
[include]
files={programs}/*.conf
''')
    monkeypatch.setenv('KERNEL_SUPERVISOR_CONFIG',str(config))
    manager=await asyncio.create_subprocess_exec('/usr/bin/supervisord','-c',str(config),stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL)
    service=None;repo=None
    try:
        for _ in range(50):
            if sock.exists():break
            await asyncio.sleep(.1)
        assert sock.exists()
        repo,service,instance,command=await service_fixture(core_settings,real_process=True)
        async with repo.database.connection() as c:
            await c.execute((Path(__file__).parents[1]/'migrations/upgrade_core5_v11.sql').read_text());await c.commit()
        definition=service.units.read_definition('demo',command['runtime_unit_ref'])
        await service.launch(instance,definition)
        async def ready(generation):
            for _ in range(80):
                await service.reconcile()
                async with repo.database.connection() as c:
                    current=await(await c.execute('SELECT * FROM assistant.service_instances ORDER BY generation DESC LIMIT 1')).fetchone()
                if current['observed_state']=='ready' and current['generation']==generation:return current
                await asyncio.sleep(.2)
            raise AssertionError('Actual isolated service did not become ready')
        first=await ready(1);assert first['state_json']['process_identity']['pid']
        task=service.documents.read('demo',definition['design_ref'])['task_id'];source=command['authorization_source_refs'][0]
        async with repo.database.connection() as c:
            await c.execute("UPDATE assistant.runs SET status='running',request_json=request_json || %s WHERE run_id=%s",(Jsonb({
                'current_stage':'operate','confirmed_turn_package_ref':definition['design_ref'],'prompt_confirmation_message_id':source['id']}),task));await c.commit()
        due=datetime.now(timezone.utc)+timedelta(seconds=10)
        body={'project_id':'demo','schedule_id':'real-maintenance','name':'Real isolated maintenance','runtime_unit_ref':command['runtime_unit_ref'],
            'action':'restart','inputs':{},'rule':{'kind':'once','at':due.isoformat()},'timezone':'Asia/Shanghai',
            'misfire_policy':'skip','overlap_policy':'skip','notification_policy':'every_occurrence','source_refs':[source]}
        ref=service.documents.write_scoped_revision(project_id='demo',scope='project',object_id='real-schedule',document_kind='kernel-schedule-definition',
            document_schema='kernel-schedule-definition.v1',payload=body,status='submitted',actor_type='kernel',actor_id='fixture',reason='Synthetic isolated authority',source_refs=[source])
        schedules=Schedules(core_settings,repo);scheduler=Scheduler(core_settings,repo)
        await schedules.register('demo',ref,task_id=task,authorization_source_refs=[source],idempotency_key='register')
        await schedules.control('demo',{'schedule_id':body['schedule_id'],'action':'enable','expected_control_revision':0,'idempotency_key':'enable',
            'authorization_source_refs':[source],'target_definition_ref':None},task_id=task)
        await scan(repo,scheduler,due);second=await ready(2)
        assert second['state_json']['process_identity']['pid']!=first['state_json']['process_identity']['pid']
        await scan(repo,scheduler,due+timedelta(seconds=1))
        assert (await schedules.ticks('demo',body['schedule_id']))['items'][0]['settlement_status']=='succeeded'
        await service.control('demo',{**command,'action':'stop','expected_control_revision':2,'idempotency_key':'stop'},authenticated_operator=True)
        for _ in range(50):
            await service.reconcile();current=await service.get(second['instance_id'])
            if current['cleanup_status']=='verified':break
            await asyncio.sleep(.2)
        assert current['cleanup_status']=='verified' and current['observed_state']=='stopped'
        await service.reconcile()  # Settle the manual stop before asking the timer for a new action.
        assert (await schedules.status('demo',body['schedule_id']))['registration']['state']=='paused'
        for action in ('start','stop'):
            sid='real-scheduled-'+action;planned=datetime.now(timezone.utc)+timedelta(seconds=10)
            candidate={**body,'schedule_id':sid,'action':action,'rule':{'kind':'once','at':planned.isoformat()}}
            ref=service.documents.write_scoped_revision(project_id='demo',scope='project',object_id=sid,document_kind='kernel-schedule-definition',
                document_schema='kernel-schedule-definition.v1',payload=candidate,status='submitted',actor_type='kernel',actor_id='fixture',
                reason='Synthetic authority for actual service lifecycle regression',source_refs=[source])
            await schedules.register('demo',ref,task_id=task,authorization_source_refs=[source],idempotency_key=sid)
            await schedules.control('demo',{'schedule_id':sid,'action':'enable','expected_control_revision':0,'idempotency_key':sid+'-enable',
                'authorization_source_refs':[source],'target_definition_ref':None},task_id=task)
            await scan(repo,scheduler,planned)
            for _ in range(80):
                await service.reconcile();await scan(repo,scheduler,planned+timedelta(seconds=1))
                tick=(await schedules.ticks('demo',sid))['items'][0]
                if tick['settlement_status']=='succeeded':break
                await asyncio.sleep(.2)
            assert tick['settlement_status']=='succeeded' and tick['started_at'] and tick['notification_id'],tick
    finally:
        if service and repo:
            from assistant_kernel.worker.python_process import cleanup_python_group
            async with repo.database.connection() as c:instances=await(await c.execute('SELECT * FROM assistant.service_instances')).fetchall()
            for item in instances:
                identity=item['state_json'].get('process_identity')
                if identity:await cleanup_python_group(identity,1)
            try:await service.manager.command('shutdown')
            except Exception:pass
        if manager.returncode is None:
            try:await asyncio.wait_for(manager.wait(),10)
            except asyncio.TimeoutError:manager.terminate();await manager.wait()
