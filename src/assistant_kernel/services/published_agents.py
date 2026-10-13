"""DB-accepted immutable Agent packages, with explicit adoption scope.

Legacy files remain readable until a Project adopts a package. An existing Run
continues its own pinned snapshot; newer physical files never move its binding.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import uuid
import yaml
from dataclasses import replace

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from assistant_kernel.services.agent_files import AgentFileLoader
from assistant_kernel.services.contracts import canonical_json, contracts
from assistant_kernel.services.document_runtime import resource_ref, validate_resource_ref


class PublishedAgentService:
    def __init__(self,settings):
        self.settings=settings
        self.loader=AgentFileLoader(settings)

    def connection(self):
        s=self.settings
        return psycopg.connect(host=s.postgres_host,port=s.postgres_port,dbname=s.postgres_database,
            user=s.postgres_user,password=s.postgres_password.get_secret_value(),row_factory=dict_row)

    @staticmethod
    def key(project_id):return 'project-runtime-package:'+project_id

    def project_policy(self,project_id,*,task=None,task_id=None):
        if task_id is not None and task is None:
            with self.connection() as c:
                task=c.execute('SELECT * FROM assistant.runs WHERE run_id=%s',(task_id,)).fetchone()
            if task is None:raise ValueError('Task policy owner unavailable')
        if task is not None:
            if task['project_id']!=project_id:raise PermissionError('Task policy belongs to another Project')
            executor=task.get('executor_json') or {}
            if executor.get('task_runtime_package_ref'):
                return self.read_package(project_id,executor['task_runtime_package_ref'])['project_policy']
            if executor.get('project_policy_ref'):
                return self.read_policy_snapshot(project_id,executor['project_policy_ref'])
        with self.connection() as c:
            row=c.execute('SELECT value FROM assistant.kernel_meta WHERE key=%s',(self.key(project_id),)).fetchone()
        if row:
            return self.read_package(project_id,row['value']['package_ref'])['project_policy']
        path=self.loader.runtime.describe(project_id).vault_root/'project.yaml'
        value=yaml.safe_load(path.read_text('utf-8')) if path.is_file() else {}
        if value is not None and not isinstance(value,dict):raise ValueError('Project policy must be an object')
        return value or {}

    def policy_snapshot(self,project_id,policy):
        encoded=canonical_json(policy);digest=hashlib.sha256(encoded).hexdigest()
        relative=Path('runtime/project-policy-snapshots')/(digest+'.json')
        path=self.loader.runtime.ensure(project_id).vault_root/relative
        path.parent.mkdir(parents=True,exist_ok=True)
        if path.exists() and path.read_bytes()!=encoded:raise ValueError('Immutable policy snapshot differs')
        if not path.exists():
            temporary=path.with_suffix('.'+uuid.uuid4().hex+'.tmp');temporary.write_bytes(encoded);temporary.replace(path)
        return resource_ref(kind='source',identity='project-policy:'+digest,
            uri='vault://'+project_id+'/'+relative.as_posix(),sha256=digest,purpose='fixed effective policy for this Task')

    def read_policy_snapshot(self,project_id,ref):
        ref=validate_resource_ref(ref);prefix='vault://'+project_id+'/'
        if ref['kind']!='source' or not ref['uri'].startswith(prefix):raise PermissionError('Policy snapshot belongs to another Project')
        path=self.loader.runtime.resolve_vault_file(project_id,ref['uri'][len(prefix):]);encoded=path.read_bytes()
        if hashlib.sha256(encoded).hexdigest()!=ref['sha256']:raise ValueError('Policy snapshot hash differs')
        policy=json.loads(encoded)
        if not isinstance(policy,dict):raise ValueError('Policy snapshot is not an object')
        return policy

    def read_package(self,project_id,ref):
        ref=validate_resource_ref(ref)
        prefix='vault://'+project_id+'/'
        if ref['kind']!='source' or not str(ref['uri']).startswith(prefix):
            raise PermissionError('Published package belongs to another Project')
        root=self.loader.runtime.describe(project_id).vault_root
        path=self.loader.runtime.resolve_below(root,ref['uri'][len(prefix):])
        encoded=path.read_bytes()
        if hashlib.sha256(encoded).hexdigest()!=ref['sha256']:raise ValueError('Published package hash differs')
        package=json.loads(encoded)
        if package['schema_version']!='project-runtime-package.v1' or package['project_id']!=project_id:
            raise ValueError('Published package identity differs')
        return package

    def runtime_skills(self,project,config,package,task):
        """Select from the already-published public bundle using structured Task requirements."""
        from assistant_kernel.services.document_runtime import TaskDocumentService
        from assistant_kernel.services.template_bundle import TemplateBundle
        if not task:return config,[]
        documents=TaskDocumentService(self.settings);planned=[];sources=[];schedule_work=bool((task.get('request_json') or {}).get('schedule_actions'))
        if schedule_work:
            sources=[a['definition_ref'] for a in task['request_json']['schedule_actions']]
            # Keep ordinary read tools; this episode grants only schedule candidate/control writes.
            from assistant_kernel.services.audit import AUDIT_READ_ONLY_TOOLS
            config=replace(config,allowed_tools=tuple(t for t in config.allowed_tools if t in AUDIT_READ_ONLY_TOOLS or
                t in {'preview_schedule','list_schedules','schedule_status','list_schedule_ticks','write_runtime_document','control_schedule'}))
        for name,field in (('task_contract','runtime_requirements'),('solution_design','runtime_designs')):
            ref=(task.get('request_json') or {}).get('accepted_documents',{}).get(name)
            if ref:
                body=documents.read(project,ref)['payload'];items=body.get(field) or []
                if items:planned.extend(items);sources.append(ref)
                if body.get('schedule_plans') or body.get('schedule_requirements') or any(x.get('trigger_mode') in {'once','recurring','scheduled'} or x.get('execution_mode')=='scheduled' for x in items):
                    schedule_work=True
                    if ref not in sources:sources.append(ref)
        if not planned and not schedule_work:return config,[]
        conditions={'python_program_work'} if planned else set()
        if schedule_work:conditions.add('schedule_work')
        if any(x.get('lifecycle_kind')=='service' for x in planned):conditions.add('service_work')
        if any(x.get('workflow_framework')=='langgraph' for x in planned):conditions.add('langgraph_work')
        public=[x for x in config.skill_bindings if (x.get('published_ref') or {}).get('id','').startswith('v12-template/')]
        if not public:raise ValueError('Conditional Skills require their fixed published template source')
        bundle=TemplateBundle(Path(public[0]['source_root']).parents[1])
        if bundle.ref('profiles.json')!=package['template_ref']:raise PermissionError('Conditional Skill catalog belongs to another accepted bundle')
        names=[x['skill_name'] for x in bundle.profiles['profiles'][config.phase_mode]['conditional_skill_rules'] if x['condition'] in conditions]
        texts=list(config.skill_texts);bindings=list(config.skill_bindings);files=list(config.source_files)
        for name in names:
            if any(x['name']==name for x in bindings):continue
            relative='skills/'+name+'/SKILL.md';path=bundle.root/relative;ref=bundle.ref(relative)
            entries=[{'path':'SKILL.md','sha256':ref['sha256'],'size':path.stat().st_size}]
            texts.append(bundle.text(relative));files.append(str(path));bindings.append({'name':name,'source':'kernel',
                'bundle_hash':hashlib.sha256(canonical_json(entries)).hexdigest(),'files':entries,'source_root':str(path.parent),'published_ref':ref})
        result=replace(config,skill_texts=tuple(texts),skill_bindings=tuple(bindings),source_files=tuple(files))
        if result==config:return config,sources
        result=replace(result,config_sha256=hashlib.sha256(canonical_json({'base':config.config_sha256,'skill_bindings':bindings,'basis':sources})).hexdigest())
        return result,sources

    def prepare(self,project_id,*,configurations,template_ref,adoption_scope,source_refs,supervisor_binding_specs=None,project_policy=None):
        """Create an immutable candidate. No DB head or live files are changed."""
        contracts().validate('AdoptionScope',adoption_scope)
        if project_id not in adoption_scope['project_ids'] or not source_refs:
            raise ValueError('Package needs its exact Project adoption scope and sources')
        pinned={}
        for selector,config in configurations.items():
            from assistant_kernel.services.agent_files import without_retired_tools
            from assistant_kernel.services.kernel_management import require_active_identity
            require_active_identity(project_id, config.agent_key)
            config = without_retired_tools(config)
            if not config.agent_key.startswith('project:'+project_id+':') or config.foundation_revision!='1.2':
                raise ValueError('Candidate configuration actor or foundation differs')
            if selector!=config.agent_key+'/'+(config.phase_mode or 'default'):
                raise ValueError('Package selector differs from the actual actor and mode')
            if config.mcp_servers and not config.pinned_mcp_servers:
                from assistant_kernel.services.execution_plan import ProjectResourceResolver
                pinned_servers,bindings=ProjectResourceResolver(self.settings).resolve_mcp_servers(project_id,config,preparing=True)
                from assistant_kernel.services.mcp_discovery import validate_for_publication
                from assistant_kernel.services.project_credentials import ProjectCredentialsStore
                import os
                environment={**os.environ,**ProjectCredentialsStore(self.settings.data_root).environment(project_id)}
                discoveries=validate_for_publication(bindings,environment)
                by_id={item['server_id']:item for item in discoveries}
                pinned_servers=[{**item,'tool_schema_sha256':by_id[item['server_id']].get('tool_schema_sha256',{}),
                    'publication_discovery':by_id[item['server_id']]} for item in pinned_servers]
                config=replace(config,pinned_mcp_servers=tuple(pinned_servers),config_sha256=hashlib.sha256(
                    canonical_json({'configuration':config.config_sha256,'mcp_servers':pinned_servers})).hexdigest())
            uri,sha=self.loader.snapshot(project_id,config)
            pinned[selector]=resource_ref(kind='source',identity='agent-config:'+config.config_sha256,
                uri=uri,sha256=sha,purpose='fixed Agent/Role/Skill/MCP configuration')
        if not pinned:raise ValueError('Empty runtime package')
        package={'schema_version':'project-runtime-package.v1','project_id':project_id,
            'design_revision':'1.2','configurations':pinned,'template_ref':validate_resource_ref(template_ref),
            'adoption_scope':adoption_scope,'source_refs':[validate_resource_ref(x) for x in source_refs]}
        policy_path=self.loader.runtime.describe(project_id).vault_root/'project.yaml'
        package['project_policy']=(project_policy if project_policy is not None else
            yaml.safe_load(policy_path.read_text('utf-8')) if policy_path.is_file() else {})
        if not isinstance(package['project_policy'],dict):raise ValueError('Project policy must be an object')
        if 'resource_limits' in package['project_policy']:
            from assistant_kernel.services.runtime_admission import tighten_limits,LEGACY_LIMITS
            from assistant_kernel.services.system_config import SystemConfigService,STATE_KEY
            with self.connection() as c:state=c.execute('SELECT value FROM assistant.kernel_meta WHERE key=%s',(STATE_KEY,)).fetchone()
            limits=SystemConfigService(self.settings)._config(state['value']['desired_ref'])['resource_limits'] if state else LEGACY_LIMITS
            requested=package['project_policy']['resource_limits'];tighten_limits(limits,requested)
            if any(value is not None and limits[key] is not None and value>limits[key] for key,value in requested.items()):
                raise ValueError('Project resource policy cannot widen the current global limits')
        if any(config.kind == 'supervisor' for config in configurations.values()):
            from types import SimpleNamespace
            from assistant_kernel.services.goal_supervisor import GoalSupervisorService
            from assistant_kernel.services.supervisor_config import legacy_bindings
            baseline = GoalSupervisorService(self.settings, SimpleNamespace()).configuration(project_id)
            package['supervisor_config_bindings'] = (supervisor_binding_specs if supervisor_binding_specs is not None
                else legacy_bindings(baseline))
        encoded=canonical_json(package);digest=hashlib.sha256(encoded).hexdigest()
        root=self.loader.runtime.describe(project_id).vault_root
        relative=Path('runtime/published-agent-packages')/(digest+'.json');path=root/relative
        path.parent.mkdir(parents=True,exist_ok=True)
        if path.exists() and path.read_bytes()!=encoded:raise ValueError('Immutable package changed')
        if not path.exists():
            temporary=path.with_suffix('.'+uuid.uuid4().hex+'.tmp');temporary.write_bytes(encoded);temporary.replace(path)
        return resource_ref(kind='source',identity='agent-package:'+digest,
            uri='vault://'+project_id+'/'+relative.as_posix(),sha256=digest,purpose='candidate; requires explicit adoption')

    def adopt(self,project_id,*,package_ref,expected_previous_ref,approval_refs):
        """Internal approved publication. PM dispatch still binds its confirmed package."""
        if not approval_refs:raise PermissionError('Package adoption requires real approval sources')
        approval_refs=[validate_resource_ref(x) for x in approval_refs]
        package=self.read_package(project_id,package_ref)
        for ref in package['configurations'].values():
            self.loader.load_snapshot(project_id,ref['uri'],ref['sha256'])
        with self.connection() as c:
            c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(self.key(project_id),))
            prior=c.execute('SELECT value FROM assistant.kernel_meta WHERE key=%s FOR UPDATE',(self.key(project_id),)).fetchone()
            current=prior['value']['package_ref'] if prior else None
            if current==package_ref:return {**prior['value'],'duplicate':True}
            if current!=expected_previous_ref:raise ValueError('Published package head is stale')
            if current:
                previous_policy=self.read_package(project_id,current)['project_policy']
            else:
                policy_path=self.loader.runtime.describe(project_id).vault_root/'project.yaml'
                previous_policy=yaml.safe_load(policy_path.read_text('utf-8')) if policy_path.is_file() else {}
            preserved_policy_ref=self.policy_snapshot(project_id,previous_policy or {})
            actors=sorted({key.rsplit('/',1)[0] for key in package['configurations']})
            if c.execute("SELECT 1 FROM assistant.runs WHERE project_id=%s AND agent_key=ANY(%s) AND status IN ('running','reconciling') LIMIT 1",(project_id,actors)).fetchone():
                raise ValueError('Adopt the package at a safe boundary, after affected invocations settle')
            old_tasks=c.execute("""SELECT * FROM assistant.runs WHERE project_id=%s AND agent_key=%s
                AND run_type='chat' AND status IN ('queued','waiting_user','waiting_supervisor','blocked')
                AND NOT (executor_json ? 'project_policy_ref') AND NOT (executor_json ? 'task_runtime_package_ref') FOR UPDATE""",
                (project_id,'project:'+project_id+':manager')).fetchall()
            previous_package=self.read_package(project_id,current) if current else None
            for task in old_tasks:
                retained={};executor=task['executor_json'] or {}
                phase=(task['request_json'] or {}).get('current_stage') or 'requirements'
                for actor in sorted(set(actors)|{str(task['agent_key'])}):
                    existing_uri=executor.get('config_snapshot_uri') or executor.get('agent_config_snapshot_uri')
                    existing_sha=executor.get('config_bundle_hash') or executor.get('agent_snapshot_sha256')
                    if actor==task['agent_key'] and existing_uri and existing_sha:
                        uri,digest=existing_uri,existing_sha
                    else:
                        prior_config=(previous_package or {}).get('configurations',{}).get(actor+'/'+(phase if actor.endswith(':manager') else 'default'))
                        if prior_config:uri,digest=prior_config['uri'],prior_config['sha256']
                        else:
                            legacy=self.loader.load(project_id,actor)
                            if actor.endswith(':supervisor') and legacy.agent_key!=actor:
                                from types import SimpleNamespace
                                from assistant_kernel.services.goal_supervisor import GoalSupervisorService
                                public=GoalSupervisorService(self.settings,SimpleNamespace())
                                material={'base':legacy.config_sha256,'actor':actor,'role':public.role,'skill':public.skill}
                                legacy=replace(legacy,agent_id='supervisor',agent_key=actor,kind='supervisor',
                                    role_text=public.role,skill_texts=(public.skill,),skill_bindings=(),available_skills=(),
                                    config_sha256=hashlib.sha256(canonical_json(material)).hexdigest())
                            uri,digest=self.loader.snapshot(project_id,legacy)
                    retained[actor]={'uri':uri,'sha256':digest}
                c.execute('UPDATE assistant.runs SET executor_json=executor_json || %s WHERE run_id=%s',
                    (Jsonb({'project_policy_ref':preserved_policy_ref,'legacy_actor_snapshot_refs':retained}),task['run_id']))
            state={'package_ref':package_ref,'previous_ref':current,'approval_refs':approval_refs,
                   'adopted_at':c.execute('SELECT now() AS at').fetchone()['at'].isoformat()}
            c.execute('INSERT INTO assistant.kernel_meta(key,value) VALUES (%s,%s) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_at=now()',
                      (self.key(project_id),Jsonb(state)))
        # Project adoption also selects the single JSON Map writer. Legacy files
        # remain archived; a projection failure must not rewind accepted policy.
        from assistant_kernel.repositories.change_ledger import LEDGER_KEY
        with self.connection() as c:
            ledger=c.execute('SELECT value FROM assistant.kernel_meta WHERE key=%s',(LEDGER_KEY,)).fetchone()
        if ledger and ledger['value'].get('write_enabled') and ledger['value'].get('backfill_complete'):
            from assistant_kernel.services.semantic_map_v2 import SemanticMapV2
            from assistant_kernel.services.workspace_scope import WorkspaceScopeService
            try:
                roots=WorkspaceScopeService(self.settings).resolve(project_id,'00000000-0000-0000-0000-000000000000',None,shared_agreement=True)
                state['map_receipt']=SemanticMapV2(self.settings).refresh(project_id,roots=roots)
            except Exception as exc:
                state['map_refresh_pending']=type(exc).__name__
        return state

    async def pin_task_package_locked(self,connection,project_id,task_id,package_ref,*,expected_previous_ref):
        """Only the confirmed-turn compiler calls this at its locked safe boundary."""
        await connection.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(self.key(project_id),))
        package=self.read_package(project_id,package_ref)
        head=await (await connection.execute('SELECT value FROM assistant.kernel_meta WHERE key=%s',(self.key(project_id),))).fetchone()
        if not head or head['value']['package_ref']!=package_ref:
            raise PermissionError('Task binding must use the currently accepted package head')
        row=await (await connection.execute('SELECT * FROM assistant.runs WHERE run_id=%s FOR UPDATE',(task_id,))).fetchone()
        if not row or row['project_id']!=project_id:raise PermissionError('Package Task belongs to another Project')
        if row['status'] in {'running','reconciling','succeeded','cancelled','failed','dead_letter'}:
            raise PermissionError('Settle execution before binding a new Task package')
        current=(row['executor_json'] or {}).get('task_runtime_package_ref')
        if current!=expected_previous_ref:raise ValueError('Task package binding is stale')
        await connection.execute("UPDATE assistant.runs SET executor_json=executor_json || %s::jsonb WHERE run_id=%s",
            (Jsonb({'task_runtime_package_ref':package_ref}),task_id))
        return package

    async def foreground_package_locked(self,connection,task,*,prefer_accepted_head=False):
        """Recognize only a complete accepted Project protocol, never a partial template."""
        project=task['project_id'];fixed=(task.get('executor_json') or {}).get('task_runtime_package_ref')
        head=await (await connection.execute('SELECT value FROM assistant.kernel_meta WHERE key=%s',(self.key(project),))).fetchone()
        if not fixed and not head:return None
        ref=fixed or head['value']['package_ref'];package=self.read_package(project,ref)
        if prefer_accepted_head and head:
            proposed=self.read_package(project,head['value']['package_ref']);scope=proposed['adoption_scope']
            from datetime import datetime
            adopted=datetime.fromisoformat(head['value']['adopted_at'])
            eligible=str(task['run_id']) in scope['existing_task_ids'] or scope['new_tasks'] and task['created_at']>=adopted
            selectors=['project:'+project+':manager/'+phase for phase in ('requirements','design','build','audit','operate')]
            selectors.append('project:'+project+':supervisor/default')
            if eligible and all(selector in proposed['configurations'] for selector in selectors):
                ref=head['value']['package_ref'];package=proposed
        if not fixed:
            scope=package['adoption_scope']
            from datetime import datetime
            adopted=datetime.fromisoformat(head['value']['adopted_at'])
            if str(task['run_id']) not in scope['existing_task_ids'] and not (scope['new_tasks'] and task['created_at']>=adopted):return None
        selectors=['project:'+project+':manager/'+phase for phase in ('requirements','design','build','audit','operate')]
        selectors.append('project:'+project+':supervisor/default')
        if not all(selector in package['configurations'] for selector in selectors):return None
        for selector in selectors:
            config_ref=package['configurations'][selector]
            loaded=self.loader.load_snapshot(project,config_ref['uri'],config_ref['sha256'])
            if loaded.foundation_revision!='1.2' or loaded.agent_key!=selector.rsplit('/',1)[0]:
                raise PermissionError('Accepted foreground package mixes incompatible actor contracts')
        return ref,package

    def load(self,project_id,agent_key,*,task_id=None,phase=None,task_kind=None,requested_skills=None):
        """Resolve a fresh invocation; never replace a supplied pinned Run snapshot."""
        with self.connection() as c:
            head=c.execute('SELECT value FROM assistant.kernel_meta WHERE key=%s',(self.key(project_id),)).fetchone()
            if not head:return self.loader.load(project_id,agent_key,task_kind=task_kind,requested_skills=requested_skills)
            state=head['value'];package=self.read_package(project_id,state['package_ref'])
            if task_id:
                task=c.execute('SELECT project_id,created_at,executor_json FROM assistant.runs WHERE run_id=%s',(task_id,)).fetchone()
                if not task or task['project_id']!=project_id:raise PermissionError('Task is not owned by this package Project')
                fixed=(task['executor_json'] or {}).get('task_runtime_package_ref')
                if fixed:package=self.read_package(project_id,fixed)
                scope=package['adoption_scope']
                from datetime import datetime
                adopted_at=datetime.fromisoformat(state['adopted_at'])
                eligible=(bool(fixed) or str(task_id) in scope['existing_task_ids'] or scope['new_tasks'] and task['created_at']>=adopted_at)
                if not eligible:
                    executor=task['executor_json'] or {}
                    preserved=(executor.get('legacy_actor_snapshot_refs') or {}).get(agent_key) or {}
                    uri=preserved.get('uri') or ((executor.get('config_snapshot_uri') or executor.get('agent_config_snapshot_uri')) if agent_key.endswith(':manager') else None)
                    sha=preserved.get('sha256') or ((executor.get('config_bundle_hash') or executor.get('agent_snapshot_sha256')) if agent_key.endswith(':manager') else None)
                    if not uri or not sha:raise ValueError('Existing Task needs its preserved configuration; do not load newer live files')
                    loaded=self.loader.load_snapshot(project_id,uri,sha)
                    if loaded.agent_key!=agent_key:raise ValueError('Preserved Task configuration has a different actor')
                    return loaded
        selector=agent_key+'/'+('build' if phase=='repair' else phase or 'default')
        ref=package['configurations'].get(selector)
        if ref is None:raise ValueError('Adopted package lacks the required actor/mode; do not fall back to live files')
        loaded=self.loader.load_snapshot(project_id,ref['uri'],ref['sha256'])
        if set(requested_skills or [])-{x['name'] for x in loaded.skill_bindings}:
            raise ValueError('Requested Skills are not in the accepted package; prepare the required binding first')
        return loaded
