"""Pinned business assembly over existing Unit/Run records; no Agent registry."""
import hashlib,json,secrets
from copy import deepcopy
from pathlib import Path
from datetime import datetime,timezone

from assistant_kernel.services.contracts import canonical_json,contracts,strict_json
from assistant_kernel.services.document_runtime import TaskDocumentService,resource_ref,same_resource_ref
from assistant_kernel.services.fixed_python import FixedPython,data_schema
from assistant_kernel.services.mcp_runtime import declared_mcp as discover_mcp
from assistant_kernel.worker.python_environment import python_environment,business_credentials


def adapter_ref(kind):
    path=Path(__file__).with_name('business_models.py');digest=hashlib.sha256(path.read_bytes()).hexdigest()
    return resource_ref(kind='source',identity='business-adapter:'+kind,uri='kernel://business-model-adapters/'+digest+'/business_models.py',
        sha256=digest,purpose='actual installed '+kind+' adapter implementation')


def read_model_policy(fixed,project,definition,roots):
    ref=definition['resource_policy_ref']
    if not ref:raise PermissionError('Business models need their fixed call/time budget policy')
    policy=strict_json(fixed.source_path(project,ref,roots).read_bytes())
    allowed={'schema_version','project_id','max_model_calls','max_total_model_seconds','max_project_model_calls_24h',
        'hard_input_tokens','hard_output_tokens','hard_cost_usd'}
    if set(policy)-allowed or policy.get('schema_version')!='runtime-resource-policy.v1' or policy.get('project_id')!=project:
        raise ValueError('Expected the fixed same-Project runtime resource policy')
    for key in ('max_model_calls','max_total_model_seconds'):
        value=policy.get(key)
        if isinstance(value,bool) or not isinstance(value,(int,float)) or value<1:raise ValueError('Runtime policy needs positive call/time bounds')
        if key=='max_model_calls' and not isinstance(value,int):raise ValueError('Model call count must be an integer')
    for key in ('max_project_model_calls_24h','hard_input_tokens','hard_output_tokens','hard_cost_usd'):
        value=policy.get(key)
        if value is not None and (isinstance(value,bool) or not isinstance(value,(int,float)) or value<=0):raise ValueError('Invalid runtime model budget bound')
        if key=='max_project_model_calls_24h' and value is not None and not isinstance(value,int):raise ValueError('Project call count must be an integer')
    return policy


class BusinessRuntime:
    def __init__(self,settings,repository):
        self.settings=settings;self.repository=repository;self.fixed=FixedPython(settings);self.documents=TaskDocumentService(settings)

    def roots(self,run):return self.fixed.scopes.resolve(run['project_id'],str(run['run_id']),'build')

    def agent(self,run,agent_id):
        from assistant_kernel.services.runtime_units import RuntimeUnits
        definition=RuntimeUnits(self.settings,self.repository).read_definition(run['project_id'],run['request_json']['runtime_unit_ref'])
        found=[x for x in definition['business_agents'] if x['agent_id']==agent_id]
        if len(found)!=1:raise PermissionError('Business Agent is not in this fixed Unit definition')
        return definition,deepcopy(found[0])

    def model(self,run,agent):
        project=run['project_id'];roots=self.roots(run)
        binding=strict_json(self.fixed.source_path(project,agent['model_binding_ref'],roots).read_bytes())
        contracts().validate('BusinessModelBinding',binding)
        if binding['adapter_kind']=='offline_fixture':raise PermissionError('A fixture cannot be adopted as real business model capability')
        if not same_resource_ref(binding['adapter_implementation_ref'],adapter_ref(binding['adapter_kind'])):
            raise PermissionError('Declared model implementation differs from the actual installed adapter')
        return binding

    def mcp_binding(self,run,declared):
        project=run['project_id'];roots=self.roots(run)
        config=strict_json(self.fixed.source_path(project,declared['client_config_ref'],roots).read_bytes())
        environment=python_environment(run,self.fixed.runtime.describe(project).vault_root)
        credentials,versions=business_credentials(self.settings.data_root,project,declared['credential_refs']);environment.update(credentials)
        binding={'server_id':declared['server_name'],'transport':'stdio' if declared['transport']=='stdio' else 'streamable-http',
            'required':declared['required'],'enabled_tools':declared['required_tools'],
            'default_tools_approval_mode':'approve',  # Only the reviewed/enabled fixed business allowlist.
            'startup_timeout_sec':config.get('startup_timeout_sec',30),'tool_timeout_sec':config.get('tool_timeout_sec',60)}
        binding.update(tool_effects=dict(config.get('tool_effects') or {}),tool_schema_sha256=dict(config.get('tool_schema_sha256') or {}))
        if declared['transport']=='stdio':
            manifest=self.fixed.read_manifest(project,run['request_json']['manifest_ref'])
            env=strict_json(self.fixed.source_path(project,manifest['environment']['environment_ref'],roots).read_bytes())
            if config['command']!=env['interpreter_path']:raise PermissionError('Business stdio tools must use the fixed Python environment')
            refs=config.get('implementation_refs') or []
            if not refs:raise PermissionError('Business stdio tools need their pinned implementation files')
            pinned={ref['uri']:self.fixed.source_path(project,ref,roots) for ref in refs}
            args=[str(pinned[x]) if x in pinned else str(x) for x in config.get('args',[])]
            if not args or args[0] not in {str(x) for x in pinned.values()}:raise PermissionError('MCP entrypoint differs from its pinned implementation')
            binding.update(command=config['command'],args=args,cwd=str(self.fixed.runtime.describe(project).vault_root),env=credentials)
        else:
            if not str(config.get('url','')).startswith(('https://','http://')):raise ValueError('Business HTTP tool endpoint is missing')
            binding.update(url=config['url'],bearer_token_env_var=config.get('bearer_token_env_var'))
        return binding,environment,versions

    async def assemble(self,run,prepared):
        definition=prepared.get('definition')
        if not definition:return None
        project=run['project_id'];roots=self.roots(run);request=run['request_json']
        self.fixed.source_path(project,definition['project_agreement_ref'],roots)
        roles=[];skills=[];models=[];requested=[];observed=[];receipts=[];loaded=[];python_skills={}
        policy=read_model_policy(self.fixed,project,definition,roots) if definition['business_agents'] else None
        for agent in definition['business_agents']:
            role=self.fixed.source_path(project,agent['role_ref'],roots).read_text();roles.append(agent['role_ref'])
            instructions=[self.fixed.source_path(project,x,roots).read_text() for x in agent['skill_refs']];skills.extend(agent['skill_refs'])
            for skill in agent['skill_bindings']:
                self.fixed.source_path(project,skill['instruction_ref'],roots)
                if skill['execution_style']=='python_step':
                    ref=skill['implementation_ref'];self.fixed.source_path(project,ref,roots)
                    if not any(same_resource_ref(ref,x) for x in prepared['manifest']['code_bundle']['module_refs']):
                        raise PermissionError('Python Skill implementation is absent from the fixed code bundle')
                    python_skills[agent['agent_id']+':'+skill['instruction_ref']['id']]=str(prepared['materialized_files'][ref['uri']])
            model=self.model(run,agent);models.append(agent['model_binding_ref'])
            for name in ('input_schema_ref','output_schema_ref'):
                if agent[name]:
                    from jsonschema import Draft202012Validator
                    Draft202012Validator.check_schema(strict_json(self.fixed.source_path(project,agent[name],roots).read_bytes()))
            if any(policy.get(k) is not None for k in ('hard_input_tokens','hard_output_tokens','hard_cost_usd')):
                raise PermissionError('This installed adapter cannot prove the requested hard token/money bound')
            for declared in agent['mcp_bindings']:
                binding,environment,_=self.mcp_binding(run,declared)
                result=await discover_mcp(binding,environment=environment)
                # Live availability is established by the actual invocation's
                # connection. This receipt only records fixed selection.
                prefix=declared['server_name']+'/'
                requested.extend(prefix+x for x in result['requested_tools'])
                from assistant_kernel.services.prompt_protocol import PromptProtocol
                receipts.append(PromptProtocol(self.settings,self.repository).write_blob(project,str(run['run_id']),'business-mcp-discovery',result))
            loaded.append({'agent_id':agent['agent_id'],'role_sha256':agent['role_ref']['sha256'],'skill_sha256':[x['sha256'] for x in agent['skill_refs']],
                'model_native_mcp':model['adapter_kind']=='codex_cli_harness','program_mcp':bool(agent['mcp_bindings'])})
        if definition['workflow']['framework']=='langgraph':
            graph_definition=strict_json(self.fixed.source_path(project,definition['workflow']['definition_ref'],roots).read_bytes())
            if graph_definition.get('schema_version')!='business-workflow-graph.v1' or not graph_definition.get('nodes'):
                raise ValueError('Business graph needs its fixed declared node names')
            env=strict_json(self.fixed.source_path(project,prepared['environment_ref'],roots).read_bytes())
            if not {'langgraph','langgraph-checkpoint-sqlite'}<=set(env['distribution_versions']):
                raise PermissionError('Actual fixed environment lacks the required persistent graph dependencies')
            from assistant_kernel.worker.process_control import run_managed_process
            code,_,_,_=await run_managed_process(str(prepared['interpreter']),'-c',
                'from langgraph.graph import StateGraph; from langgraph.checkpoint.sqlite import SqliteSaver; from assistant_kernel.runtime_client import RuntimeClient; import httpx',
                cwd=str(prepared['cwd']),env=python_environment(run,prepared['attempt_root']),timeout_seconds=10)
            if code:raise PermissionError('Actual business graph/SDK imports failed in the fixed environment')
            if definition['workflow']['checkpoint_policy']!='project_namespaced':raise PermissionError('A managed business graph needs persistent Project checkpoint ownership')
        from assistant_kernel.services.business_owner import execution_ref
        execution=execution_ref(run)
        payload={'project_id':project,'runtime_unit_id':definition['runtime_unit_id'],'definition_ref':request['definition_ref'],
            'execution_ref':execution,'role_refs':roles,'skill_refs':skills,'model_binding_refs':models,
            'requested_mcp_tools':sorted(set(requested)),'observed_mcp_tools':sorted(set(observed)),'mcp_receipt_refs':receipts,
            'project_agreement_ref':definition['project_agreement_ref'],'environment_ref':prepared['environment_ref'],
            'status':'ready','missing_capabilities':[],'limits':['MCP bindings declared; live initialization occurs at actual invocation','Project shared-file behavior; not OS isolation','Model hard bounds require actual adapter capability'],
            'captured_at':datetime.now(timezone.utc).isoformat(),'background_selections':[],'resolved_config_ref':prepared['manifest']['resolved_config_ref']}
        receipt=self.documents.write_scoped_revision(project_id=project,scope='project',object_id='assembly-'+str(run['run_id'])+'-a'+str(run['attempt']),
            document_kind='runtime-assembly-receipt',document_schema='runtime-assembly-receipt.v1',payload=payload,status='settled',
            actor_type='kernel',actor_id='runtime-assembler',reason='Fixed business bindings; MCP availability is recorded by the actual invocation',source_refs=receipts+[request['definition_ref']])
        state_root=self.fixed.runtime.describe(project).vault_root/'business-state'/hashlib.sha256(canonical_json([
            definition['runtime_unit_id'],definition['workflow']['graph_id'],definition['workflow']['namespace'],request['logical_execution_id']])).hexdigest()
        state_root.mkdir(parents=True,exist_ok=True)
        input_path=prepared['attempt_root']/'input.json'
        input_ref=resource_ref(kind='source',identity='runtime-input',uri='vault://'+project+'/'+input_path.relative_to(self.fixed.runtime.describe(project).vault_root).as_posix(),
            sha256=hashlib.sha256(input_path.read_bytes()).hexdigest(),purpose='actual fixed business input')
        context={'schema_version':'business-runtime-context.v1','project_id':project,'runtime_unit_ref':request['runtime_unit_ref'],
            'manifest_ref':request['manifest_ref'],'execution_ref':execution,'service_instance_ref':run.get('service_instance_ref'),'logical_execution_id':request['logical_execution_id'],
            'project_agreement_ref':definition['project_agreement_ref'],'role_refs':roles,'skill_refs':skills,'model_binding_refs':models,
            'mcp_bindings':[x for a in definition['business_agents'] for x in a['mcp_bindings']],'input_ref':input_ref,
            'output_root_uri':str(prepared['business_output_root']),'resource_policy_ref':definition['resource_policy_ref'],
            'background_selections':[],'assembly_status':'ready','resolved_config_ref':prepared['manifest']['resolved_config_ref']}
        contracts().validate('V12_RuntimeContext',context)
        token=secrets.token_urlsafe(32)
        from psycopg.types.json import Jsonb
        async with self.repository.database.connection() as c:
            if run.get('service_instance_id'):
                row=await (await c.execute("""UPDATE assistant.service_instances SET state_json=state_json || %s
                    WHERE instance_id=%s AND generation=%s AND observed_state='starting' RETURNING instance_id""",
                    (Jsonb({'business_capability_sha256':hashlib.sha256(token.encode()).hexdigest(),'assembly_ref':receipt}),
                     run['service_instance_id'],run['attempt']))).fetchone()
            else:
                row=await (await c.execute("""UPDATE assistant.runs SET executor_json=executor_json || %s WHERE run_id=%s
                AND status='running' AND worker_instance_id=%s AND claim_token=%s AND cancel_requested_at IS NULL
                AND lease_until>now() RETURNING run_id""",
                (Jsonb({'business_capability_sha256':hashlib.sha256(token.encode()).hexdigest(),
                    'business_capability_attempt':run['attempt']}),run['run_id'],run['worker_instance_id'],run['claim_token']))).fetchone()
            if not row:raise PermissionError('Business assembly lost its active execution claim')
            await c.commit()
        private={'context':context,'gateway_url':'http://127.0.0.1:'+str(self.settings.api_port),'run_id':str(run['run_id']),
            'runtime_token':token,
            'business_state_root':str(state_root),'workflow':definition['workflow'],'resume_answers':request.get('runtime_resume_answers'),
            'assembly_ref':receipt,'loaded_agents':loaded,'project_vault':str(self.fixed.runtime.describe(project).vault_root),
            'python_skills':python_skills,'graph_definition':graph_definition if definition['workflow']['framework']=='langgraph' else None,
            'interrupt_ids':request.get('runtime_interrupt_ids',[])}
        if run.get('service_instance_id'):private.update(service_instance_id=str(run['service_instance_id']),generation=run['attempt'],work_limits=prepared['manifest']['resources'],service_health=prepared['manifest']['health'])
        if request.get('resume_checkpoint_ref'):
            private.update(resume_checkpoint_path=str(self.fixed.source_path(project,request['resume_checkpoint_ref'],roots)),
                resume_checkpoint_sha256=request['resume_checkpoint_ref']['sha256'])
        path=prepared['attempt_root']/'runtime-client.json';path.write_bytes(canonical_json(private));path.chmod(0o600)
        prepared['assembly_receipt_ref']=receipt
        from assistant_kernel.services.business_budget import snapshot
        prepared['accumulated_budget_ref']=await snapshot(self.settings,self.repository,run)
        return path
