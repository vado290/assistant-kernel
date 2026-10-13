"""Shared, invocation-owned MCP configuration, transport and complete tool catalog."""
from __future__ import annotations
import asyncio, hashlib, os, re
from contextlib import AsyncExitStack
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import PaginatedRequestParams
from assistant_kernel.services.contracts import canonical_json

SERVER_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')
CORE_ENV = ('HOME','LOGNAME','PATH','SHELL','USER','LANG','LC_ALL','TERM','TMPDIR','TZ',
            'SSL_CERT_FILE','SSL_CERT_DIR','REQUESTS_CA_BUNDLE','NODE_EXTRA_CA_CERTS')

def resolve_binding(binding, environment=None, *, require_credentials=True):
    source = os.environ if environment is None else environment
    result = dict(binding)
    if not SERVER_ID.fullmatch(str(result.get('server_id') or '')):raise ValueError('Invalid MCP server identity')
    result['enabled_tools'] = list(dict.fromkeys(result.get('enabled_tools') or []))
    result['required'] = bool(result.get('required', True))
    for key, default in [('startup_timeout_sec',30),('tool_timeout_sec',60)]:
        result[key] = float(result.get(key, default))
        if not 0 < result[key] <= 3600:raise ValueError('MCP timeout must be within 0..3600 seconds')
    env = {key:str(source[key]) for key in [*CORE_ENV,*result.get('env_vars',[])] if key in source}
    env.update({str(k):str(v) for k,v in result.get('env',{}).items()})
    if result['transport'] == 'stdio':
        if not result.get('command'):raise ValueError('MCP command is missing')
    elif result['transport'] == 'streamable-http':
        if not str(result.get('url','')).startswith(('https://','http://')):raise ValueError('MCP endpoint is missing')
        key = result.get('bearer_token_env_var')
        if key:
            if not source.get(key):
                if require_credentials:raise PermissionError('Declared MCP credential is unbound: '+key)
            else:env[key] = str(source[key])
    else:raise ValueError('Unsupported MCP transport')
    return result, env

async def list_all_tools(session):
    tools = {}; cursor = None; seen = set()
    for _ in range(100):
        result = await session.list_tools(params=PaginatedRequestParams(cursor=cursor) if cursor else None)
        for tool in result.tools:
            if tool.name in tools:raise ValueError('MCP returned a duplicate tool name: '+tool.name)
            tools[tool.name] = tool
        cursor = result.next_cursor
        if not cursor:return tools
        if cursor in seen:raise ValueError('MCP tools/list cursor cycle')
        seen.add(cursor)
    raise ValueError('MCP tools/list page limit exceeded')

def schema_hash(tool):return hashlib.sha256(canonical_json(tool.input_schema)).hexdigest()

def read_only(binding, name):
    # Server annotations describe behavior; they cannot grant a bypass of the
    # Kernel effect ledger. Only the fixed, reviewed binding declares a query.
    return (binding.get('tool_effects') or {}).get(name)=='read'

def declaration(binding):
    return {'server_id':binding['server_id'],'transport':binding['transport'],
        'requested_tools':list(binding.get('enabled_tools') or []),'loaded_tools':[],
        'missing_tools':[], 'status':'declared','tool_schema_sha256':dict(binding.get('tool_schema_sha256') or {})}

async def declared_mcp(binding, *, environment=None, cancel_event=None):
    """Selection only: never resolve secrets or execute a server for confirmation."""
    if cancel_event is not None and cancel_event.is_set():raise asyncio.CancelledError()
    return declaration(binding)

class McpConnection:
    def __init__(self, binding, environment=None):
        self.binding, self.environment = resolve_binding(binding, environment)
        self.stack = AsyncExitStack();self.session = None;self.tools = {};self.instructions=None

    async def __aenter__(self):
        # MCP SDK task groups must enter/exit in the same task; do not wrap
        # _open in asyncio.wait_for (which creates a separate task).
        try:
            await self._open()
            return self
        except BaseException:
            await self.stack.aclose();raise

    async def _open(self):
        b = self.binding
        if b['transport'] == 'stdio':
            streams = await self.stack.enter_async_context(stdio_client(StdioServerParameters(
                command=b['command'],args=b.get('args',[]),cwd=b.get('cwd'),env=self.environment)))
        else:
            import httpx2
            from mcp.client.streamable_http import streamable_http_client
            headers = {}; key = b.get('bearer_token_env_var')
            if key:headers['Authorization'] = 'Bearer '+self.environment[key]
            client = await self.stack.enter_async_context(httpx2.AsyncClient(headers=headers,timeout=b['tool_timeout_sec']))
            streams = await self.stack.enter_async_context(streamable_http_client(b['url'],http_client=client))
        self.session = await self.stack.enter_async_context(ClientSession(*streams[:2]))
        import anyio
        with anyio.fail_after(b['startup_timeout_sec']):
            initialized=await self.session.initialize();self.instructions=initialized.instructions
            self.tools = await list_all_tools(self.session)
        missing = set(b['enabled_tools'])-self.tools.keys()
        if missing and b['required']:raise ValueError('Required MCP tools are missing: '+', '.join(sorted(missing)))
        for name, digest in (b.get('tool_schema_sha256') or {}).items():
            if name in b['enabled_tools'] and (name not in self.tools or schema_hash(self.tools[name]) != digest):
                raise PermissionError('Pinned MCP input schema changed: '+name)

    def receipt(self):
        requested = self.binding['enabled_tools'];missing = sorted(set(requested)-self.tools.keys())
        return {'server_id':self.binding['server_id'],'transport':self.binding['transport'],
            'requested_tools':requested,'loaded_tools':sorted(set(requested)&self.tools.keys()),
            'missing_tools':missing,'status':'partial' if missing else 'ready',
            'tool_schema_sha256':{name:schema_hash(self.tools[name]) for name in requested if name in self.tools}}

    async def call(self, name, arguments):
        if name not in self.binding['enabled_tools'] or name not in self.tools:raise PermissionError('Tool is outside this invocation binding')
        from assistant_kernel.services.fixed_python import data_schema
        data_schema(arguments,self.tools[name].input_schema)
        return await asyncio.wait_for(self.session.call_tool(name,arguments),self.binding['tool_timeout_sec'])

    async def __aexit__(self, *exc):await self.stack.aclose()
