"""Project service control over Unit/Instance rows and the existing maintenance loop."""
import asyncio,hashlib,json,secrets,uuid
from copy import deepcopy
from datetime import datetime,timezone,timedelta
from pathlib import Path
from psycopg.types.json import Jsonb
from assistant_kernel.services.contracts import contracts,canonical_json
from assistant_kernel.services.document_runtime import resource_ref,same_resource_ref
from assistant_kernel.services.runtime_units import RuntimeUnits
from assistant_kernel.services.runtime_admission import RuntimeAdmission,resources_overlap,RuntimeCapacityUnavailable
from assistant_kernel.services.fixed_python import FixedPython,data_schema
from assistant_kernel.services.process_manager import ProcessManager
from assistant_kernel.services.v12_evidence import V12Evidence
from assistant_kernel.worker.python_process import inspect_python_group,cleanup_python_group

def now():return datetime.now(timezone.utc)
def stamp():return now().isoformat()

class ServiceRuntime:
    def __init__(self,settings,repository,manager=None):
        self.settings=settings;self.repository=repository;self.units=RuntimeUnits(settings,repository)
        self.documents=self.units.documents;self.fixed=FixedPython(settings);self.manager=manager or ProcessManager(settings)

    def root(self,row):
        return self.fixed.runtime.describe(row['project_id']).vault_root/'operations/service-instances'/str(row['instance_id'])

    def blob(self,project,kind,value):
        raw=canonical_json(value);digest=hashlib.sha256(raw).hexdigest()
        relative=Path('operations/runtime-evidence')/kind/(digest+'.json');path=self.fixed.runtime.describe(project).vault_root/relative
        path.parent.mkdir(parents=True,exist_ok=True)
        if path.exists() and path.read_bytes()!=raw:raise ValueError('Immutable runtime evidence changed')
        if not path.exists():path.write_bytes(raw)
        return resource_ref(kind='source',identity=kind+':'+digest,uri='vault://'+project+'/'+relative.as_posix(),sha256=digest,purpose=kind)

    def document(self,project,kind,payload,sources):
        identity=payload.get('instance_id') or payload.get('runtime_unit_id') or (payload.get('runtime_unit_ref') or {}).get('id') or payload.get('notification_id') or kind
        return self.documents.write_scoped_revision(project_id=project,scope='project',object_id=kind+'-'+hashlib.sha256(str(identity).encode()).hexdigest()[:24],
            document_kind=kind,document_schema=kind+'.v1',payload=payload,status='settled',actor_type='kernel',actor_id='service-runtime',
            reason='Actual registered service state and control',source_refs=sources)

    async def record_control_locked(self,c,unit,ref):
        from assistant_kernel.services.project_changes import ProjectChangeService
        prefix='vault://'+unit['project_id']+'/'
        await ProjectChangeService(self.settings,self.repository.database).capture_batch(unit['project_id'],batch_id='runtime-control:'+ref['sha256'],roots=[],
            changes=[{'target_uri':'workspace://project/'+ref['uri'].removeprefix(prefix),'document_ref':ref,'operation':'discovered',
                'before_sha256':None,'after_sha256':ref['sha256'],'purpose':'runtime-control-receipt.v1',
                'accepted_head_guard':{'kind':'unit_control','owner_id':unit['unit_id']}}],
            actor='kernel',reason='Accepted runtime control state',reason_source_refs=[ref],exact_time=False,scope_kind='artifact',connection=c)

    def instance_payload(self,row):
        state=row['state_json'];identity=state.get('process_identity') or {}
        return {'instance_id':str(row['instance_id']),'project_id':row['project_id'],'runtime_unit_id':row['unit_id'],'generation':row['generation'],
            'definition_ref':row['definition_ref'],'manager_program_key':state['manager_program_key'],'host_identity_ref':state['host_identity_ref'],
            'pid':identity.get('pid'),'process_start_ticks':identity.get('start_ticks'),'process_group_id':identity.get('pgid'),
            'observed_state':row['observed_state'],'last_probe_at':state.get('last_probe_at'),'readiness_ref':state.get('readiness_ref'),
            'cleanup_status':row['cleanup_status'],'cleanup_ref':state.get('cleanup_ref'),'receipt_refs':state.get('receipt_refs',[]),
            'resource_keys':state['resource_keys'],'resolved_config_ref':state['resolved_config_ref'],'code_bundle_ref':state['code_bundle_ref'],
            'work_control':state['work_control']}

    def snapshot(self,row):
        payload=self.instance_payload(row);contracts().validate('V12_ServiceInstance',payload)
        return self.document(row['project_id'],'service-instance',payload,[row['definition_ref']])

    def control_receipt(self,row,request,status,reason,instance=None):
        payload={'control_id':uuid.uuid5(uuid.NAMESPACE_URL,row['project_id']+':'+row['unit_id']+':'+request['idempotency_key']).hex,
          'runtime_unit_ref':row['effective_definition_ref'],'control_revision':row['control_revision'],'desired_state':row['desired_state'],
          'status':status,'adapter_step_refs':[],'instance_ref':self.snapshot(instance) if instance else None,
          'observed_state':instance['observed_state'] if instance else 'stopped','cleanup_status':instance['cleanup_status'] if instance else 'verified',
          'cleanup_ref':instance['state_json'].get('cleanup_ref') if instance else None,'evidence_refs':request['authorization_source_refs'],
          'error':None,'next_action':reason,'maintenance_receipt_ref':None,'resolved_config_ref':instance['state_json']['resolved_config_ref'] if instance else None}
        return self.document(row['project_id'],'runtime-control-receipt',payload,request['authorization_source_refs'])

    async def control_scheduled_locked(self,c,project,request,*,definition):
        """Internal capability: prove the pending Tick and exact command in this transaction."""
        tick=await (await c.execute('SELECT * FROM assistant.schedule_ticks WHERE tick_id=%s FOR UPDATE',(request['tick_ref']['id'],))).fetchone()
        if (not tick or tick['project_id']!=project or tick['settlement_status']!='pending'
                or tick['control_request']!=request or request['cause']!='scheduled'):
            raise PermissionError('Scheduled service command has no matching pending Tick')
        return await self.control(project,request,connection=c,_scheduled_definition=definition)

    async def control(self,project,request,*,authenticated_operator=False,connection=None,_scheduled_definition=None,_cancel_schedule_tick=None):
        contracts().validate('V12_RuntimeControlRequest',request)
        scheduled=_scheduled_definition is not None and connection is not None and request['cause']=='scheduled'
        if not scheduled and (not authenticated_operator or request['cause']!='manual'):raise PermissionError('Service control requires the actual manual Kernel authority')
        if request['expires_at'] and datetime.fromisoformat(request['expires_at'].replace('Z','+00:00'))<=now():raise PermissionError('Control expired')
        definition=_scheduled_definition if scheduled else self.units.read_definition(project,request['runtime_unit_ref'])
        if definition['lifecycle_kind']!='service':raise PermissionError('This is not a service')
        sources=request['authorization_source_refs']
        if not sources:raise PermissionError('Control requires actual authorization sources')
        if not scheduled:await V12Evidence(self.settings,self.repository).verify(project,sources)
        if request['action'] in {'start','resume','restart'} and not any(same_resource_ref(x,definition['approved_execution_scope_ref']) for x in sources):
            raise PermissionError('The approved service execution scope is missing')
        async with self.units.transaction(connection) as c:
            if _cancel_schedule_tick is not None:
                actual=await (await c.execute('SELECT * FROM assistant.schedule_ticks WHERE tick_id=%s',(_cancel_schedule_tick,))).fetchone()
                if (connection is None or not actual or actual['project_id']!=project or actual['settlement_status']!='pending'
                        or request['idempotency_key']!='schedule-cancel:'+str(actual['tick_id']) or request['action'] not in {'stop','retire'}):
                    raise PermissionError('Current-round cancellation needs its actual pending Tick')
            await c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('runtime-unit:'+project+':'+definition['runtime_unit_id'],))
            row=await (await c.execute('SELECT * FROM assistant.project_runtime_units WHERE project_id=%s AND unit_id=%s FOR UPDATE',(project,definition['runtime_unit_id']))).fetchone()
            if not row or not same_resource_ref(row['effective_definition_ref'],request['runtime_unit_ref']):raise PermissionError('Runtime Unit head changed')
            state=deepcopy(row['control_json']);digest=hashlib.sha256(canonical_json(request)).hexdigest();old=state.setdefault('commands',{}).get(request['idempotency_key'])
            if old:
                if old['request_sha256']!=digest:raise ValueError('Control key reused for different request')
                return old['receipt_ref']
            if row['control_revision']!=request['expected_control_revision']:raise PermissionError('Stale service control revision')
            if row['desired_state']=='retired' and request['action']!='retire':raise PermissionError('Retired service cannot resume')
            if not scheduled and _cancel_schedule_tick is None and request['action'] in {'stop','pause','retire','update'}:
                from assistant_kernel.services.schedule_support import pause_unit_schedules
                await pause_unit_schedules(c,project,definition['runtime_unit_id'],'manual_unit_'+request['action'])
            instance=await (await c.execute("SELECT * FROM assistant.service_instances WHERE project_id=%s AND unit_id=%s ORDER BY generation DESC LIMIT 1 FOR UPDATE",(project,row['unit_id']))).fetchone()
            if request['expected_instance_ref'] and (not instance or self.documents.read(project,request['expected_instance_ref'])['payload']['instance_id']!=str(instance['instance_id'])):
                raise PermissionError('Expected service generation differs')
            busy=bool(instance and instance['state_json']['work_control']['items'])
            action=request['action'];reason='Control accepted; existing maintenance consumer applies the manager step'
            skipped=action=='restart' and busy
            if action=='update':
                if busy:raise PermissionError('Finish or explicitly cancel old work and answers before updating its definition')
                target=self.units.read_definition(project,request['target_definition_ref'])
                if target['runtime_unit_id']!=row['unit_id'] or target['lifecycle_kind']!='service':raise PermissionError('Update changes Unit identity')
                design=self.documents.read(project,target['design_ref']);task=await self.repository.get_run(design.get('task_id'))
                accepted=(task['request_json'].get('accepted_documents') or {}) if task else {}
                await self.units.reviewed_definition(project,request['target_definition_ref'],accepted.get('review_report'),accepted.get('publication'))
                row['effective_definition_ref']=request['target_definition_ref'];row['resource_policy_ref']=target['resource_policy_ref']
            if not skipped:
                if not scheduled:
                    row['desired_state']={'start':'running','resume':'running','restart':'running','stop':'stopped','pause':'paused','retire':'retired','update':'stopped'}[action]
                state['pending_control']=request
                if action in {'start','resume','restart'}:row['enable_authorization_ref']=definition['approved_execution_scope_ref']
                elif action=='update':row['enable_authorization_ref']=None
                if not scheduled and instance and action in {'stop','pause','retire','update','restart'}:
                    istate=deepcopy(instance['state_json']);istate['work_control'].update(gate='maintenance' if action=='restart' else 'closed',gate_revision=row['control_revision']+1)
                    await c.execute('UPDATE assistant.service_instances SET state_json=%s,updated_at=now() WHERE instance_id=%s',(Jsonb(istate),instance['instance_id']));instance['state_json']=istate
            else:reason='Manual maintenance skipped: active I/O, CPU, model work or waiting input; no deferred restart queued'
            row['control_revision']+=1
            ref=self.control_receipt(row,request,'settled' if skipped else 'accepted',reason,instance)
            if not scheduled and not skipped:
                from assistant_kernel.services.schedule_support import installed
                if await installed(c):
                    # A manual override owns completion of an already accepted control.
                    # Keep its Tick waiting for actual cleanup/readiness, not an orphaned old revision.
                    await c.execute("""UPDATE assistant.schedule_ticks SET control_request=%s,control_receipt_ref=%s,
                        reason='superseded_by_manual_control' WHERE project_id=%s AND settlement_status='pending'
                        AND control_request->'runtime_unit_ref'=%s AND control_request->>'idempotency_key'<>%s""",
                        (Jsonb(request),Jsonb(ref),project,Jsonb(request['runtime_unit_ref']),request['idempotency_key']))
            if instance and not skipped and not scheduled:
                instance['state_json']['work_control']['control_ref']=ref
                await c.execute("UPDATE assistant.service_instances SET state_json=jsonb_set(state_json,'{work_control,control_ref}',%s::jsonb) WHERE instance_id=%s",(Jsonb(ref),instance['instance_id']))
            state['commands'][request['idempotency_key']]={'request_sha256':digest,'receipt_ref':ref}
            await c.execute('UPDATE assistant.project_runtime_units SET effective_definition_ref=%s,desired_state=%s,control_revision=%s,enable_authorization_ref=%s,resource_policy_ref=%s,control_json=%s,last_control_receipt_ref=%s,updated_at=now() WHERE project_id=%s AND unit_id=%s',
                (Jsonb(row['effective_definition_ref']),row['desired_state'],row['control_revision'],Jsonb(row['enable_authorization_ref']) if row['enable_authorization_ref'] else None,
                 Jsonb(row['resource_policy_ref']) if row['resource_policy_ref'] else None,Jsonb(state),Jsonb(ref),project,row['unit_id']))
            await self.record_control_locked(c,row,ref)
            if skipped:await self.notify_locked(c,project,'maintenance-skipped:'+ref['sha256'],'维护重启已跳过：服务仍有工作或等待回答。',[ref])
            return ref

    async def claim_scheduled(self,unit):
        """The shared admission transaction is the start boundary for service actions."""
        project=unit['project_id'];request=unit['control_json']['pending_control']
        async with self.units.transaction() as c:
            row=await (await c.execute('SELECT * FROM assistant.project_runtime_units WHERE project_id=%s AND unit_id=%s FOR UPDATE',
                (project,unit['unit_id']))).fetchone()
            state=deepcopy(row['control_json'])
            if row['control_revision']!=unit['control_revision'] or state.get('pending_control')!=request:return None
            command=state['commands'][request['idempotency_key']]
            if command.get('schedule_started_at'):return row
            tick=await (await c.execute('SELECT * FROM assistant.schedule_ticks WHERE tick_id=%s FOR UPDATE',(request['tick_ref']['id'],))).fetchone()
            if not tick or tick['project_id']!=project or tick['control_request']!=request:
                raise PermissionError('Service control has no actual matching scheduled Tick')
            schedule=await (await c.execute('SELECT * FROM assistant.project_schedules WHERE project_id=%s AND schedule_id=%s',
                (project,tick['schedule_id']))).fetchone()
            states={'enabled','exhausted'} if tick['origin']=='scheduled' else {'disabled','enabled','paused','exhausted'}
            reason=None;outcome='cancelled'
            if (tick['settlement_status']!='pending' or not schedule or schedule['state'] not in states
                    or schedule['control_revision']!=tick['control_revision']):reason='cancelled_before_start'
            instance=await (await c.execute('SELECT * FROM assistant.service_instances WHERE project_id=%s AND unit_id=%s ORDER BY generation DESC LIMIT 1 FOR UPDATE',
                (project,unit['unit_id']))).fetchone()
            if not reason and (instance and (instance['state_json']['work_control']['items'] or instance['observed_state'] in {'unknown','starting','stopping','degraded'})):
                reason='service_busy_or_unavailable_before_start';outcome='skipped'
            if not reason and request['action']=='restart' and (not instance or instance['observed_state']!='ready'):
                reason='maintenance_requires_ready_service';outcome='skipped'
            if reason:
                state.pop('pending_control',None);command['schedule_outcome']=outcome
                ref=self.control_receipt(row,request,'settled',reason,instance);command['receipt_ref']=ref
                await c.execute('UPDATE assistant.schedule_ticks SET reason=%s,control_receipt_ref=%s WHERE tick_id=%s',(reason,Jsonb(ref),tick['tick_id']))
                await c.execute('UPDATE assistant.project_runtime_units SET control_json=%s,last_control_receipt_ref=%s,updated_at=now() WHERE project_id=%s AND unit_id=%s',
                    (Jsonb(state),Jsonb(ref),project,unit['unit_id']))
                await self.record_control_locked(c,row,ref)
                return None
            command['schedule_started_at']=(await (await c.execute('SELECT clock_timestamp() AS value')).fetchone())['value'].isoformat()
            row['desired_state']='stopped' if request['action']=='stop' else 'running'
            if instance and request['action'] in {'stop','restart'}:
                istate=deepcopy(instance['state_json'])
                istate['work_control'].update(gate='maintenance' if request['action']=='restart' else 'closed',
                    gate_revision=row['control_revision'],control_ref=command['receipt_ref'])
                await c.execute('UPDATE assistant.service_instances SET state_json=%s,updated_at=now() WHERE instance_id=%s',(Jsonb(istate),instance['instance_id']))
            row['control_json']=state
            await c.execute('UPDATE assistant.project_runtime_units SET desired_state=%s,control_json=%s,updated_at=now() WHERE project_id=%s AND unit_id=%s',
                (row['desired_state'],Jsonb(state),project,unit['unit_id']))
            return row

    async def notify_locked(self,c,project,key,text,refs):
        from assistant_kernel.services.runtime_notifications import RuntimeNotifications
        route=await RuntimeNotifications(self.settings,self.repository).pm_route(c,project)
        await self.repository._create_outbound_locked(c,project_id=project,session_id=None,run_id=None,
          channel=route[0] if route else 'internal',chat_id=route[1] if route else 'pending-project-pm',user_id=route[2] if route else None,
          message_type='reply',content={'interaction_kind':'service_runtime','pending_pm_route':not bool(route),'text':text,'evidence_refs':refs},
          reply_to_message_id=None,idempotency_key=key)

    async def get(self,identity,c=None,lock=False):
        if c is None:
            async with self.repository.database.connection() as connection:return await self.get(identity,connection,lock)
        return await (await c.execute('SELECT * FROM assistant.service_instances WHERE instance_id=%s'+(' FOR UPDATE' if lock else ''),(identity,))).fetchone()

    def owner(self,row,logical=None):
        state=row['state_json'];definition=self.units.read_definition(row['project_id'],row['definition_ref'])
        return {'service_instance_id':str(row['instance_id']),'service_instance_ref':state.get('instance_ref'),
            'run_id':str(row['instance_id']),'project_id':row['project_id'],'attempt':row['generation'],
            'request_json':{'manifest_ref':definition['implementation_manifest_ref'],'runtime_unit_ref':row['definition_ref'],'definition_ref':row['definition_ref'],
                'logical_execution_id':logical or 'service:'+str(row['instance_id']),'runtime_resource_policy_ref':definition['resource_policy_ref'],
                'runtime_resources':self.manifest(row)['resources'],'input':{}},
            'executor_json':{}}

    def manifest(self,row):
        if row['state_json'].get('effective_manifest'):return deepcopy(row['state_json']['effective_manifest'])
        definition=self.units.read_definition(row['project_id'],row['definition_ref'])
        return self.fixed.read_manifest(row['project_id'],definition['implementation_manifest_ref'])

    async def resolve_configuration(self,row,definition,manifest):
        from assistant_kernel.services.system_config import SystemConfigService
        from assistant_kernel.config.system_config import resolve_config_bindings
        service=SystemConfigService(self.settings);state=await service.state()
        bindings=[{key:item[key] for key in ('path','mode','override_value')} for item in definition['config_bindings']]
        if not bindings:return manifest
        owner=resource_ref(kind='receipt',identity=str(row['instance_id']),uri='db://assistant/service_instances/'+str(row['instance_id']),purpose='actual service generation configuration')
        resolved=resolve_config_bindings(service._config(state['desired_ref']),state['desired_ref'],bindings,owner_ref=owner,captured_at=stamp(),effective_group_refs=[])
        effective=deepcopy(manifest)
        for path,value in resolved['selected_values'].items():
            if path.startswith('/service_defaults/recovery/'):
                effective['recovery'][path.rsplit('/',1)[1]]=value
            elif path in {'/service_defaults/startup_timeout_seconds','/service_defaults/stop_grace_seconds'}:effective['resources'][path.rsplit('/',1)[1]]=value
            elif path=='/service_defaults/heartbeat_seconds':effective['health']['heartbeat_interval_seconds']=value
            elif path=='/service_defaults/stale_after_seconds':effective['health']['stale_after_seconds']=value
            else:raise ValueError('Service binding has no declared consumer: '+path)
        ref=self.documents.write_scoped_revision(project_id=row['project_id'],scope='project',object_id='service-config-'+str(row['instance_id']),
            document_kind='resolved-config-snapshot',document_schema='resolved-system-config.v1',payload=resolved,status='settled',
            actor_type='kernel',actor_id='service-runtime',reason='Resolve only this generation declared inheritance/overrides',source_refs=[state['desired_ref']])
        effective['resolved_config_ref']=ref;effective['resources']['resolved_config_ref']=ref
        row['state_json'].update(effective_manifest=effective,resolved_config_ref=ref,config_source_ref=state['desired_ref'])
        return effective

    async def allocate(self,unit,definition,manifest):
        if manifest['resources']['max_model_cost_per_window_ref'] is not None:raise PermissionError('Installed adapters cannot guarantee a hard service money cap')
        resource_policy=manifest['resources']['resource_policy_ref']
        if resource_policy and not same_resource_ref(resource_policy,definition['resource_policy_ref']):raise PermissionError('Service and Unit budget policies differ')
        project=unit['project_id'];identity=str(uuid.uuid4());admission=RuntimeAdmission(self.settings)
        async with self.repository.database.connection() as c:
            await admission.lock(c)
            fresh=await (await c.execute('SELECT * FROM assistant.project_runtime_units WHERE project_id=%s AND unit_id=%s FOR UPDATE',(project,unit['unit_id']))).fetchone()
            if fresh['desired_state']!='running' or fresh['control_revision']!=unit['control_revision']:return None
            occupied=await (await c.execute("SELECT * FROM assistant.service_instances WHERE cleanup_status<>'verified'")).fetchall()
            if any(x['project_id']==project and x['unit_id']==unit['unit_id'] for x in occupied):return None
            limits,_,config_ref=await admission.configuration_locked(c);local,_=await admission.project_limits_locked(c,project,limits)
            if not config_ref:raise PermissionError('Services require accepted system configuration')
            if limits['max_declared_memory_mb'] is not None or local['max_declared_memory_mb'] is not None:raise PermissionError('Service has no declared memory bound for the configured hard admission limit')
            if len(occupied)>=limits['max_service_instances'] or sum(x['project_id']==project for x in occupied)>=local['max_service_instances']:return None
            keys=sorted(set(manifest['shared_resource_keys']+definition['shared_resource_keys']+self.fixed.resource_keys(project,manifest)))
            finite=await admission.occupied_locked(c)
            if any(resources_overlap(keys,x['state_json']['resource_keys']) for x in occupied):return None
            if any(resources_overlap(keys,(x['request_json'] or {}).get('resource_keys',[])) or keys and x['mutex_key']=='shared-write:global' for x in finite):return None
            latest=await (await c.execute('SELECT COALESCE(max(generation),0) AS generation FROM assistant.service_instances WHERE project_id=%s AND unit_id=%s',(project,unit['unit_id']))).fetchone()
            host=self.blob(project,'service-host',{'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip()})
            state={'manager_program_key':self.manager.key(identity),'host_identity_ref':host,'resource_keys':keys,
              'resolved_config_ref':manifest['resources']['resolved_config_ref'],'code_bundle_ref':manifest['code_bundle']['bundle_ref'],
              'work_control':{'gate':'closed','gate_revision':unit['control_revision'],'control_ref':unit['last_control_receipt_ref'],'items':{}},
              'work_records':{},'cpu_reservations':{},'control_revision':unit['control_revision'],'started_at':stamp(),
              'receipt_refs':[],'last_probe_at':None,'ready_event':None}
            row=await (await c.execute("INSERT INTO assistant.service_instances(instance_id,project_id,unit_id,generation,definition_ref,observed_state,cleanup_status,state_json) VALUES(%s,%s,%s,%s,%s,'starting','pending',%s) RETURNING *",
              (identity,project,unit['unit_id'],latest['generation']+1,Jsonb(unit['effective_definition_ref']),Jsonb(state)))).fetchone()
            await c.commit();return row

    async def launch(self,row,definition):
        project=row['project_id'];root=self.root(row);root.mkdir(parents=True,exist_ok=True)
        definition_manifest=self.fixed.read_manifest(project,definition['implementation_manifest_ref'])
        effective=await self.resolve_configuration(row,definition,definition_manifest)
        row['state_json']['instance_ref']=self.snapshot(row)
        async with self.repository.database.connection() as c:
            await c.execute('UPDATE assistant.service_instances SET state_json=%s WHERE instance_id=%s',(Jsonb(row['state_json']),row['instance_id']));await c.commit()
        owner=self.owner(row);prepared=self.fixed.prepare(run=owner,authorization_verified=True,attempt_root=root)
        prepared['manifest']=effective;prepared['resources']=effective['resources']
        async with self.repository.database.connection() as c:
            await c.execute('UPDATE assistant.service_instances SET state_json=state_json || %s WHERE instance_id=%s',
                (Jsonb({'output_root':str(prepared['business_output_root'])}),row['instance_id']));await c.commit()
        prepared['definition']=definition;(root/'input.json').write_bytes(canonical_json({}))
        from assistant_kernel.services.business_runtime import BusinessRuntime
        client=await BusinessRuntime(self.settings,self.repository).assemble(owner,prepared)
        from assistant_kernel.worker.python_environment import python_environment,business_credentials
        environment=python_environment(owner,root);credentials,_=business_credentials(self.settings.data_root,project,prepared['manifest']['io']['secret_refs']);environment.update(credentials)
        environment.update(ASSISTANT_RUN_ID=str(row['instance_id']),ASSISTANT_ATTEMPT=str(row['generation']),ASSISTANT_RUNTIME_CLIENT_PATH=str(client),
          ASSISTANT_INPUT_PATH=str(root/'input.json'),ASSISTANT_RESULT_PATH=str(root/'result.json'),ASSISTANT_OUTPUT_ROOT=str(prepared['business_output_root']))
        launch={'instance_id':str(row['instance_id']),'generation':row['generation'],'command':[str(prepared['interpreter']),str(prepared['source']),*prepared['args']],
            'cwd':str(prepared['cwd']),'environment':environment,'stop_grace_seconds':prepared['resources']['stop_grace_seconds']}
        path=root/'launch.json';path.write_bytes(canonical_json(launch));path.chmod(0o600)
        await self.manager.ensure(str(row['instance_id']),path,prepared['resources'])
        # Recheck desired state immediately before the manager side effect.
        async with self.repository.database.connection() as c:
            current=await (await c.execute('SELECT * FROM assistant.project_runtime_units WHERE project_id=%s AND unit_id=%s',(project,row['unit_id']))).fetchone()
        if current['desired_state']!='running' or current['control_revision']!=row['state_json']['control_revision']:
            await self.finish_instance(row,{'status':'verified','reason':'launch overtaken before process start'},'stopped');return
        await V12Evidence(self.settings,self.repository).verify(project,definition['approved_execution_scope_ref'])
        async with self.repository.database.connection() as c:
            await c.execute("UPDATE assistant.service_instances SET state_json=state_json || %s WHERE instance_id=%s",(Jsonb({'start_attempted':True}),row['instance_id']));await c.commit()
        await self.manager.start(self.manager.key(row['instance_id']))

    async def finish_instance(self,row,cleanup,observed,*,cancel_work=False):
        ref=self.blob(row['project_id'],'service-cleanup',{'instance_id':str(row['instance_id']),'generation':row['generation'],**cleanup})
        async with self.repository.database.connection() as c:
            live=await self.get(row['instance_id'],c,True);state=deepcopy(live['state_json']);state.update(cleanup_ref=ref)
            state['work_control']['gate']='closed'
            pending=await (await c.execute("SELECT 1 FROM assistant.model_invocations WHERE metadata_json->>'service_instance_id'=%s AND (status='running' OR metadata_json->>'outcome_status'='unknown') LIMIT 1",(str(row['instance_id']),))).fetchone()
            unresolved=any(effect['status']!='succeeded' for work in state['work_records'].values() for effect in work.get('runtime_effects',{}).values())
            if (pending or unresolved) and cleanup['status']=='verified':cleanup={**cleanup,'status':'unknown','reason':'managed calls or effects remain unresolved'}
            if cancel_work and cleanup['status']=='verified':
                questions=__import__('assistant_kernel.services.question_groups',fromlist=['QuestionGroupService']).QuestionGroupService(self.settings,self.repository)
                for logical in state['work_control']['items']:
                    record=state['work_records'][logical];record['status']='cancelled';record['control_ref']=state['work_control']['control_ref']
                    if record.get('question_group_id'):
                        group=await (await c.execute('SELECT content_json FROM assistant.messages WHERE message_id=%s FOR UPDATE',(record['question_group_id'],))).fetchone()
                        wrapper=deepcopy(group['content_json']);wrapper['group']['status']='cancelled';wrapper['group']['draft']['revision']+=1
                        wrapper['closure_control_ref']=state['work_control']['control_ref']
                        await questions.write_group_locked(c,record['question_group_id'],wrapper)
                state['work_control']['items']={};state['cpu_reservations']={}
            await c.execute('UPDATE assistant.service_instances SET state_json=%s,observed_state=%s,cleanup_status=%s,updated_at=now() WHERE instance_id=%s',
                (Jsonb(state),observed,cleanup['status'],row['instance_id']));await c.commit()

    async def failure_notice(self,row,manifest,exit_record):
        root=self.root(row);stderr=root/'stderr.log';captured=root/'stderr-captured.log'
        if not captured.exists():captured.write_bytes(stderr.read_bytes()[-manifest['resources']['log_limit_bytes']:] if stderr.exists() else b'')
        relative=captured.relative_to(self.fixed.runtime.describe(row['project_id']).vault_root)
        stderr_ref=resource_ref(kind='source',identity='service-stderr:'+str(row['instance_id']),uri='vault://'+row['project_id']+'/'+relative.as_posix(),
            sha256=hashlib.sha256(captured.read_bytes()).hexdigest(),purpose='bounded captured service stderr')
        key='service-failure:'+str(row['instance_id']);code=(exit_record or {}).get('exit_code');error=(exit_record or {}).get('error_type') or 'ServiceExited'
        payload={'notification_id':key,'kind':'script_failure','project_id':row['project_id'],'script_name':manifest['service_id'],
            'runtime_unit_ref':row['definition_ref'],'run_ref':None,'attempt':None,'instance_ref':self.snapshot(row),
            'occurred_at':(exit_record or {}).get('ended_at') or stamp(),'captured_error':error,'returncode':code,'controlled_stop':False,
            'stderr_ref':stderr_ref,'error_excerpt':error+'; exit='+str(code)+'; cleanup='+row['cleanup_status'],
            'dedup_key':key,'target_role':'pm','delivery_state_owner':'messages_outbox'}
        ref=self.document(row['project_id'],'script-failure-notice',payload,[stderr_ref])
        async with self.repository.database.connection() as c:
            await self.notify_locked(c,row['project_id'],key,'常驻程序异常退出，已保存日志：'+payload['error_excerpt'],[ref]);await c.commit()
        from assistant_kernel.services.operation_records import OperationRecords
        async with self.repository.database.connection() as c:
            await OperationRecords(self.settings,self.repository).record_event_locked(c,project=row['project_id'],
                event_key='service-failure:'+row['unit_id']+':'+str(row['state_json']['control_revision']),scene='service_exit',source_ref=ref,
                target_ref=row['definition_ref'],occurred_at=payload['occurred_at'],summary=payload['error_excerpt']);await c.commit()

    async def probe(self,row,manifest):
        root=self.root(row);state=deepcopy(row['state_json']);identity_path=root/'process.json'
        if identity_path.exists():state['process_identity']=json.loads(identity_path.read_text())
        identity=state.get('process_identity');group=inspect_python_group(identity);state['last_probe_at']=stamp()
        exited=json.loads((root/'exit.json').read_text()) if (root/'exit.json').exists() else None
        ready=False;detail={'group':group}
        if group['status']=='active':
            policy=manifest['health'];config=json.loads(self.fixed.source_path(row['project_id'],policy['probe_config_ref'],self.fixed.scopes.resolve(row['project_id'],str(row['instance_id']),'build')).read_text())
            if policy['probe_kind']=='runtime_events':
                event=state.get('ready_event');ready=bool(event and (now()-datetime.fromisoformat(event)).total_seconds()<=policy['stale_after_seconds'])
            elif policy['probe_kind']=='heartbeat_file':
                relative=__import__('assistant_kernel.services.fixed_python',fromlist=['safe_relative']).safe_relative(config['relative_path'])
                path=root/str(relative);ready=path.is_file() and now().timestamp()-path.stat().st_mtime<=policy['stale_after_seconds']
                if ready:
                    data=json.loads(path.read_text());ready=data.get('instance_id')==str(row['instance_id']) and data.get('ready') is True
            elif policy['probe_kind']=='http':
                import httpx
                async with httpx.AsyncClient(timeout=min(5,policy['readiness_timeout_seconds'])) as client:
                    response=await client.get(config['url']);ready=response.status_code==config.get('status_code',200)
                    if 'expected_json' in config:ready=ready and response.json()==config['expected_json']
            elif policy['probe_kind']=='command':
                from assistant_kernel.worker.process_control import run_managed_process
                launch=json.loads((root/'launch.json').read_text());ref=config['entrypoint_ref']
                probe=self.fixed.source_path(row['project_id'],ref,self.fixed.scopes.resolve(row['project_id'],str(row['instance_id']),'build'))
                code,_,_,_=await run_managed_process(launch['command'][0],str(probe),*config.get('args',[]),cwd=launch['cwd'],env=launch['environment'],timeout_seconds=min(10,policy['readiness_timeout_seconds']))
                ready=code==0
        detail.update(ready=ready,checked_at=state['last_probe_at'])
        if ready and (not state.get('readiness_ref') or row['observed_state']!='ready'):
            state['readiness_ref']=self.blob(row['project_id'],'service-probe',detail)
        if ready and not state.get('ready_since'):state['ready_since']=stamp()
        observed='ready' if ready else 'degraded' if group['status']=='active' and row['observed_state']=='ready' else row['observed_state']
        async with self.repository.database.connection() as c:
            fresh=await self.get(row['instance_id'],c,True);merged=deepcopy(fresh['state_json'])
            for key in ('process_identity','last_probe_at','readiness_ref','ready_since'):
                if key in state:merged[key]=state[key]
            unit=await (await c.execute('SELECT desired_state,control_revision,control_json,effective_definition_ref FROM assistant.project_runtime_units WHERE project_id=%s AND unit_id=%s',(row['project_id'],row['unit_id']))).fetchone()
            pending=(unit['control_json'].get('pending_control') or {}) if unit else {}
            admitted=bool(unit and unit['desired_state']=='running' and same_resource_ref(unit['effective_definition_ref'],row['definition_ref']) and
                (unit['control_revision']==merged['control_revision'] or pending.get('action') in {'start','resume'}))
            if ready and admitted and merged['work_control']['gate']=='closed':
                merged['work_control']['gate']='open';merged['work_control']['gate_revision']=unit['control_revision'];merged['control_revision']=unit['control_revision']
            await c.execute('UPDATE assistant.service_instances SET state_json=%s,observed_state=%s,updated_at=now() WHERE instance_id=%s',
                (Jsonb(merged),observed,row['instance_id']));await c.commit()
        return group,ready,exited

    async def reconcile(self):
        async with self.repository.database.connection() as c:
            if not (await (await c.execute("SELECT to_regclass('assistant.service_instances') AS name")).fetchone())['name']:return
            units=await (await c.execute('SELECT * FROM assistant.project_runtime_units ORDER BY updated_at')).fetchall()
        for unit in units:
            try:
                definition=self.units.read_definition(unit['project_id'],unit['effective_definition_ref'])
                if definition['lifecycle_kind']!='service':continue
                await self.reconcile_unit(unit,definition)
            except RuntimeCapacityUnavailable:continue
            except Exception as exc:
                # Preserve a durable fact; never turn an unknown start into a duplicate.
                async with self.repository.database.connection() as c:
                    await self.notify_locked(c,unit['project_id'],'service-control-error:'+unit['project_id']+':'+unit['unit_id']+':'+str(unit['control_revision']),
                        '常驻控制尚未完成：'+type(exc).__name__+'。保留当前实例及日志，未自动重放未知操作。',[unit['effective_definition_ref']]);await c.commit()

    async def reconcile_unit(self,unit,definition):
        if (unit['control_json'].get('pending_control') or {}).get('cause')=='scheduled':
            unit=await self.claim_scheduled(unit)
            if unit is None:return
        async with self.repository.database.connection() as c:
            instance=await (await c.execute("SELECT * FROM assistant.service_instances WHERE project_id=%s AND unit_id=%s ORDER BY generation DESC LIMIT 1",(unit['project_id'],unit['unit_id']))).fetchone()
        control=unit['control_json'].get('pending_control') or {};action=control.get('action')
        if instance and instance['cleanup_status']!='verified':
            old_definition=self.units.read_definition(unit['project_id'],instance['definition_ref']);manifest=self.manifest(instance)
            group,ready,exited=await self.probe(instance,manifest);instance=await self.get(instance['instance_id'])
            stopping=unit['desired_state']!='running' or action in {'restart','update'} and instance['state_json']['control_revision']!=unit['control_revision']
            if stopping and (not instance['state_json']['work_control']['items'] or control.get('stop_policy')=='cancel'):
                async with self.repository.database.connection() as c:
                    await c.execute("UPDATE assistant.service_instances SET observed_state='stopping' WHERE instance_id=%s",(instance['instance_id'],));await c.commit()
                await self.manager.stop(instance['state_json']['manager_program_key'])
                cleanup=await cleanup_python_group(instance['state_json'].get('process_identity'),manifest['resources']['stop_grace_seconds'])
                if not instance['state_json'].get('process_identity') and not (self.root(instance)/'process.json').exists():cleanup={'status':'unknown','reason':'no verified process identity; reconcile manager outcome'}
                await self.finish_instance(instance,cleanup,'stopped' if cleanup['status']=='verified' else 'unknown',cancel_work=control.get('stop_policy')=='cancel')
                return
            if group['status']=='verified':
                cleanup=group
                await self.finish_instance(instance,cleanup,'failed' if not exited or not exited['controlled_stop'] else 'stopped')
                if not exited or not exited['controlled_stop']:await self.failure_notice(await self.get(instance['instance_id']),manifest,exited)
                if control:await self.settle_control(unit,control,await self.get(instance['instance_id']),status='failed')
                return
            if not ready and (now()-instance['created_at']).total_seconds()>manifest['resources']['startup_timeout_seconds']:
                # A living but unhealthy process is not blindly restarted.
                async with self.repository.database.connection() as c:
                    await self.notify_locked(c,unit['project_id'],'service-not-ready:'+str(instance['instance_id']),'常驻实例尚未通过就绪检查；现有进程保留，未另起实例。',[self.snapshot(instance)]);await c.commit()
            if ready and not stopping and action in {'start','resume','restart'}:await self.settle_control(unit,control,instance)
            if ready and unit['control_json'].get('recovery_attempts') and instance['state_json'].get('ready_since') and (now()-datetime.fromisoformat(instance['state_json']['ready_since'])).total_seconds()>=manifest['recovery']['stable_window_seconds']:
                async with self.repository.database.connection() as c:
                    await c.execute("UPDATE assistant.project_runtime_units SET control_json=jsonb_set(control_json,'{recovery_attempts}','0'::jsonb) WHERE project_id=%s AND unit_id=%s AND control_revision=%s",
                        (unit['project_id'],unit['unit_id'],unit['control_revision']));await c.commit()
            return
        if unit['desired_state']!='running':
            if instance and instance['state_json']['work_control']['items'] and control.get('stop_policy')=='cancel':
                await self.finish_instance(instance,{'status':'verified','reason':'already verified process cleanup'},instance['observed_state'],cancel_work=True)
                instance=await self.get(instance['instance_id'])
            if unit['desired_state']=='retired' and instance and instance['cleanup_status']=='verified' and not instance['state_json'].get('manager_removed'):
                await self.manager.remove(instance['state_json']['manager_program_key'])
                async with self.repository.database.connection() as c:
                    await c.execute('UPDATE assistant.service_instances SET state_json=state_json || %s WHERE instance_id=%s',(Jsonb({'manager_removed':True}),instance['instance_id']));await c.commit()
                instance=await self.get(instance['instance_id'])
            if control:await self.settle_control(unit,control,instance)
            return
        manifest=self.fixed.read_manifest(unit['project_id'],definition['implementation_manifest_ref'])
        if instance and instance['state_json']['work_control']['items']:
            async with self.repository.database.connection() as c:
                await self.notify_locked(c,unit['project_id'],'service-unfinished-work:'+str(instance['instance_id']),
                    '旧常驻实例仍有未完成工作或待答，暂不另起进程；请核对原工作，或明确取消后再启动。',[self.snapshot(instance)]);await c.commit()
            return # Waiting/unknown work and effects survive crashes; explicit reconciliation is required.
        if instance and action not in {'restart','start','resume'}:
            policy=manifest['recovery'];state=deepcopy(unit['control_json']);events=state.get('recovery_events',[])
            events=[x for x in events if (now()-datetime.fromisoformat(x)).total_seconds()<policy['window_seconds']]
            episode=state.get('recovery_attempts',0)
            if episode>=policy['max_restarts'] or len(events)>=policy['max_restarts_per_window']:
                async with self.repository.database.connection() as c:
                    await RuntimeAdmission.lock(c)
                    await c.execute("UPDATE assistant.project_runtime_units SET desired_state='paused',updated_at=now() WHERE project_id=%s AND unit_id=%s AND control_revision=%s",(unit['project_id'],unit['unit_id'],unit['control_revision']))
                    from assistant_kernel.services.schedule_support import pause_unit_schedules
                    await pause_unit_schedules(c,unit['project_id'],unit['unit_id'],'service_recovery_limit')
                    await self.notify_locked(c,unit['project_id'],'service-recovery-limit:'+str(instance['instance_id']),'常驻恢复次数已达上限，已暂停并保留原故障记录。',[self.snapshot(instance)]);await c.commit()
                return
            delay=policy['backoff_seconds'][min(episode,len(policy['backoff_seconds'])-1)] if policy['backoff_seconds'] else 1
            if (now()-instance['updated_at']).total_seconds()<delay:return
            await V12Evidence(self.settings,self.repository).verify(unit['project_id'],policy['authorization_ref'])
            state.update(recovery_attempts=episode+1,recovery_events=events+[stamp()])
            async with self.repository.database.connection() as c:
                await c.execute('UPDATE assistant.project_runtime_units SET control_json=%s WHERE project_id=%s AND unit_id=%s AND control_revision=%s',(Jsonb(state),unit['project_id'],unit['unit_id'],unit['control_revision']));await c.commit()
        row=await self.allocate(unit,definition,manifest)
        if row:
            try:await asyncio.wait_for(self.launch(row,definition),manifest['resources']['startup_timeout_seconds'])
            except Exception:
                current=await self.get(row['instance_id']);attempted=current['state_json'].get('start_attempted')
                async with self.repository.database.connection() as c:
                    await c.execute('UPDATE assistant.service_instances SET observed_state=%s,cleanup_status=%s WHERE instance_id=%s',
                        ('unknown' if attempted else 'failed','unknown' if attempted else 'verified',row['instance_id']));await c.commit()
                if control:await self.settle_control(unit,control,await self.get(row['instance_id']),status='failed')
                raise

    async def settle_control(self,unit,request,instance,*,status='settled'):
        async with self.repository.database.connection() as c:
            row=await (await c.execute('SELECT * FROM assistant.project_runtime_units WHERE project_id=%s AND unit_id=%s FOR UPDATE',(unit['project_id'],unit['unit_id']))).fetchone()
            if row['control_revision']!=unit['control_revision']:return
            state=deepcopy(row['control_json']);state.pop('pending_control',None)
            ref=self.control_receipt(row,request,status,'Actual manager state, readiness and cleanup recorded',instance)
            state['commands'][request['idempotency_key']]['receipt_ref']=ref
            await c.execute('UPDATE assistant.project_runtime_units SET control_json=%s,last_control_receipt_ref=%s,updated_at=now() WHERE project_id=%s AND unit_id=%s',
                (Jsonb(state),Jsonb(ref),unit['project_id'],unit['unit_id']))
            await self.record_control_locked(c,row,ref);await c.commit()
