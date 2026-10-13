"""Role diagnostics use the same binding as message ingestion and leave tasks alone."""
import uuid

from psycopg.types.json import Jsonb
import pytest

from test_core4 import core_settings, prepare_bindable_project, prepare_discord_config
from assistant_kernel.repositories import CoreRepository
from assistant_kernel.services.discord import ingest_discord_event
from assistant_kernel.services.discord_interfaces import DiscordInterfaceRegistry


async def test_role_commands_report_actual_route_without_consuming_waiting_task(core_settings):
    prepare_discord_config(core_settings, users=[], owners=['300'])
    prepare_bindable_project(core_settings)
    repo=CoreRepository(core_settings)
    source=await repo.record_channel_command(project_id='demo',agent_key='project:demo:manager',channel='discord',
        chat_id='200',user_id='300',external_event_id='binding-source',external_message_id=None,content={'text':'test setup'})
    await repo.bind_chat_project(channel='discord',chat_id='200',project_id='demo',agent_key='project:demo:manager',
        bound_by_user_id='300',source_message_id=str(source['message']['message_id']),project_routing_hash='test-route',idempotency_key='pm-route')
    registry=DiscordInterfaceRegistry(core_settings)
    registry.upsert(channel_id='555',guild_id='100',project_id='demo',role='supervisor')
    task=await repo.create_run(project_id='demo',agent_key='project:demo:manager',run_type='chat',request={},
        trigger_message_id=str(source['message']['message_id']),idempotency_key='waiting-task')
    async with repo.database.connection() as c:
        await c.execute("UPDATE assistant.runs SET status='waiting_user',result_json=%s WHERE run_id=%s",
            (Jsonb({'preserved':'unanswered'}),task['run_id']));await c.commit()
    before=await repo.get_run(str(task['run_id']))
    async def command(text,channel='555',thread=False):
        identity=uuid.uuid4().hex
        return await ingest_discord_event(core_settings,{'event_id':identity,'message_id':identity,'guild_id':'100',
            'channel_id':channel,'parent_channel_id':'555' if thread else None,'is_thread':thread,
            'user_id':'300','author_bot':False,'content':text,'attachments':[]})
    for channel,actor in [('200','manager'),('555','supervisor'),('556','supervisor')]:
        for text in ['/whoami','/binding']:
            result=await command(text,channel,thread=channel=='556')
            reply=result['outbound']['content_json']['text']
            assert 'project:demo:'+actor in reply and '未绑定' not in reply
            assert result['run'] is None
    result=await command('/pm')
    assert 'Supervisor' in result['outbound']['content_json']['text'] and '<#200>' in result['outbound']['content_json']['text']
    for text in ['/bind_project demo','/unbind_project demo']:
        result=await command(text)
        assert '不能' in result['outbound']['content_json']['text']
    assert registry.resolve('555')['project_id']=='demo'
    assert (await repo.resolve_project_chat_binding('discord','demo'))['chat_id']=='200'
    assert await repo.resolve_chat_project_binding('discord','555') is None
    assert await repo.get_run(str(task['run_id']))==before
    assert len(await repo.list_runs('demo'))==1
