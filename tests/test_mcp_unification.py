"""Real local MCP/CLI protocol checks; no model call except the opt-in canary."""
import asyncio,json,os,sys,uuid
from pathlib import Path
import pytest
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client
from assistant_kernel.services.mcp_runtime import McpConnection,list_all_tools
from assistant_kernel.services.mcp_discovery import discover_mcp
from assistant_kernel.mcp_server.managed import ManagedTools,exposed_name
from assistant_kernel.harness.mcp_config import compile_mcp
from assistant_kernel.harness.base import AgentTurnRequest

FIXTURE='''import json,os,sys
from pathlib import Path
log=Path(sys.argv[1]);log.open('a').write('start\\n')
for line in sys.stdin:
 d=json.loads(line);m=d.get('method')
 if 'id' not in d:continue
 if m=='initialize':r={'protocolVersion':'2024-11-05','capabilities':{'tools':{}},'serverInfo':{'name':'fixture','version':'1'}}
 elif m=='tools/list':
  second=(d.get('params') or {}).get('cursor')=='second'
  r={'tools':[{'name':'second' if second else 'first','description':'Harmless test query','inputSchema':{'type':'object','properties':{}},'annotations':{'readOnlyHint':True}}]}
  if not second:r['nextCursor']='second'
 elif m=='tools/call':
  log.open('a').write('call\\n');r={'content':[{'type':'text','text':str('ONLY_MCP_SECRET' in os.environ)}]}
 else:r={}
 print(json.dumps({'jsonrpc':'2.0','id':d['id'],'result':r}),flush=True)
'''

def binding(tmp_path):
    server=tmp_path/'server.py';server.write_text(FIXTURE)
    return {'server_id':'project.docs','transport':'stdio','command':sys.executable,
        'args':[str(server),str(tmp_path/'starts.log')],'cwd':str(tmp_path),
        'env_vars':['ONLY_MCP_SECRET'],'enabled_tools':['second'],'required':True,
        'startup_timeout_sec':10,'tool_timeout_sec':5,'tool_effects':{'first':'read','second':'read'}}

async def test_paginated_discovery_and_connection_reuse(tmp_path):
    b=binding(tmp_path);env={**os.environ,'ONLY_MCP_SECRET':'synthetic'}
    receipt=await discover_mcp(b,environment=env)
    assert receipt['loaded_tools']==['second']
    async with McpConnection(b,env) as client:
        assert (await client.call('second',{})).content[0].text=='True'
        assert (await client.call('second',{})).content[0].text=='True'
        with pytest.raises(PermissionError):await client.call('first',{})
    assert (tmp_path/'starts.log').read_text().splitlines().count('start')==2

async def test_managed_registry_is_filtered_and_keeps_one_connection(tmp_path):
    b=binding(tmp_path)
    g=ManagedTools({'invocation_id':'fixture','bindings':[{'binding':b,'environment':{**os.environ,'ONLY_MCP_SECRET':'synthetic'}}],
        'receipt_path':str(tmp_path/'receipt.json')})
    try:
        await g.open();name=exposed_name('project.docs','second')
        assert set(g.tools)=={name}
        await g.call(name,{});await g.call(name,{})
        with pytest.raises(PermissionError):await g.call(exposed_name('project.docs','first'),{})
    finally:await g.close()
    assert (tmp_path/'starts.log').read_text().splitlines().count('start')==1
    receipt=json.loads((tmp_path/'receipt.json').read_text())
    assert receipt['cleanup']=='closed' and len(receipt['calls'])==2

async def test_managed_fail_closed_on_schema_drift(tmp_path):
    b=binding(tmp_path);b['tool_schema_sha256']={'second':'0'*64}
    with pytest.raises(Exception):await discover_mcp(b,environment=os.environ)

async def test_server_readonly_hint_cannot_bypass_effect_policy(tmp_path):
    b=binding(tmp_path);b.pop('tool_effects')
    g=ManagedTools({'invocation_id':'untrusted-hint','bindings':[{'binding':b,'environment':dict(os.environ)}],
        'receipt_path':str(tmp_path/'hint.json')})
    try:
        with pytest.raises(PermissionError,match='business execution'):await g.open()
    finally:await g.close()

@pytest.mark.skipif(not Path('/home/v/AI/bin/codex').exists(),reason='requires installed Codex CLI')
async def test_real_cli_closes_inherited_servers_without_launching_them(tmp_path):
    b=binding(tmp_path);home=tmp_path/'home';home.mkdir();out=tmp_path/'invocation';out.mkdir()
    (home/'config.toml').write_text('[mcp_servers.unselected]\ncommand='+json.dumps(sys.executable)+'\nargs='+json.dumps(b['args'])+'\n')
    env={**os.environ,'CODEX_HOME':str(home),'ONLY_MCP_SECRET':'synthetic-never-in-argv'}
    request=AgentTurnRequest(project_id='fixture',prompt='unused',cwd=tmp_path,mcp_servers=(b,))
    flags,private=await compile_mcp(request,env,'/home/v/AI/bin/codex',out)
    assert not (tmp_path/'starts.log').exists()
    assert 'synthetic-never-in-argv' not in ' '.join(flags)
    assert 'mcp_servers={"unselected"={enabled=false}}' in flags
    # This is the actual selected managed process, not a second probe of the backend.
    params=StdioServerParameters(command=sys.executable,args=['-m','assistant_kernel.mcp_server.managed'],env=env)
    async with stdio_client(params) as streams:
        async with ClientSession(*streams[:2]) as session:
            await session.initialize();tools=await list_all_tools(session)
            assert set(tools)=={exposed_name('project.docs','second')}
            await session.call_tool(next(iter(tools)),{})
    private.unlink()
    assert (tmp_path/'starts.log').read_text().splitlines().count('start')==1

def test_aliases_do_not_collide():
    assert exposed_name('a.b','x')!=exposed_name('a_b','x')
    assert exposed_name('a','b__x')!=exposed_name('a__b','x')

async def test_http_credential_and_protocol_are_shared(tmp_path):
    import threading
    from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
    seen=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            assert self.headers.get('Authorization')=='Bearer synthetic-only-mcp-token'
            d=json.loads(self.rfile.read(int(self.headers['Content-Length'])));method=d.get('method');seen.append(method)
            if 'id' not in d:
                self.send_response(202);self.send_header('Content-Length','0');self.end_headers();return
            if method=='initialize':result={'protocolVersion':d['params']['protocolVersion'],'capabilities':{'tools':{}},'serverInfo':{'name':'http-fixture','version':'1'}}
            elif method=='tools/list':result={'tools':[{'name':'read','inputSchema':{'type':'object','properties':{}},'annotations':{'readOnlyHint':True}}]}
            else:result={'content':[{'type':'text','text':'http-ok'}]}
            raw=json.dumps({'jsonrpc':'2.0','id':d['id'],'result':result}).encode()
            self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
    http=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
    b={'server_id':'http.docs','transport':'streamable-http','url':f'http://127.0.0.1:{http.server_port}/mcp',
        'bearer_token_env_var':'ONLY_HTTP_TOKEN','enabled_tools':['read'],'required':True,'startup_timeout_sec':5,'tool_effects':{'read':'read'}}
    try:
        async with McpConnection(b,{'ONLY_HTTP_TOKEN':'synthetic-only-mcp-token'}) as client:
            assert (await client.call('read',{})).content[0].text=='http-ok'
            assert (await client.call('read',{})).content[0].text=='http-ok'
        assert seen.count('initialize')==1 and seen.count('tools/list')==1
        with pytest.raises(PermissionError):McpConnection(b,{})
    finally:http.shutdown();http.server_close();thread.join(2)

def test_phase_selection_is_explicit():
    from assistant_kernel.services.template_bundle import TemplateBundle
    p=TemplateBundle().profiles['mcp_tool_policy']['phases']
    writes={'delete_project_credential','write_candidate_file','delete_candidate_file','control_schedule','register_schedule'}
    assert not writes.intersection(p['requirements']) and not writes.intersection(p['audit'])
    assert 'control_schedule' in p['operate'] and 'get_authorized_workspace' in p['build']

def test_adoption_never_expands_existing_project_tools():
    from types import SimpleNamespace
    from scripts.adopt_mcp_release import select_existing_tools
    policy={'phases':{'build':['read','write']},'actors':{'supervisor':['read','write']}}
    config=SimpleNamespace(phase_mode='build',allowed_tools=('read','legacy'))
    assert select_existing_tools(config,policy,'pm')==('read',)
    assert select_existing_tools(config,policy,'supervisor')==('read',)

def test_file_configuration_does_not_add_undeclared_model_tools(tmp_path):
    from test_runtime_v2 import write_v2_project
    from assistant_kernel.services.agent_files import AgentFileLoader
    settings,root=write_v2_project(tmp_path)
    import yaml
    path=root/'agents/analyst/config.yaml';raw=yaml.safe_load(path.read_text());raw['tools']=['get_authorized_workspace'];path.write_text(yaml.safe_dump(raw))
    config=AgentFileLoader(settings).load('demo','project:demo:analyst')
    assert config.allowed_tools==('get_authorized_workspace',)
    assert not any(x['name']=='assistant-volcengine-models' for x in config.skill_bindings)

async def test_cleanup_only_touches_this_invocation():
    from assistant_kernel.worker.mcp_process import cleanup_mcp_processes
    own='test-'+uuid.uuid4().hex
    first=await asyncio.create_subprocess_exec('/bin/sleep','30',env={**os.environ,'KERNEL_MCP_INVOCATION_ID':own},start_new_session=True)
    other=await asyncio.create_subprocess_exec('/bin/sleep','30',env={**os.environ,'KERNEL_MCP_INVOCATION_ID':own+'-other'},start_new_session=True)
    try:
        result=await cleanup_mcp_processes(own);await first.wait()
        assert result['status']=='verified' and other.returncode is None
    finally:
        if first.returncode is None:first.kill();await first.wait()
        if other.returncode is None:other.kill();await other.wait()

# Database fixtures use a separate disposable PostgreSQL database.
from test_core4 import core_settings
from test_v12_p4_business_runtime import claimed_business

def test_publication_validates_and_pins_project_mcp(core_settings):
    from dataclasses import replace
    from test_v12_packages import candidate,source
    from assistant_kernel.services.published_agents import PublishedAgentService
    bundle,config,_=candidate(core_settings)
    root=core_settings.data_root/'vaults/demo/mcp/project.docs';root.mkdir(parents=True)
    (root/'server.py').write_text("from mcp.server.mcpserver import MCPServer\nm=MCPServer('published')\n@m.tool()\ndef read()->str:return 'fixed-v1'\nif __name__=='__main__':m.run(transport='stdio')\n")
    (root/'mcp-manifest.yaml').write_text('server_id: project.docs\ntransport: stdio\nentrypoint: server.py\ntools:\n- name: read\n  effect: read\n')
    config=replace(config,mcp_servers=({'server_id':'project.docs','enabled_tools':['read']},),pinned_mcp_servers=())
    service=PublishedAgentService(core_settings)
    ref=service.prepare('demo',configurations={config.agent_key+'/'+config.phase_mode:config},template_ref=bundle.ref('profiles.json'),
        adoption_scope={'project_ids':['demo'],'existing_task_ids':[],'new_tasks':True,'next_turn_binding_confirmation':True},source_refs=[source()])
    package=service.read_package('demo',ref);snapshot=next(iter(package['configurations'].values()))
    loaded=service.loader.load_snapshot('demo',snapshot['uri'],snapshot['sha256'])
    pinned=loaded.pinned_mcp_servers[0]
    assert pinned['publication_discovery']['status']=='ready'
    assert len(pinned['tool_schema_sha256']['read'])==64 and pinned['tool_effects']=={'read':'read'}

async def test_managed_business_read_uses_actual_owner_and_shared_service(core_settings):
    from assistant_kernel.services.business_runtime import BusinessRuntime
    repo,run,worker=await claimed_business(core_settings)
    async with repo.database.connection() as c:
        await c.execute("UPDATE assistant.runs SET lease_until=now()+interval '30 minutes' WHERE run_id=%s",(run['run_id'],));await c.commit()
    runtime=BusinessRuntime(core_settings,repo);_,agent=runtime.agent(run,'reporter')
    b,env,_=runtime.mcp_binding(run,agent['mcp_bindings'][0])
    invocation=uuid.uuid4().hex
    await repo.start_model_invocation(invocation_id=invocation,project_id='demo',run_id=str(run['run_id']),
        session_id=None,actor_role='business',harness='codex_cli',provider='fixture',model='fixture',reasoning_effort=None,auth_mode='unknown',metadata={})
    from assistant_kernel.config import KernelSettings
    from unittest.mock import patch
    g=ManagedTools({'invocation_id':invocation,'bindings':[{'binding':b,'environment':env}],
        'receipt_path':str(core_settings.data_root/'receipt.json'),'business_context':{'project_id':'demo',
            'agent_id':'reporter','run_id':str(run['run_id']),'attempt':run['attempt'],'claim_token':str(run['claim_token'])}})
    with patch.object(KernelSettings,'from_environment',return_value=core_settings):
        try:
            await g.open();result=await g.call(exposed_name(b['server_id'],'double'),{'n':21})
            assert result.content[0].text=='42'
            await repo.finish_run(str(run['run_id']),worker,str(run['claim_token']),status='succeeded')
            with pytest.raises(PermissionError):await g.call(exposed_name(b['server_id'],'double'),{'n':21})
        finally:await g.close()

async def test_managed_mutation_deduplicates_and_refuses_unknown_replay(core_settings,monkeypatch):
    import test_v12_p4_business_runtime as fixture
    monkeypatch.setattr(fixture,'TOOLCODE',fixture.TOOLCODE.replace('read_only_hint=True','read_only_hint=False').replace(
        '    return n*2',"    from pathlib import Path\n    with Path('effect-count.txt').open('a') as f:f.write('effect\\n')\n    return n*2"))
    from assistant_kernel.services.business_runtime import BusinessRuntime
    repo,run,worker=await claimed_business(core_settings,tool_effect="write")
    async with repo.database.connection() as c:
        await c.execute("UPDATE assistant.runs SET lease_until=now()+interval '30 minutes' WHERE run_id=%s",(run['run_id'],));await c.commit()
    runtime=BusinessRuntime(core_settings,repo);_,agent=runtime.agent(run,'reporter');b,env,_=runtime.mcp_binding(run,agent['mcp_bindings'][0])
    invocation=uuid.uuid4().hex
    await repo.start_model_invocation(invocation_id=invocation,project_id='demo',run_id=str(run['run_id']),session_id=None,
        actor_role='business',harness='codex_cli',provider='fixture',model='fixture',reasoning_effort=None,auth_mode='unknown',metadata={})
    from assistant_kernel.config import KernelSettings
    from unittest.mock import patch
    g=ManagedTools({'invocation_id':invocation,'bindings':[{'binding':b,'environment':env}],
        'receipt_path':str(core_settings.data_root/'mutation.json'),'business_context':{'project_id':'demo','agent_id':'reporter',
            'run_id':str(run['run_id']),'attempt':run['attempt'],'claim_token':str(run['claim_token'])}})
    with patch.object(KernelSettings,'from_environment',return_value=core_settings):
        try:
            await g.open();name=exposed_name(b['server_id'],'double');request={'arguments':{'n':21},'idempotency_key':'operation-1'}
            assert (await g.call(name,request)).content[0].text=='42'
            assert (await g.call(name,request)).content[0].text=='42'
            with pytest.raises(ValueError):await g.call(name,{**request,'arguments':{'n':22}})
            client=g.routes[name][0];original=client.call
            async def lost_reply(tool,args):
                await original(tool,args)
                raise TimeoutError('synthetic reply loss after real tool execution')
            client.call=lost_reply
            unknown={**request,'idempotency_key':'operation-2'}
            with pytest.raises(TimeoutError):await g.call(name,unknown)
            with pytest.raises(PermissionError):await g.call(name,unknown)
            assert (Path(b['cwd'])/'effect-count.txt').read_text().splitlines()==['effect','effect']
        finally:await g.close()

@pytest.mark.skipif(os.getenv('KERNEL_MCP_LIVE')!='1',reason='explicit real-model canary only')
async def test_real_model_scoped_and_project_mcp_canary(core_settings):
    """Real subscription model + CLI + scoped/project MCP, in an isolated DB/Vault.

    The synthetic local Run is not a human/channel acceptance test.
    """
    import shutil
    from assistant_kernel.repositories import CoreRepository
    from assistant_kernel.storage import ProjectRuntimeManager
    from assistant_kernel.services.workspace_scope import WorkspaceScopeService
    from assistant_kernel.harness.codex_cli import CodexCliHarness
    from test_v12_foundations import seed
    await seed(core_settings)
    template=core_settings.data_root/'system/codex-home-template';template.mkdir(parents=True,exist_ok=True)
    auth=Path('/home/v/AI/assistant-data/system/codex-home-template/auth.json')
    shutil.copy2(auth,template/'auth.json');(template/'auth.json').chmod(0o600)
    runtime=ProjectRuntimeManager(core_settings.data_root);vault=runtime.ensure('demo').vault_root
    repo=CoreRepository(core_settings)
    incoming=await repo.ingest_message(project_id='demo',agent_key='project:demo:manager',channel='test',chat_id='mcp-canary',
        user_id='synthetic-canary',external_event_id=uuid.uuid4().hex,external_message_id=None,content={'text':'Explicit isolated MCP implementation canary; not human acceptance'})
    worker=str(uuid.uuid4());run=await repo.claim_run('mcp-canary',worker,300)
    scope=WorkspaceScopeService(core_settings);uri,sha=scope.snapshot('demo',str(run['run_id']),None,variant='mcp-canary',shared_agreement=True)
    b=binding(vault);alias=exposed_name(b['server_id'],'second')
    context={'ASSISTANT_DATA_ROOT':str(core_settings.data_root),'POSTGRES_DB':core_settings.postgres_database,
        'ASSISTANT_POSTGRES_HOST':core_settings.postgres_host,'ASSISTANT_POSTGRES_PORT':str(core_settings.postgres_port),
        'POSTGRES_USER':core_settings.postgres_user,'POSTGRES_PASSWORD':core_settings.postgres_password.get_secret_value(),
        'KERNEL_SCOPE_PROJECT_ID':'demo','KERNEL_SCOPE_RUN_ID':str(run['run_id']),'KERNEL_SCOPE_CLAIM_TOKEN':str(run['claim_token']),
        'KERNEL_SCOPE_WORKER_INSTANCE_ID':worker,'KERNEL_SCOPE_ALLOWED_TOOLS':'get_authorized_workspace',
        'KERNEL_SCOPE_SNAPSHOT_URI':uri,'KERNEL_SCOPE_SNAPSHOT_SHA256':sha,'KERNEL_SCOPE_STAGE':''}
    invocation='live-mcp-'+uuid.uuid4().hex
    try:
        harness=CodexCliHarness(runtime)
        profile=await harness.resolve_model_profile(project_id='demo',provider='openai',model=None,reasoning_effort='low',
            context_policy='model_default',custom_context_window_tokens=None,custom_auto_compact_token_limit=None)
        result=await harness.run_turn(AgentTurnRequest(project_id='demo',cwd=vault,sandbox='danger-full-access',
            model=profile.model,provider='openai',reasoning_effort='low',timeout_seconds=120,invocation_id=invocation,
            scoped_tools=('get_authorized_workspace',),scope_context=context,mcp_servers=(b,),
            prompt='This is an authorized isolated integration canary. First call get_authorized_workspace. Then call the MCP tool '+alias+' with {}. Do not use shell or modify files. After both tools succeed reply exactly MCP_CANARY_OK.'))
        events=[e['item'] for e in result.events if e.get('type')=='item.completed' and e.get('item',{}).get('type')=='mcp_tool_call']
        assert {'get_authorized_workspace',alias}<={e.get('tool') for e in events}
        assert 'MCP_CANARY_OK' in result.final_text
        evidence=Path('/home/v/AI/.cache/kernel-mcp-review-20261013/live-canary.json')
        evidence.write_text(json.dumps({'invocation':invocation,'model':profile.model,'native_session':result.physical_session_id,
            'tools':[e.get('tool') for e in events],'metadata':result.metadata,'final_text':result.final_text,
            'scope':'isolated real-model canary; no human/channel acceptance'},indent=2))
    finally:
        (template/'auth.json').unlink(missing_ok=True);(runtime.describe('demo').codex_home/'auth.json').unlink(missing_ok=True)
