import hashlib
import json
import uuid
import pytest
from assistant_kernel.repositories import CoreRepository
from assistant_kernel.services.project_changes import ProjectChangeService
from assistant_kernel.services.semantic_map import SemanticMapService
from assistant_kernel.services.workspace_scope import AuthorizedRoot
from assistant_kernel.storage import ProjectRuntimeManager
from scripts.migrate_core5_v8 import migrate, WAIT_SQL
from scripts.migrate_core5_v8 import rollback_before_exposure
from scripts.inspect_upgrade_state import inspect, preserved
from test_core4 import core_settings, _questionnaire_waiting


async def record(settings, batch, filename):
    runtime=ProjectRuntimeManager(settings.data_root).ensure('demo')
    maps=SemanticMapService(settings)
    if not (runtime.vault_root/'knowledge/map/latest.json').exists():maps.bootstrap('demo',purpose='fixture')
    path=runtime.vault_root/filename;path.write_text('print(1)\n')
    service=ProjectChangeService(settings)
    result=await service.capture_batch('demo',batch_id=batch,
        roots=[AuthorizedRoot('project',runtime.vault_root,'write','test')],
        changes=[{'target_uri':'workspace://project/'+filename,'path':filename,'operation':'create',
                  'after_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'purpose':'test file'}],
        reason='approved isolated test')
    assert result['ledger_committed']
    return result['changes'][0]


async def test_backfill_preserves_waiting_question_and_switches_only_new_writes(core_settings):
    repo,run,question=await _questionnaire_waiting(core_settings)
    old=await record(core_settings,'before','before.py')
    async with repo.database.connection() as c:
        before=list(await (await c.execute(WAIT_SQL)).fetchall())
        prior=(await (await c.execute("SELECT count(*) AS n FROM assistant.messages WHERE message_type='project_change'")).fetchone())['n']
    preview=await migrate(core_settings);assert preview['legacy_change_count']==1
    assert await repo.schema_version()=='core5.v7'
    result=await migrate(core_settings,apply=True);assert result['waiting_preserved']==1
    assert (await migrate(core_settings,apply=True))['verified']
    new=await record(core_settings,'after','after.py')
    repeated=await record(core_settings,'after','after.py');assert repeated==new
    async with repo.database.connection() as c:
        assert before==list(await (await c.execute(WAIT_SQL)).fetchall())
        assert (await (await c.execute("SELECT count(*) AS n FROM assistant.messages WHERE message_type='project_change'")).fetchone())['n']==prior
        assert (await (await c.execute('SELECT count(*) AS n FROM assistant.project_file_changes')).fetchone())['n']==2
    svc=ProjectChangeService(core_settings)
    page=await svc.list_project_changes('demo',limit=1)
    other=await svc.list_project_changes('demo',limit=1,cursor=page['next_cursor'])
    assert {page['items'][0]['change_id'],other['items'][0]['change_id']}=={old['change_id'],new['change_id']}
    assert (await svc.list_project_changes('another'))['items']==[]
    with pytest.raises(FileNotFoundError):await svc.read_change_diff('another',new['change_id'])


async def test_migration_refuses_active_execution_without_touching_it(core_settings):
    repo=CoreRepository(core_settings)
    await repo.ingest_message(project_id='demo',agent_key='project:demo:manager',channel='test',chat_id='one',user_id='owner',external_event_id='migration-active',external_message_id=None,content={'text':'fixture'})
    run=await repo.claim_run('fixture',str(uuid.uuid4()),60)
    with pytest.raises(ValueError,match='active'):await migrate(core_settings,apply=True)
    assert await repo.schema_version()=='core5.v7'
    assert (await repo.get_run(str(run['run_id'])))['status']=='running'


async def test_outbox_helper_is_atomic_with_its_obligation_and_ignores_transport_metadata(core_settings):
    repo=CoreRepository(core_settings)
    args=dict(project_id='demo',session_id=None,run_id=None,channel='test',chat_id='one',
              user_id='owner',message_type='kernel_notice',content={'text':'fixture'},
              reply_to_message_id=None,idempotency_key='notice-fixture')
    with pytest.raises(RuntimeError):
        async with repo.database.connection() as c:
            await c.execute("INSERT INTO assistant.kernel_meta(key,value) VALUES ('fixture-obligation','true'::jsonb)")
            await repo._create_outbound_locked(c,**args)
            raise RuntimeError('crash before commit')
    async with repo.database.connection() as c:
        assert not await (await c.execute("SELECT 1 FROM assistant.messages WHERE idempotency_key='notice-fixture'")).fetchone()
        assert not await (await c.execute("SELECT 1 FROM assistant.kernel_meta WHERE key='fixture-obligation'")).fetchone()
    message=await repo.create_outbound(**args)
    async with repo.database.connection() as c:
        await c.execute("UPDATE assistant.messages SET content_json=content_json || '{\"delivery\":{\"external_id\":\"fixture\"}}'::jsonb WHERE message_id=%s",(message['message_id'],));await c.commit()
    assert (await repo.create_outbound(**args))['message_id']==message['message_id']
    with pytest.raises(ValueError):await repo.create_outbound(**{**args,'content':{'text':'different'}})
    with pytest.raises(ValueError):await repo.create_outbound(**{**args,'chat_id':'other'})


async def test_upgrade_snapshot_and_pre_exposure_rollback_preserve_waiting_state(core_settings):
    repo,run,question=await _questionnaire_waiting(core_settings)
    await record(core_settings,'before','before.py')
    before=await inspect(core_settings)
    assert len(before['protected_interactions'])==1
    assert not before['blockers']
    await migrate(core_settings,apply=True)
    assert preserved(before,await inspect(core_settings))
    assert (await rollback_before_exposure(before,core_settings))['rollback_allowed']
    assert (await rollback_before_exposure(before,core_settings,apply=True))['business_facts_preserved']
    assert await repo.schema_version()=='core5.v7'
    assert preserved(before,await inspect(core_settings))
    await migrate(core_settings,apply=True)
    await record(core_settings,'new','new.py')
    with pytest.raises(ValueError,match='ledger facts'):
        await rollback_before_exposure(before,core_settings,apply=True)
    assert await repo.schema_version()=='core5.v8'


async def test_rollback_refuses_new_user_input(core_settings):
    repo,run,question=await _questionnaire_waiting(core_settings)
    before=await inspect(core_settings)
    await migrate(core_settings,apply=True)
    await repo.ingest_message(project_id='demo',agent_key='project:demo:manager',channel='test',chat_id='new-chat',user_id='owner',external_event_id='new-during-deploy',external_message_id=None,content={'text':'new input'})
    with pytest.raises(ValueError,match='business facts'):
        await rollback_before_exposure(before,core_settings,apply=True)
    assert await repo.schema_version()=='core5.v8'
