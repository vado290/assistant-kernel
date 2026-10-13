"""Live publication/test probe; normal invocations initialize their actual connection."""
import asyncio
from assistant_kernel.services.mcp_runtime import McpConnection

async def discover_mcp(binding, *, environment=None, cancel_event=None):
    async def perform():
        async with McpConnection(binding,environment) as connection:return connection.receipt()
    if cancel_event is not None and cancel_event.is_set():raise asyncio.CancelledError()
    task=asyncio.create_task(perform());stopped=asyncio.create_task(cancel_event.wait()) if cancel_event is not None else None
    try:
        done,_=await asyncio.wait({task,stopped} if stopped else {task},timeout=float(binding.get('startup_timeout_sec',30)),return_when=asyncio.FIRST_COMPLETED)
        if stopped is not None and stopped in done:raise asyncio.CancelledError()
        if task not in done:raise TimeoutError('MCP discovery exceeded startup window')
        return await task
    except Exception as exc:
        if binding.get('required',True):raise
        return {'server_id':binding['server_id'],'transport':binding['transport'],'requested_tools':list(binding.get('enabled_tools') or []),
            'loaded_tools':[],'missing_tools':list(binding.get('enabled_tools') or []),'status':'unavailable','error_type':type(exc).__name__}
    finally:
        for item in (task,stopped):
            if item is not None and not item.done():item.cancel()
        await asyncio.gather(*[x for x in (task,stopped) if x is not None],return_exceptions=True)


def validate_for_publication(bindings, environment):
    """Synchronous publisher bridge; secrets travel over stdin, never argv/files.

    The child's own discovery timeout closes transports before the outer guard.
    It is called only when publishing a new MCP implementation snapshot.
    """
    import json,os,subprocess,sys
    from assistant_kernel.services.mcp_runtime import resolve_binding
    receipts=[]
    for binding in bindings:
        normalized,env=resolve_binding(binding,environment,require_credentials=False)
        result=subprocess.run([sys.executable,'-m',__name__],input=json.dumps({'binding':normalized,'environment':env}),
            text=True,capture_output=True,timeout=normalized['startup_timeout_sec']+15,
            env={k:v for k,v in os.environ.items() if k!='PYTHONHOME'})
        if result.returncode:raise ValueError('Published MCP protocol validation failed: '+binding['server_id'])
        receipts.append(json.loads(result.stdout))
    return receipts


if __name__=='__main__':
    import json,sys
    request=json.load(sys.stdin)
    print(json.dumps(asyncio.run(discover_mcp(request['binding'],environment=request['environment']))))
