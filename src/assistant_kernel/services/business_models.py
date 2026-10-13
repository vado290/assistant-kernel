"""Managed business calls, using the existing invocation ledger and Work identity."""
import asyncio,hashlib,json,time,uuid
from copy import deepcopy
from psycopg.types.json import Jsonb
from assistant_kernel.services.contracts import canonical_json,strict_json
from assistant_kernel.services.fixed_python import data_schema
from assistant_kernel.services.business_runtime import BusinessRuntime,read_model_policy
from assistant_kernel.services.runtime_admission import RuntimeAdmission,RuntimeCapacityUnavailable
from assistant_kernel.services.prompt_protocol import PromptProtocol


class BusinessModels:
    def __init__(self,settings,repository):
        self.settings=settings;self.repository=repository;self.runtime=BusinessRuntime(settings,repository)

    async def invoke(self,run,request):
        if set(request)-{'agent_id','input','idempotency_key'} or not request.get('idempotency_key'):
            raise ValueError('Business model call needs its Agent, input and stable key')
        definition,agent=self.runtime.agent(run,request['agent_id']);binding=self.runtime.model(run,agent)
        roots=self.runtime.roots(run);project=run['project_id'];fixed=self.runtime.fixed
        policy=read_model_policy(fixed,project,definition,roots)
        if any(policy.get(k) is not None for k in ('hard_input_tokens','hard_output_tokens','hard_cost_usd')):
            raise PermissionError('The installed adapter cannot verify this hard token/money budget')
        if agent['input_schema_ref']:
            data_schema(request['input'],strict_json(fixed.source_path(project,agent['input_schema_ref'],roots).read_bytes()))
        root_id=run['request_json'].get('runtime_root_run_id') or str(run['run_id'])
        logical=run['request_json']['logical_execution_id']
        identity=project+':'+logical+':'+agent['agent_id']+':'+request['idempotency_key']
        invocation=uuid.uuid5(uuid.NAMESPACE_URL,'business-model:'+identity).hex
        digest=hashlib.sha256(canonical_json({'request':request,'binding':binding,'definition':run['request_json']['definition_ref']})).hexdigest()
        # Reserve call/time bounds together with the actual global model slot.
        async with self.repository.database.connection() as c:
            await c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('business-model-work:'+project+':'+logical,))
            await RuntimeAdmission(self.settings).lock(c)
            from assistant_kernel.services.business_owner import active
            live=await active(self.repository,run,c,lock=True)
            if not live:raise PermissionError('Model caller is no longer the active business execution')
            prior=await (await c.execute('SELECT * FROM assistant.model_invocations WHERE invocation_id=%s',(invocation,))).fetchone()
            if prior:
                if prior['metadata_json'].get('request_sha256')!=digest:raise ValueError('Business call key was reused for different inputs')
                if prior['metadata_json'].get('outcome_status')=='unknown':raise PermissionError('Model process cleanup needs reconciliation')
                response=prior['metadata_json'].get('business_response')
                if response is not None:return deepcopy(response)
                raise PermissionError('Previous model outcome needs reconciliation; it cannot be automatically replayed')
            totals=await (await c.execute("""SELECT count(*) AS calls,COALESCE(sum(CASE WHEN status='running'
                THEN (metadata_json->>'reserved_seconds')::numeric ELSE COALESCE((metadata_json->>'elapsed_seconds')::numeric,
                (metadata_json->>'reserved_seconds')::numeric) END),0) AS seconds
                FROM assistant.model_invocations WHERE project_id=%s AND actor_role='business'
                AND metadata_json->>'logical_execution_id'=%s""",(project,logical))).fetchone()
            if totals['calls']>=policy['max_model_calls']:raise PermissionError('Logical work item model call budget exhausted')
            remaining=float(policy['max_total_model_seconds'])-float(totals['seconds'])
            if remaining<1:raise PermissionError('Logical work item model time budget exhausted')
            timeout=min(binding['timeout_seconds'],remaining)
            capacity=await RuntimeAdmission(self.settings).model_reservation_locked(c,project,invocation)
            if policy.get('max_project_model_calls_24h'):
                daily=await (await c.execute("SELECT count(*) AS calls FROM assistant.model_invocations WHERE project_id=%s AND actor_role='business' AND started_at>=now()-interval '24 hours'",(project,))).fetchone()
                if daily['calls']>=policy['max_project_model_calls_24h']:raise PermissionError('Project rolling 24h model budget exhausted')
            active=await (await c.execute("SELECT count(*) AS calls FROM assistant.model_invocations WHERE project_id=%s AND status='running' AND actor_role='business' AND metadata_json->>'logical_execution_id'=%s AND metadata_json->>'agent_id'=%s",(project,logical,agent['agent_id']))).fetchone()
            if active['calls']>=agent['max_inflight_calls']:raise RuntimeCapacityUnavailable('Business Agent concurrent calls are occupied')
            if run.get('service_instance_id'):
                from assistant_kernel.services.service_runtime import ServiceRuntime
                runtime=ServiceRuntime(self.settings,self.repository);instance=await runtime.get(run['service_instance_id'],c)
                manifest=runtime.manifest(instance)
                concurrent=await (await c.execute("SELECT count(*) AS n FROM assistant.model_invocations WHERE status='running' AND metadata_json->>'service_instance_id'=%s",(str(run['service_instance_id']),))).fetchone()
                if concurrent['n']>=manifest['resources']['max_inflight_model_calls']:raise RuntimeCapacityUnavailable('Service model capacity is occupied')
            metadata={'request_sha256':digest,'logical_execution_id':logical,'runtime_unit_ref':run['request_json']['runtime_unit_ref'],
                'root_run_id':root_id,'agent_id':agent['agent_id'],'model_binding_ref':agent['model_binding_ref'],
                'business_input':request['input'],
                'reserved_seconds':timeout,'usage_status':'unknown','capacity_reservation':capacity}
            if run.get('service_instance_id'):
                metadata['service_instance_id']=str(run['service_instance_id'])
            await c.execute("""INSERT INTO assistant.model_invocations(invocation_id,project_id,run_id,actor_role,harness,provider,model,status,metadata_json)
                VALUES(%s,%s,%s,'business',%s,%s,%s,'running',%s)""",
                (invocation,project,None if run.get('service_instance_id') else run['run_id'],binding['adapter_kind'],binding['provider'],binding['model'],Jsonb(metadata)))
            await c.commit()
        instructions='\n\n'.join([fixed.source_path(project,agent['role_ref'],roots).read_text(),
            *[fixed.source_path(project,x,roots).read_text() for x in agent['skill_refs']]])
        manifest=fixed.read_manifest(project,run['request_json']['manifest_ref'])
        instructions+='\n\nCurrent Project roots: '+canonical_json([{'key':x.key,'path':str(x.path)} for x in roots]).decode()
        instructions+='\nFixed Project agreement: '+fixed.source_path(project,definition['project_agreement_ref'],roots).read_text()
        instructions+='\nThis business work is limited to its approved Unit purpose and declared writes: '+canonical_json({
            'purpose':definition['purpose'],'write_set':definition['write_set'],'write_targets':manifest['io']['write_targets']}).decode()
        instructions+='\nReturn only this declared business result. You cannot change Kernel stages, approvals, or execution control.'
        if agent['output_schema_ref']:
            instructions+='\nReturn JSON matching: '+fixed.source_path(project,agent['output_schema_ref'],roots).read_text()
        prompt=request['input'] if isinstance(request['input'],str) else canonical_json(request['input']).decode()
        stopped=asyncio.Event();started=time.monotonic();watch=asyncio.create_task(self.watch(run,stopped))
        result=None;usage={};trace=None;native=None
        try:
            task=asyncio.create_task(self.call(run,agent,binding,instructions,prompt,timeout,invocation,stopped))
            cancelled=asyncio.create_task(stopped.wait())
            try:
                done,_=await asyncio.wait({task,cancelled},timeout=timeout,return_when=asyncio.FIRST_COMPLETED)
                if task not in done:
                    stopped.set();task.cancel();await asyncio.gather(task,return_exceptions=True)
                    raise TimeoutError('Business model call cancelled or timed out')
                observed=await task;result,usage,trace,native=observed[:4];tool_calls=observed[4] if len(observed)>4 else []
            finally:
                cancelled.cancel();await asyncio.gather(cancelled,return_exceptions=True)
                if not task.done():task.cancel();await asyncio.gather(task,return_exceptions=True)
            output=strict_json(result) if agent['output_schema_ref'] else result
            if agent['output_schema_ref']:data_schema(output,strict_json(fixed.source_path(project,agent['output_schema_ref'],roots).read_bytes()))
            known=all(isinstance(usage.get(k),int) and usage[k]>=0 for k in ('input_tokens','output_tokens'))
            response={'value':output,'invocation_id':invocation,'usage_status':'actual' if known else 'unknown',
                'usage':usage if known else None,'trace_ref':trace,'tool_calls':tool_calls}
            receipt=PromptProtocol(self.settings,self.repository).write_blob(project,str(run['run_id']),'business-model-call',
                {'agent_id':agent['agent_id'],'input_sha256':hashlib.sha256(canonical_json(request['input'])).hexdigest(),
                    'model_binding_ref':agent['model_binding_ref'],'role_ref':agent['role_ref'],'skill_refs':agent['skill_refs'],**response})
            response['receipt_ref']=receipt
            cleanup=await self.cleanup(invocation)
            await self.repository.finish_model_invocation(invocation_id=invocation,status='succeeded',native_session_id=native,
                input_tokens=usage.get('input_tokens') or 0,output_tokens=usage.get('output_tokens') or 0,cached_input_tokens=usage.get('cached_input_tokens') or 0,
                metadata={'business_response':response,'usage_status':response['usage_status'],'elapsed_seconds':time.monotonic()-started,**cleanup})
            if cleanup['outcome_status']=='unknown':raise PermissionError('Model child cleanup is unknown; business continuation is stopped')
            return response
        except BaseException as exc:
            known=all(isinstance(usage.get(k),int) and usage[k]>=0 for k in ('input_tokens','output_tokens'))
            cleanup=await self.cleanup(invocation)
            await self.repository.finish_model_invocation(invocation_id=invocation,status='cancelled' if isinstance(exc,asyncio.CancelledError) else 'failed',
                input_tokens=usage.get('input_tokens') or 0,output_tokens=usage.get('output_tokens') or 0,
                error_type=type(exc).__name__,error_message='Managed business call failed; see its actual trace',
                metadata={'usage_status':'actual' if known else 'unknown','elapsed_seconds':time.monotonic()-started,**cleanup})
            raise
        finally:
            watch.cancel();await asyncio.gather(watch,return_exceptions=True)

    async def watch(self,run,event):
        while True:
            from assistant_kernel.services.business_owner import active
            if not await active(self.repository,run):
                event.set();return
            await asyncio.sleep(.5)

    async def cleanup(self,invocation):
        async with self.repository.database.connection() as c:
            row=await (await c.execute('SELECT metadata_json FROM assistant.model_invocations WHERE invocation_id=%s',(invocation,))).fetchone()
        identity=(row['metadata_json'] if row else {}).get('process_identity')
        if not identity:return {'outcome_status':'settled'}  # SDK calls/pre-launch failures have no managed child.
        from assistant_kernel.worker.python_process import cleanup_python_group
        cleanup=await cleanup_python_group(identity)
        return {'process_cleanup':cleanup,'outcome_status':'settled' if cleanup['status']=='verified' else 'unknown'}

    async def call(self,run,agent,binding,instructions,prompt,timeout,invocation,cancel_event):
        from assistant_kernel.worker.python_environment import python_environment,business_credentials
        runtime=self.runtime.fixed.runtime.describe(run['project_id'])
        environment=python_environment(run,runtime.vault_root)
        environment.update(ASSISTANT_RUN_ID=invocation,ASSISTANT_ATTEMPT='1')
        credentials,_=business_credentials(self.settings.data_root,run['project_id'],binding['credential_refs']);environment.update(credentials)
        if binding['adapter_kind']=='codex_cli_harness':
            import shutil
            from assistant_kernel.harness import CodexCliHarness
            from assistant_kernel.harness.base import AgentTurnRequest
            async def process_started(pid,ticks):
                from assistant_kernel.worker.python_process import python_process_identity
                identity=python_process_identity(pid,invocation,1)
                async with self.repository.database.connection() as c:
                    await c.execute('UPDATE assistant.model_invocations SET metadata_json=metadata_json || %s WHERE invocation_id=%s',
                        (Jsonb({'process_identity':identity}),invocation));await c.commit()
            # A business CLI gets its own native home: no inherited Kernel MCP/config.
            home_key=[run['request_json']['runtime_unit_ref'],run['request_json']['logical_execution_id'],agent['agent_id']]
            if run.get('service_instance_id'):home_key.append(str(run['service_instance_id']))
            home=runtime.vault_root/'business-state'/'model-homes'/hashlib.sha256(canonical_json(home_key)).hexdigest()
            home.mkdir(parents=True,exist_ok=True)
            if binding['provider']=='openai':
                if not (home/'auth.json').exists():shutil.copy2(runtime.codex_home/'auth.json',home/'auth.json');(home/'auth.json').chmod(0o600)
            else:
                if not CodexCliHarness._MCP_ID.fullmatch(binding['provider']) or not credentials.get('API_KEY') or not credentials.get('BASE_URL'):
                    raise PermissionError('Custom native provider needs its declared Project API_KEY and BASE_URL')
                environment['BUSINESS_MODEL_API_KEY']=credentials['API_KEY']
                (home/'config.toml').write_text('[model_providers.'+binding['provider']+']\nname='+json.dumps(binding['provider'])+
                    '\nbase_url='+json.dumps(credentials['BASE_URL'])+'\nwire_api="responses"\nenv_key="BUSINESS_MODEL_API_KEY"\n')
            tools=[]
            for declared in agent['mcp_bindings']:
                tool,tool_environment,_=self.runtime.mcp_binding(run,declared)
                tools.append({**tool,'_environment':tool_environment})
            native=None
            if agent['state_policy']=='business_thread_state':
                async with self.repository.database.connection() as c:
                    prior=await (await c.execute("SELECT native_session_id FROM assistant.model_invocations WHERE project_id=%s AND actor_role='business' AND status='succeeded' AND metadata_json->>'logical_execution_id'=%s AND metadata_json->>'agent_id'=%s AND COALESCE(metadata_json->>'service_instance_id','')=%s ORDER BY ended_at DESC LIMIT 1",
                        (run['project_id'],run['request_json']['logical_execution_id'],agent['agent_id'],str(run.get('service_instance_id') or '')))).fetchone()
                native=prior['native_session_id'] if prior else None
            result=await CodexCliHarness(self.runtime.fixed.runtime).run_turn(AgentTurnRequest(
                project_id=run['project_id'],prompt=instructions+'\n\nBusiness input:\n'+prompt,cwd=runtime.vault_root,
                physical_session_id=native,
                codex_home=home,
                process_started_callback=process_started,
                provider=binding['provider'],model=binding['model'],timeout_seconds=timeout,invocation_id=invocation,
                cancel_event=cancel_event,base_environment=environment,mcp_servers=tuple(tools),sandbox='danger-full-access',
                mcp_control_environment={'ASSISTANT_DATA_ROOT':str(self.settings.data_root),
                    'POSTGRES_DB':self.settings.postgres_database,'ASSISTANT_POSTGRES_HOST':self.settings.postgres_host,
                    'ASSISTANT_POSTGRES_PORT':str(self.settings.postgres_port),'POSTGRES_USER':self.settings.postgres_user,
                    'POSTGRES_PASSWORD':self.settings.postgres_password.get_secret_value()},
                business_mcp_context={'run_id':str(run['run_id']),'project_id':run['project_id'],
                    'attempt':run['attempt'],'claim_token':str(run.get('claim_token') or ''),
                    'service_instance_id':run.get('service_instance_id'),'logical_execution_id':run['request_json']['logical_execution_id'],
                    'agent_id':agent['agent_id']}))
            # Context usage is not billed turn usage. Read the actual completed-turn event.
            usage={}
            for event in result.events:
                if event.get('type')=='turn.completed':usage=event.get('usage') or {}
            from assistant_kernel.services.document_runtime import resource_ref
            trace=resource_ref(kind='source',identity=invocation,uri=result.archive_uri,sha256=result.archive_sha256,purpose='actual business native CLI events') if result.archive_uri else None
            calls=[event['item'] for event in result.events if event.get('type')=='item.completed' and event.get('item',{}).get('type')=='mcp_tool_call']
            return result.final_text,usage,trace,result.physical_session_id,calls
        if binding['adapter_kind']!='provider_api':raise PermissionError('Unsupported real business adapter')
        from assistant_kernel.config.settings import ProviderCredential
        from assistant_kernel.model_providers import CatalogChatProvider
        if agent['state_policy']=='business_thread_state':
            async with self.repository.database.connection() as c:
                previous=await (await c.execute("SELECT metadata_json FROM assistant.model_invocations WHERE project_id=%s AND actor_role='business' AND status='succeeded' AND metadata_json->>'logical_execution_id'=%s AND metadata_json->>'agent_id'=%s AND COALESCE(metadata_json->>'service_instance_id','')=%s ORDER BY started_at",
                    (run['project_id'],run['request_json']['logical_execution_id'],agent['agent_id'],str(run.get('service_instance_id') or '')))).fetchall()
            history=[{'input':x['metadata_json']['business_input'],'output':x['metadata_json']['business_response']['value']} for x in previous]
            if history:prompt='Prior same-business-thread messages:\n'+canonical_json(history).decode()+'\n\nCurrent input:\n'+prompt
        # Credentials belong to this Project, rather than the Kernel provider admin store.
        if not credentials.get('API_KEY') or not credentials.get('BASE_URL'):raise PermissionError('Declared Project provider credential needs API_KEY and BASE_URL')
        config=ProviderCredential(enabled=True,api_key=credentials['API_KEY'],provider_type=credentials.get('PROVIDER_TYPE','openai_compatible'),
            openai_compatible_base_url=credentials['BASE_URL'],chat_models={binding['model']:{'model_id':binding['model']}})
        result=await CatalogChatProvider(binding['provider'],config,timeout).chat(prompt,binding['model'],instructions,binding['max_output_tokens'])
        raw=result.usage
        usage={'input_tokens':raw.get('input_tokens',raw.get('prompt_tokens')),'output_tokens':raw.get('output_tokens',raw.get('completion_tokens'))}
        return result.text,usage,None,None
