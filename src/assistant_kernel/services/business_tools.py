"""Fixed business MCP tools and effect receipts over the logical owner's Run JSON."""
import asyncio,hashlib
from copy import deepcopy
from psycopg.types.json import Jsonb
from assistant_kernel.services.business_runtime import BusinessRuntime
from assistant_kernel.services.contracts import canonical_json
from assistant_kernel.services.fixed_python import data_schema
from assistant_kernel.services.prompt_protocol import PromptProtocol


class BusinessEffects:
    def __init__(self,settings,repository):self.settings=settings;self.repository=repository

    async def change(self,run,key,payload,*,finish=False):
        if not isinstance(key,str) or not key or len(key)>160:raise ValueError('Effect needs a bounded stable key')
        root=run['request_json'].get('runtime_root_run_id') or str(run['run_id'])
        async with self.repository.database.connection() as c:
            from assistant_kernel.services.business_owner import effect_owner,save_effect_owner,active
            if not finish and not await active(self.repository,run,c):raise PermissionError('Effect caller is no longer active')
            row,state,effects=await effect_owner(c,run);prior=effects.get(key)
            if finish:
                if not prior or prior['status']!='pending' or prior['attempt_run_id']!=str(run['run_id']):raise PermissionError('Effect completion has no matching active attempt')
                if payload.get('status') not in {'succeeded','unknown'}:raise ValueError('Effect completion needs a real result or unknown')
                receipt=PromptProtocol(self.settings,self.repository).write_blob(run['project_id'],str(run['run_id']),'business-effect',
                    {'logical_execution_id':run['request_json']['logical_execution_id'],'key':key,'request_sha256':prior['request_sha256'],**payload})
                prior.update(status=payload['status'],result=payload.get('result'),receipt_ref=receipt)
                response=deepcopy(prior)
            else:
                digest=hashlib.sha256(canonical_json(payload)).hexdigest()
                if prior:
                    if prior['request_sha256']!=digest:raise ValueError('Effect key was reused for different work')
                    if prior['status']!='succeeded':raise PermissionError('Effect outcome is pending/unknown; automatic replay is refused')
                    return {**deepcopy(prior),'duplicate':True}
                effects[key]={'request_sha256':digest,'status':'pending','attempt_run_id':str(run['run_id'])}
                response={**effects[key],'duplicate':False}
            await save_effect_owner(c,run,row,state);await c.commit()
            return response


class BusinessTools:
    def __init__(self,settings,repository):
        self.settings=settings;self.repository=repository;self.runtime=BusinessRuntime(settings,repository)

    async def call(self,run,request,*,connection=None,authority=None):
        if set(request)-{'agent_id','server_name','tool','arguments','idempotency_key'}:raise ValueError('Unknown business tool call fields')
        if connection is None:
            _,agent=self.runtime.agent(run,request['agent_id'])
            declarations=[x for x in agent['mcp_bindings'] if x['server_name']==request['server_name']]
            if len(declarations)!=1 or request['tool'] not in declarations[0]['required_tools']:raise PermissionError('Tool is outside the fixed Agent binding')
            binding,environment,_=self.runtime.mcp_binding(run,declarations[0])
        else:
            # Only a trusted in-process entry point supplies this already-checked
            # connection. HTTP/SDK request fields cannot supply authority.
            if not authority or authority!={'project_id':run['project_id'],'agent_id':request['agent_id'],
                    'runtime_unit_ref':run['request_json']['runtime_unit_ref']}:
                raise PermissionError('Invocation binding owner changed')
            binding,environment=connection.binding,connection.environment
            if binding['server_id']!=request['server_name'] or request['tool'] not in binding['enabled_tools']:
                raise PermissionError('Tool is outside the fixed invocation binding')
        from assistant_kernel.services.mcp_runtime import McpConnection,read_only
        from assistant_kernel.services.business_owner import active
        if not await active(self.repository,run):raise PermissionError('Business execution is no longer active')
        async def invoke(client):
                tool=client.tools.get(request['tool'])
                if tool is None:raise PermissionError('Required tool is not actually available')
                data_schema(request['arguments'],tool.input_schema)
                # Read results remain fresh. Unknown effects conservatively use
                # the existing pending/succeeded/unknown business ledger.
                readonly=read_only(binding,request['tool'])
                if readonly:
                    result=await client.call(request['tool'],request['arguments'])
                    if result.is_error:raise RuntimeError('Actual business MCP tool reported an error')
                    return result.model_dump(mode='json',by_alias=True)
                effect=BusinessEffects(self.settings,self.repository);key='mcp:'+request['idempotency_key']
                prior=await effect.change(run,key,request)
                if prior['duplicate']:return {**prior['result'],'effect_receipt_ref':prior['receipt_ref']}
                try:
                    result=await client.call(request['tool'],request['arguments'])
                    output=result.model_dump(mode='json',by_alias=True)
                    if result.is_error:raise RuntimeError('Actual business MCP tool reported an error')
                    completed=await effect.change(run,key,{'status':'succeeded','result':output},finish=True)
                    return {**output,'effect_receipt_ref':completed['receipt_ref']}
                except BaseException:
                    await effect.change(run,key,{'status':'unknown','result':None},finish=True);raise
        async def perform():
            async with McpConnection(binding,environment) as client:return await invoke(client)
        if connection is not None:
            # The enclosing BusinessModels invocation already owns cancellation
            # and its process lifetime. Do not nest another lease-watcher task
            # group for each call on that same connection.
            return await invoke(connection)
        task=asyncio.create_task(perform())
        from assistant_kernel.services.business_models import BusinessModels
        stopped=asyncio.Event();watch=asyncio.create_task(BusinessModels(self.settings,self.repository).watch(run,stopped));cancel=asyncio.create_task(stopped.wait())
        try:
            done,_=await asyncio.wait({task,cancel},timeout=binding['startup_timeout_sec']+binding['tool_timeout_sec'],return_when=asyncio.FIRST_COMPLETED)
            if task not in done:raise TimeoutError('Business tool caller cancelled or timed out')
            return await task
        finally:
            for item in (task,watch,cancel):
                if not item.done():item.cancel()
            await asyncio.gather(task,watch,cancel,return_exceptions=True)
