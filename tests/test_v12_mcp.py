"""Real stdio MCP and isolated PostgreSQL; no model or channel impersonation."""
import json
import os
from pathlib import Path
import sys
import uuid

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from assistant_kernel.repositories import CoreRepository
from assistant_kernel.services.contracts import contracts
from assistant_kernel.services.workspace_scope import WorkspaceScopeService
from test_core4 import core_settings
from test_v12_foundations import seed, write_requirement


async def test_fixed_config_document_tools_over_real_mcp(core_settings):
    service = await seed(core_settings)
    doc = write_requirement(core_settings)
    repo = CoreRepository(core_settings)
    await repo.ingest_message(project_id='demo', agent_key='project:demo:manager',
        channel='test', chat_id='fixture', user_id='fixture-owner',
        external_event_id='v12-mcp', external_message_id=None, content={'text':'isolated test'})
    worker = str(uuid.uuid4())
    claimed = await repo.claim_run('v12-mcp', worker, 60)
    uri, sha = WorkspaceScopeService(core_settings).snapshot('demo', str(claimed['run_id']), None)
    names = {'get_authorized_workspace', 'get_system_config', 'validate_system_config',
             'update_system_config', 'restore_system_config', 'read_project_document'}
    env = {**os.environ, 'AI_HOME':str(core_settings.workspace_root),
        'ASSISTANT_DATA_ROOT':str(core_settings.data_root),
        'ASSISTANT_PROVIDER_SECRETS':str(core_settings.provider_secrets_path),
        'ASSISTANT_POSTGRES_HOST':core_settings.postgres_host,
        'ASSISTANT_POSTGRES_PORT':str(core_settings.postgres_port),
        'POSTGRES_DB':core_settings.postgres_database,
        'POSTGRES_USER':core_settings.postgres_user,
        'POSTGRES_PASSWORD':core_settings.postgres_password.get_secret_value(),
        'KERNEL_SCOPE_PROJECT_ID':'demo', 'KERNEL_SCOPE_RUN_ID':str(claimed['run_id']),
        'KERNEL_SCOPE_CLAIM_TOKEN':str(claimed['claim_token']),
        'KERNEL_SCOPE_WORKER_INSTANCE_ID':worker,
        'KERNEL_SCOPE_ALLOWED_TOOLS':','.join(sorted(names)),
        'KERNEL_SCOPE_STAGE':'', 'KERNEL_SCOPE_SNAPSHOT_URI':uri,
        'KERNEL_SCOPE_SNAPSHOT_SHA256':sha,
        'PYTHONPATH':str(Path(__file__).parents[1]/'src')}
    params = StdioServerParameters(command=sys.executable,
        args=['-B','-m','assistant_kernel.mcp_server.scoped_tools'],env=env)
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader,writer) as session:
            await session.initialize()
            assert {tool.name for tool in (await session.list_tools()).tools} == names
            await session.call_tool('get_authorized_workspace',{})
            async def invoke(name,args):
                result=await session.call_tool(name,args)
                assert not result.is_error
                payload=json.loads(result.content[0].text)
                contracts().validate(contracts().registry['public_interfaces'][name]['response'],payload)
                return payload
            public=await invoke('get_system_config',{'view':'desired','paths':[]})
            assert '/python_defaults/cpu_pool_size' in public['values']
            assert not any(path.startswith('/providers') or path.startswith('/channels') for path in public['values'])
            private=await invoke('get_system_config',{'view':'desired','paths':['/channels']})
            assert private['error_type']=='SCOPE_DENIED'
            effective=await invoke('get_system_config',{'view':'effective','paths':['/python_defaults/cpu_pool_size']})
            assert effective['values']['/python_defaults/cpu_pool_size'] is None
            read=await invoke('read_project_document',{'ref':doc,'max_bytes':262144})
            assert read['actual_scope']=='task' and read['coverage']['status']=='complete'
            foreign=await invoke('read_project_document',{'ref':(await service.state())['desired_ref'],'max_bytes':262144})
            assert foreign['error_type']=='SCOPE_DENIED'
            invalid=await invoke('read_project_document',{'ref':{**doc,'sha256':'0'*64},'max_bytes':262144})
            assert invalid['ok'] is False
            denied=await invoke('update_system_config',{'changes':[{'path':'/python_defaults/io_workers','value':2}],
                'expected_revision':(await service.state())['revision'],'reason':'test denial'})
            assert denied['error_type']=='SCOPE_DENIED' and denied['effect_status']=='not_started'
