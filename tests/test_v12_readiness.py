"""API schema admission uses actual isolated PostgreSQL, not table-count guesses."""
import httpx
from pathlib import Path

from assistant_kernel.repositories import CoreRepository
from scripts.migrate_core5_v8 import migrate
from test_core4 import core_settings


async def test_readiness_uses_scheduler_heartbeat_updated_during_config_read(monkeypatch):
    from datetime import datetime, timezone, timedelta
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    import assistant_kernel.api.main as api
    import assistant_kernel.services.system_config as config_module

    now=lambda:datetime.now(timezone.utc).isoformat()
    stale=(datetime.now(timezone.utc)-timedelta(seconds=20)).isoformat()
    heartbeat={'process_running':True,'connected':True,'stale':False,'last_scan_at':stale}
    refresh=True
    def config(_ref):
        # The scheduler advances while the health request is loading config.
        if refresh:heartbeat['last_scan_at']=now()
        return {'kernel':{'scheduler':{'enabled':True,'poll_seconds':1}}}
    monkeypatch.setattr(config_module,'SystemConfigService',lambda _:SimpleNamespace(
        state=AsyncMock(return_value={'desired_ref':{}}),_config=config))
    monkeypatch.setattr(api,'settings',SimpleNamespace(
        load_provider_secrets=lambda:SimpleNamespace(providers={})))
    monkeypatch.setattr(api,'repository',SimpleNamespace(
        database=SimpleNamespace(ping=AsyncMock(return_value=True)),
        schema_version=AsyncMock(return_value=api.__schema_version__),
        table_names=AsyncMock(return_value=['kernel_meta','messages','model_invocations',
            'project_grants','runs','sessions','project_file_changes','project_runtime_units',
            'service_instances','project_schedules','schedule_ticks']),
        reconciliation_health=AsyncMock(return_value={}),execution_health=AsyncMock(return_value={}),
        delivery_health=AsyncMock(return_value={})))
    monkeypatch.setattr(api,'read_channel_health',lambda settings,channel:dict(heartbeat) if channel=='scheduler'
        else {'process_running':True,'connected':True,'stale':False})
    monkeypatch.setattr(api,'read_worker_health',lambda _: {'fresh_by_queue':{'interactive':2,'runtime':4}})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app),base_url='http://isolated') as client:
        ready=await client.get('/health/ready')
        assert ready.status_code==200 and ready.json()['scheduler_ready'] is True
        assert ready.json()['scheduler']['last_scan_at']==heartbeat['last_scan_at']
        refresh=False;heartbeat['last_scan_at']=stale
        unavailable=await client.get('/health/ready')
        assert unavailable.status_code==503 and unavailable.json()['scheduler_ready'] is False
        assert 'scheduler_unavailable' in unavailable.json()['degraded_reasons']


async def test_readiness_requires_v8_ledger_and_tolerates_unrelated_tables(core_settings, monkeypatch):
    import assistant_kernel.api.main as api
    repository=CoreRepository(core_settings)
    monkeypatch.setattr(api,'settings',core_settings)
    monkeypatch.setattr(api,'repository',repository)
    # This is a schema/readiness integration test. The isolated fixture does not
    # start production workers or fabricate a business-runtime acceptance claim.
    monkeypatch.setattr(api,'read_channel_health',lambda *args: {
        'process_running':True,'connected':True,'stale':False})
    monkeypatch.setattr(api,'read_worker_health',lambda *args: {
        'fresh_by_queue':{'interactive':2,'runtime':4}})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app),base_url='http://isolated') as client:
        assert (await client.get('/health/ready')).status_code==503
        before=(await client.get('/health/ready')).json()
        assert before['schema_version']=='core5.v7' and before['status']=='not_ready'
        await migrate(core_settings,apply=True)
        after=(await client.get('/health/ready')).json()
        assert after['schema_version']=='core5.v8' and after['status']=='not_ready'
        async with repository.database.connection() as c:
            for version in (9,10,11):await c.execute((Path(__file__).parents[1]/'migrations'/f'upgrade_core5_v{version}.sql').read_text())
            await c.commit()
        assert (await client.get('/health/ready')).json()['status']=='ready'
        monkeypatch.setattr(api,'read_worker_health',lambda *args: {
            'fresh_by_queue':{'interactive':0,'runtime':0}})
        missing=await client.get('/health/ready')
        assert missing.status_code==503 and missing.json()['status']=='not_ready'
        assert 'run_worker_missing' in missing.json()['degraded_reasons']
        assert (await client.get('/health/live')).status_code==200
        monkeypatch.setattr(api,'read_worker_health',lambda *args: {
            'fresh_by_queue':{'interactive':2,'runtime':4}})
        assert (await client.get('/health/ready')).status_code==200
        async with repository.database.connection() as c:
            await c.execute('CREATE TABLE assistant.unrelated_fixture (value integer)')
            await c.commit()
        assert (await client.get('/health/ready')).json()['status']=='ready'
        async with repository.database.connection() as c:
            await c.execute('DROP TABLE assistant.project_file_changes')
            await c.commit()
        assert (await client.get('/health/ready')).json()['status']=='not_ready'
