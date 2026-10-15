"""Per-invocation MCP entry point. No daemon, registry, or independent task state."""
from __future__ import annotations
import asyncio, hashlib, importlib, json, os, re
from contextlib import AsyncExitStack
from pathlib import Path
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, ListToolsResult, TextContent,ToolAnnotations
from assistant_kernel.services.mcp_runtime import McpConnection, schema_hash,read_only
from assistant_kernel.services.contracts import canonical_json


def exposed_name(server, tool):
    raw=server+'__'+tool
    name=re.sub(r'[^A-Za-z0-9_-]','_',raw)
    # Hash every business name, so normalization/concatenation cannot collide.
    return name[:88]+'_'+hashlib.sha256(canonical_json([server,tool])).hexdigest()[:12]


class ManagedTools:
    def __init__(self, config):
        self.config=config;self.stack=AsyncExitStack();self.kernel=None;self.tools={};self.routes={};self.authority=None
        self.business_repository=None;self.business_settings=None;self.service_owner=None
        self.instructions=[]
        self.receipt={'schema_version':'mcp-invocation.v1','invocation_id':config['invocation_id'],
            'declared':config.get('declared',[]),'selected':config.get('selected',[]),
            'initialized':[],'exposed':[],'calls':[],'cleanup':'pending',
            'inventory_scope':'this managed MCP entry point; not a CLI-wide observation'}

    def save(self):
        path=Path(self.config['receipt_path']);temporary=path.with_suffix('.tmp')
        temporary.write_bytes(canonical_json(self.receipt));temporary.replace(path)

    async def owner(self):
        context=self.config.get('business_context')
        if not context:return None
        from assistant_kernel.config import KernelSettings
        from assistant_kernel.repositories import CoreRepository
        from assistant_kernel.services.business_owner import active
        if self.business_repository is None:
            self.business_settings=KernelSettings.from_environment();self.business_repository=CoreRepository(self.business_settings)
        settings=self.business_settings;repository=self.business_repository
        async with repository.database.connection() as c:
            row=await (await c.execute("SELECT status,project_id FROM assistant.model_invocations WHERE invocation_id=%s",(self.config['invocation_id'],))).fetchone()
        if not row or row['status']!='running' or row['project_id']!=context['project_id']:
            raise PermissionError('Business model invocation is not active')
        if context.get('service_instance_id'):
            from assistant_kernel.services.service_runtime import ServiceRuntime
            if self.service_owner is None:
                runtime=ServiceRuntime(settings,repository);instance=await runtime.get(context['service_instance_id'])
                if not instance or instance['generation']!=context['attempt']:raise PermissionError('Service generation changed')
                self.service_owner=runtime.owner(instance,context['logical_execution_id'])
            run=self.service_owner
        else:
            run=await repository.get_run(context['run_id'])
            if not run or str(run['claim_token'])!=context['claim_token'] or run['attempt']!=context['attempt']:
                raise PermissionError('Business Run claim changed')
        if run['project_id']!=context['project_id'] or not await active(repository,run):raise PermissionError('Business owner is inactive')
        return settings,repository,run

    async def authorize(self):
        if self.config.get('business_context'):return await self.owner()
        if self.kernel is not None:
            await self.kernel.check_lease()  # legacy runs or core.runs, depending on the invocation
        return None

    async def open(self):
        os.environ.update(self.config.get('control_environment',{}))
        os.environ.update(self.config.get('scope_context',{}))
        if self.config.get('scoped_tools'):
            if self.config.get('business_context'):raise PermissionError('Business entry point cannot expose Kernel control tools')
            self.kernel=importlib.import_module('assistant_kernel.mcp_server.scoped_tools')
            listed=await self.kernel.server.list_tools();wanted=set(self.config['scoped_tools'])
            self.tools={t.name:t for t in listed if t.name in wanted}
            if wanted-self.tools.keys():raise ValueError('Required Kernel tool is not implemented')
            self.receipt['initialized'].append({'server_id':'kernel_scoped','status':'ready',
                'loaded_tools':sorted(self.tools),'tool_schema_sha256':{k:schema_hash(t) for k,t in self.tools.items()}})
        owner=await self.authorize()
        business_bindings={}
        if owner:
            settings,repository,run=owner
            from assistant_kernel.services.business_runtime import BusinessRuntime
            from assistant_kernel.services.mcp_runtime import resolve_binding
            runtime=BusinessRuntime(settings,repository);agent_id=self.config['business_context']['agent_id']
            _,agent=runtime.agent(run,agent_id)
            for declared in agent['mcp_bindings']:
                binding,env,_=runtime.mcp_binding(run,declared)
                business_bindings[binding['server_id']]=resolve_binding(binding,env)
            self.authority={'project_id':run['project_id'],'agent_id':agent_id,'runtime_unit_ref':run['request_json']['runtime_unit_ref']}
        for entry in self.config.get('bindings',[]):
            binding=entry['binding']
            if owner:
                actual=business_bindings.get(binding['server_id'])
                from assistant_kernel.services.mcp_runtime import resolve_binding
                if actual!=resolve_binding(binding,entry['environment']):raise PermissionError('Business MCP differs from the fixed binding')
            try:
                client=McpConnection(binding,entry['environment'])
                client.environment['KERNEL_MCP_INVOCATION_ID']=self.config['invocation_id']
                client=await self.stack.enter_async_context(client)
            except Exception as exc:
                if binding.get('required',True):raise
                self.receipt['initialized'].append({'server_id':binding['server_id'],'status':'unavailable','loaded_tools':[],'error_type':type(exc).__name__});continue
            self.receipt['initialized'].append(client.receipt())
            if client.instructions:self.instructions.append('Project MCP '+binding['server_id']+':\n'+client.instructions)
            for name in binding['enabled_tools']:
                if name not in client.tools:continue
                tool=client.tools[name];alias=exposed_name(binding['server_id'],name)
                if alias in self.tools:raise ValueError('Exposed tool name collision')
                readonly=read_only(binding,name)
                if not readonly and not self.config.get('business_context'):
                    raise PermissionError('Project mutation tools require a fixed business execution owner')
                annotations=tool.annotations or ToolAnnotations()
                changes={'name':alias,'description':(tool.description or '')+'\nProject MCP '+binding['server_id']+'/'+name,
                    'annotations':annotations.model_copy(update={'read_only_hint':readonly})}
                if not readonly:
                    changes['input_schema']={'type':'object','properties':{'arguments':tool.input_schema,
                        'idempotency_key':{'type':'string','minLength':1,'maxLength':150,'description':'Stable logical operation key. Reuse on retry; use a new key only for a distinct intended operation.'}},
                        'required':['arguments','idempotency_key'],'additionalProperties':False}
                    # The original output schema does not include Kernel error/receipt wrappers.
                    changes['output_schema']=None
                self.tools[alias]=tool.model_copy(update=changes);self.routes[alias]=(client,name,readonly)
        expected={r['server_id']:r.get('tool_schema_sha256',{}) for r in self.config.get('expected',[])}
        for actual in self.receipt['initialized']:
            if actual.get('status')=='unavailable':continue  # Only optional bindings reach this state.
            for name,digest in expected.get(actual['server_id'],{}).items():
                if actual.get('tool_schema_sha256',{}).get(name)!=digest:raise PermissionError('Confirmed MCP schema changed: '+name)
        self.receipt['exposed']=sorted(self.tools);self.save()

    async def call(self,name,arguments):
        if name not in self.tools:raise PermissionError('Tool is outside this invocation selection')
        owner=await self.authorize()
        from assistant_kernel.services.fixed_python import data_schema
        data_schema(arguments,self.tools[name].input_schema)
        record={'tool':name,'status':'started'};self.receipt['calls'].append(record);self.save()
        try:
            if name not in self.routes:
                result=await self.kernel.server.call_tool(name,arguments)
            else:
                client,raw,readonly=self.routes[name]
                if owner:
                    from assistant_kernel.services.business_tools import BusinessTools
                    settings,repository,run=owner
                    request={'agent_id':self.config['business_context']['agent_id'],'server_name':client.binding['server_id'],
                        'tool':raw,'arguments':arguments if readonly else arguments['arguments'],
                        'idempotency_key':'read' if readonly else arguments['idempotency_key']}
                    output=await BusinessTools(settings,repository).call(run,request,connection=client,authority=self.authority)
                    record['effect_receipt_ref']=output.get('effect_receipt_ref')
                    result=CallToolResult.model_validate(output)
                    if output.get('effect_receipt_ref'):
                        result=result.model_copy(update={'meta':{**(result.meta or {}),'kernel_effect_receipt_ref':output['effect_receipt_ref']}})
                else:result=await client.call(raw,arguments)
            # Legacy control handlers encode failures as JSON text; make the
            # protocol error bit agree without changing their result body.
            for content in getattr(result,'content',[]):
                if getattr(content,'type',None)=='text':
                    try:payload=json.loads(content.text)
                    except (ValueError,TypeError):continue
                    if isinstance(payload,dict) and payload.get('ok') is False:
                        result=result.model_copy(update={'is_error':True})
            record['status']='error' if getattr(result,'is_error',False) else 'succeeded';return result
        except BaseException as exc:
            record.update(status='unknown',error_type=type(exc).__name__);raise
        finally:self.save()

    async def close(self):
        try:await self.stack.aclose();self.receipt['cleanup']='closed'
        except BaseException:self.receipt['cleanup']='unknown';raise
        finally:self.save()


async def run():
    path=Path(os.environ['KERNEL_MCP_CONFIG_PATH']);encoded=path.read_bytes()
    if hashlib.sha256(encoded).hexdigest()!=os.environ['KERNEL_MCP_CONFIG_SHA256']:raise PermissionError('Invocation MCP configuration changed')
    config=json.loads(encoded)
    # Native child sessions may start their own connection. Preserve each actual
    # entry point receipt instead of racing on one shared JSON file.
    receipt=Path(config['receipt_path'])
    config['receipt_path']=str(receipt.with_name(receipt.stem+'.'+str(os.getpid())+receipt.suffix))
    managed=ManagedTools(config)
    try:
        await managed.open()
        async def listing(context,params):return ListToolsResult(tools=list(managed.tools.values()))
        async def calling(context,params):
            try:return await managed.call(params.name,params.arguments or {})
            except Exception as exc:
                # Never return endpoint credentials or nested HTTP request details.
                route=managed.routes.get(params.name);mutation=route is not None and not route[2]
                message={'ok':False,'error_type':type(exc).__name__,'outcome_status':'unknown' if mutation else 'rejected',
                    'retryable':False,'message':'Inspect the invocation receipt. Do not replay an uncertain mutation or invent a new idempotency key; use Kernel reconciliation.' if mutation else 'Managed MCP call rejected or failed; inspect the invocation receipt.'}
                return CallToolResult(isError=True,content=[TextContent(type='text',text=json.dumps(message))])
        server=Server('kernel-managed-tools',instructions='Tools are fixed to this invocation.\n'+'\n\n'.join(managed.instructions),
            on_list_tools=listing,on_call_tool=calling)
        async with stdio_server() as streams:await server.run(*streams,server.create_initialization_options())
    except BaseException as exc:
        managed.receipt['error_type']=type(exc).__name__
        raise
    finally:await managed.close()

if __name__=='__main__':asyncio.run(run())
