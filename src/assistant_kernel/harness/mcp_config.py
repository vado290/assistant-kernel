"""Expose the Kernel tool server to one Codex invocation and close every inherited MCP server."""
import asyncio,hashlib,json,os,sys
from assistant_kernel.harness.process_control import terminate_process_group

SOURCE_FLAGS=['-c','features.apps=false','-c','features.plugins=false','-c','features.remote_plugin=false',
              '-c','features.skill_mcp_dependency_install=false']
CONTROL_ENV=('AI_HOME','ASSISTANT_DATA_ROOT','ASSISTANT_PROVIDER_SECRETS','ASSISTANT_POSTGRES_HOST',
             'ASSISTANT_POSTGRES_PORT','POSTGRES_DB','POSTGRES_USER','POSTGRES_PASSWORD','RAG_ENV_FILE','PYTHONPATH')
TOOL_SERVER='assistant_kernel.core.tools'


def canonical_json(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8')


async def configured_servers(binary,cwd,environment,flags,cancel_event):
    # Config/read has no thread/start or MCP tools/list. It reveals effective
    # feature policy as well as ordinary servers; `mcp list` omits hosted Apps.
    p=await asyncio.create_subprocess_exec(binary,*flags,'app-server',cwd=str(cwd),env=environment,
        stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,
        start_new_session=True,limit=16*1024*1024)
    stderr=asyncio.create_task(p.stderr.read())
    async def rpc(identity,method,params):
        p.stdin.write((json.dumps({'id':identity,'method':method,'params':params})+'\n').encode());await p.stdin.drain()
        while True:
            line=await p.stdout.readline()
            if not line:raise PermissionError('Codex configuration reader exited')
            item=json.loads(line)
            if item.get('id')==identity:
                if 'error' in item:raise PermissionError('Codex rejected configuration inspection')
                return item['result']
    async def inspect():
        await rpc(1,'initialize',{'clientInfo':{'name':'kernel_mcp_policy','version':'0.32.0'},'capabilities':{'experimentalApi':True}})
        p.stdin.write(b'{"method":"initialized","params":{}}\n');await p.stdin.drain()
        result=await rpc(2,'config/read',{'cwd':str(cwd),'includeLayers':False});config=result['config']
        if any((config.get('features') or {}).get(key) is not False for key in ('apps','plugins','remote_plugin')):
            raise PermissionError('Effective managed policy does not disable unselected MCP sources')
        return [{'name':name,'enabled':value.get('enabled',True)} for name,value in (config.get('mcp_servers') or {}).items()]
    task=asyncio.create_task(inspect());stopped=asyncio.create_task(cancel_event.wait()) if cancel_event is not None else None
    try:
        done,_=await asyncio.wait({task,stopped} if stopped else {task},timeout=30,return_when=asyncio.FIRST_COMPLETED)
        if stopped is not None and stopped in done:raise asyncio.CancelledError()
        if task not in done:raise TimeoutError('Codex configuration inspection timed out')
        return await task
    finally:
        for t in (task,stopped):
            if t is not None and not t.done():t.cancel()
        await terminate_process_group(p)
        await asyncio.gather(*[t for t in (task,stopped,stderr) if t is not None],return_exceptions=True)
        p.stdin.close()
        try:await p.stdin.wait_closed()
        except (BrokenPipeError,ConnectionResetError):pass

async def compile_mcp(request,environment,binary,archive_dir):
    flags=list(SOURCE_FLAGS)
    inherited=await configured_servers(binary,request.cwd,environment,flags,request.cancel_event)
    if any(x['name']=='kernel_scoped' for x in inherited):
        raise PermissionError('Reserved kernel_scoped server must not be defined in persistent Codex configuration')
    # CLI dotted-path keys do not parse TOML quoting. A parsed inline table
    # preserves literal server IDs (including dots) while merging enabled=false.
    for item in inherited:flags+=['-c','mcp_servers={'+json.dumps(item['name'])+'={enabled=false}}']
    if inherited:
        effective=await configured_servers(binary,request.cwd,environment,flags,request.cancel_event)
        if any(x['enabled'] for x in effective):raise PermissionError('Managed policy prevents closing inherited MCP servers')
    source_receipt={'apps':False,'plugins':False,'remote_plugin':False,
        'disabled_inherited_servers':sorted(x['name'] for x in inherited),
        'verification':'effective configuration, not an observed CLI-wide live tool inventory'}
    (archive_dir/'mcp-source-policy.json').write_bytes(canonical_json(source_receipt))
    if not request.scoped_tools:return flags,None
    control={key:str(os.environ[key]) for key in CONTROL_ENV if key in os.environ}
    # Scope-selected values belong to this run, never a daemon's implicit state.
    payload={'invocation_id':archive_dir.name,'scope_context':request.scope_context,'control_environment':control,
        'scoped_tools':list(request.scoped_tools),'receipt_path':str(archive_dir/'mcp-runtime.json')}
    raw=canonical_json(payload);private=archive_dir/'mcp-private.json'
    fd=os.open(private,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'wb') as stream:stream.write(raw)
    environment['KERNEL_MCP_CONFIG_PATH']=str(private)
    environment['KERNEL_MCP_CONFIG_SHA256']=hashlib.sha256(raw).hexdigest()
    environment['KERNEL_MCP_INVOCATION_ID']=archive_dir.name
    # Credentials are read only by the trusted tool process, never serialized in argv.
    flags+=['-c','mcp_servers.kernel_scoped.command='+json.dumps(sys.executable),
        '-c','mcp_servers.kernel_scoped.args='+json.dumps(['-m',TOOL_SERVER]),
        '-c','mcp_servers.kernel_scoped.enabled=true','-c','mcp_servers.kernel_scoped.required=true',
        '-c','mcp_servers.kernel_scoped.disabled_tools=[]',
        '-c','mcp_servers.kernel_scoped.enabled_tools='+json.dumps(list(request.scoped_tools)),
        '-c','mcp_servers.kernel_scoped.default_tools_approval_mode="auto"',
        '-c','mcp_servers.kernel_scoped.startup_timeout_sec=30',
        '-c','mcp_servers.kernel_scoped.tool_timeout_sec=60',
        '-c','mcp_servers.kernel_scoped.env_vars=["KERNEL_MCP_CONFIG_PATH","KERNEL_MCP_CONFIG_SHA256","KERNEL_MCP_INVOCATION_ID","PYTHONPATH"]']
    return flags,private
