import uuid
import pytest
from test_core4 import core_settings, prepare_discord_config, prepare_bindable_project
from assistant_kernel.repositories import CoreRepository
from assistant_kernel.services.discord import ingest_discord_event
from assistant_kernel.services.agent_files import AgentFileLoader
from assistant_kernel.services.public_background import PublicBackgroundService


@pytest.mark.parametrize("text", ["hello", "/new", "/butler"])
async def test_old_and_unbound_entry_creates_notice_without_any_run(core_settings, text):
    prepare_discord_config(core_settings, users=[], owners=['300'])
    result = await ingest_discord_event(core_settings, {
        'event_id':'retirement','message_id':'retirement','guild_id':'100',
        'channel_id':'999','user_id':'777','author_bot':False,
        'content':text,'attachments':[]})
    assert result.get('run') is None
    repo=CoreRepository(core_settings)
    async with repo.database.connection() as c:
        count=await (await c.execute('SELECT count(*) AS n FROM assistant.runs')).fetchone()
        notice=await (await c.execute("SELECT content_json FROM assistant.messages WHERE direction='outbound'")).fetchone()
    assert count['n']==0 and '退役' in notice['content_json']['text']


async def test_retired_identities_cannot_enter_but_existing_pm_can(core_settings):
    repo=CoreRepository(core_settings)
    for project,agent in [('system','system-butler'),('kernel','project:kernel:intake'),('butler-maintenance','project:butler-maintenance:manager')]:
        with pytest.raises(PermissionError,match='retired'):
            await repo.ingest_message(project_id=project,agent_key=agent,channel='test',chat_id='c',user_id='u',
                external_event_id=uuid.uuid4().hex,external_message_id=None,content={'text':'hello'})
    prepare_bindable_project(core_settings,'demo')
    result=await repo.ingest_message(project_id='demo',agent_key='project:demo:manager',channel='test',chat_id='c',user_id='u',
        external_event_id=uuid.uuid4().hex,external_message_id=None,content={'text':'hello'})
    assert result['run']['agent_key']=='project:demo:manager'
    assert result['run']['request_json'].get('interaction_kind') is None
    assert AgentFileLoader(core_settings).load('demo','project:demo:manager').kind=='manager'


async def test_old_migration_and_creation_are_disabled(core_settings):
    from scripts.merge_butler_into_kernel import migrate,planned_files
    from assistant_kernel.services.project_directory import ProjectDirectoryService
    with pytest.raises(PermissionError,match='retired'):await migrate(core_settings,apply=True)
    with pytest.raises(PermissionError,match='retired'):planned_files(core_settings)
    with pytest.raises(PermissionError,match='retired'):
        ProjectDirectoryService(core_settings,CoreRepository(core_settings)).propose('run','new-project','purpose')


def test_public_background_is_read_only_history_not_new_context(tmp_path):
    service=PublicBackgroundService(tmp_path)
    assert service.read_project_context('demo')['documents']==[]
    with pytest.raises(PermissionError,match='retired'):service.ensure()


async def test_historical_run_cannot_resume_retry_answer_or_be_claimed(core_settings):
    prepare_bindable_project(core_settings,'kernel');repo=CoreRepository(core_settings)
    created=await repo.ingest_message(project_id='kernel',agent_key='project:kernel:manager',channel='test',
        chat_id='history',user_id='owner',external_event_id='historical-fixture',external_message_id=None,content={'text':'old'})
    run_id=str(created['run']['run_id'])
    async with repo.database.connection() as c:
        await c.execute("UPDATE assistant.runs SET agent_key='project:kernel:intake',status='waiting_user' WHERE run_id=%s",(run_id,))
        await c.commit()
    before=await repo.get_run(run_id)
    calls=[repo.unblock_run(run_id,'old unblock'),repo.reconcile_run(run_id,'retry','old card'),
        repo.auto_requeue_reconciled(run_id=run_id,reason='old retry'),
        repo.resume_waiting_run(run_id=run_id,project_id='kernel',channel='test',chat_id='history',user_id='owner',
            source_message_id=str(created['message']['message_id']),idempotency_key='old-resume'),
        repo.record_question_choice(project_id='kernel',channel='test',chat_id='history',user_id='owner',
            external_event_id='old-choice',question_message_id=str(uuid.uuid4()),run_id=run_id,question_id='q',interaction_seq=1,choice_id='yes')]
    for call in calls:
        with pytest.raises(PermissionError,match='retired'):await call
        assert await repo.get_run(run_id)==before
    async with repo.database.connection() as c:
        await c.execute("UPDATE assistant.runs SET status='queued' WHERE run_id=%s",(run_id,));await c.commit()
    queued=await repo.get_run(run_id)
    assert await repo.claim_run('retirement-test',str(uuid.uuid4()),60) is None
    assert await repo.get_run(run_id)==queued


def test_new_configuration_removes_tools_but_fixed_old_snapshot_remains_readable(core_settings):
    from dataclasses import replace
    from assistant_kernel.services.agent_files import without_retired_tools
    from assistant_kernel.services.kernel_management import RETIRED_TOOLS
    prepare_bindable_project(core_settings,'kernel');loader=AgentFileLoader(core_settings)
    base=loader.load('kernel','project:kernel:manager')
    legacy=replace(base,allowed_tools=tuple(sorted(RETIRED_TOOLS)),config_sha256='e'*64)
    uri,digest=loader.snapshot('kernel',legacy)
    new=without_retired_tools(legacy)
    assert new.allowed_tools==() and new.config_sha256!=legacy.config_sha256
    assert loader.load_snapshot('kernel',uri,digest).allowed_tools==legacy.allowed_tools
    assert without_retired_tools(new)==new


def test_current_catalog_does_not_advertise_retired_tools():
    from assistant_kernel.services.contracts import contracts
    from assistant_kernel.services.kernel_management import RETIRED_TOOLS
    registry=contracts().registry
    assert not set(registry['public_interfaces']) & RETIRED_TOOLS
