"""API schema admission uses actual isolated PostgreSQL, not table-count guesses."""
import httpx
from pathlib import Path

from assistant_kernel.repositories import CoreRepository
from scripts.migrate_core5_v8 import migrate
from test_core4 import core_settings


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
