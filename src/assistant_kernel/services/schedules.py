"""Project-owned schedules. Only this service may turn a due time into existing work."""
from copy import deepcopy
from datetime import datetime,timezone
import hashlib
import uuid

from psycopg.types.json import Jsonb

from assistant_kernel.services.contracts import contracts,canonical_json
from assistant_kernel.services.document_runtime import TaskDocumentService,resource_ref,same_resource_ref
from assistant_kernel.services.runtime_units import RuntimeUnits
from assistant_kernel.services.runtime_admission import RuntimeAdmission
from assistant_kernel.services.project_python import ProjectPython
from assistant_kernel.services.schedule_time import instant,next_time,preview
from assistant_kernel.services.schedule_support import cancel_unstarted
from assistant_kernel.services.v12_evidence import V12Evidence

UTC=timezone.utc
DEFAULTS={'enabled':False,'poll_seconds':1,'late_grace_seconds':5,'batch_size':100}
TERMINAL={'succeeded','failed','cancelled','dead_letter'}


def iso(value):return value.isoformat() if value is not None else None
def tick_ref(identity):return resource_ref(kind='receipt',identity=str(identity),uri='db://assistant/schedule_ticks/'+str(identity),purpose='actual Kernel schedule occurrence')


class Schedules:
    def __init__(self,settings,repository):
        self.settings=settings;self.repository=repository;self.documents=TaskDocumentService(settings)
        self.units=RuntimeUnits(settings,repository);self.python=ProjectPython(settings,repository)

    async def operator(self,project,task_id,sources):
        task=await self.repository.get_run(str(task_id));state=(task or {}).get('request_json') or {}
        confirmation=state.get('prompt_confirmation_message_id')
        if (not task or task['project_id']!=project or task['agent_key']!='project:'+project+':manager'
                or task['status']!='running' or task.get('cancel_requested_at') or state.get('current_stage')!='operate'
                or not confirmation or not state.get('confirmed_turn_package_ref')):
            raise PermissionError('Schedule changes require the actual confirmed Operate invocation')
        if not any(x.get('uri')=='db://assistant/messages/'+confirmation for x in sources):
            raise PermissionError('The current user confirmation is missing')
        message=await self.repository.get_message(confirmation)
        if not message or message['project_id']!=project or message['direction']!='inbound':
            raise PermissionError('Schedule authority is not an actual same-Project user message')
        await V12Evidence(self.settings,self.repository).verify(project,sources)
        if state.get('schedule_actions'):
            from assistant_kernel.services.prompt_protocol import PromptProtocol
            package=PromptProtocol(self.settings,self.repository).read_blob(project,state['confirmed_turn_package_ref'])
            draft=self.documents.read(project,package['prompt_ref'])['payload']['draft']
            if draft.get('schedule_actions')!=state['schedule_actions']:raise PermissionError('Daily Schedule scope differs from the confirmed Prompt')
        return task

    def definition(self,project,ref):
        document=self.documents.read(project,ref)
        if document['schema_version']!='kernel-schedule-definition.v1' or document.get('scope')!='project':
            raise PermissionError('A fixed Project schedule definition is required')
        value=document['payload'];contracts().validate('KernelScheduleDefinition',value)
        if value['project_id']!=project:raise PermissionError('Schedule belongs to another Project')
        unit=self.units.read_definition(project,value['runtime_unit_ref'])
        if (value['action']=='run')!=(unit['lifecycle_kind']=='finite'):
            raise ValueError('Schedule action and RuntimeUnit lifecycle differ')
        if value['action']!='run' and value['inputs']:
            raise ValueError('Service controls use their fixed manifest inputs, not schedule inputs')
        if not unit['approved_execution_scope_ref']:raise PermissionError('Target has no approved execution scope')
        return deepcopy(value),unit

    async def prepare(self,project,definition_ref,*,future=True):
        definition,unit=self.definition(project,definition_ref)
        if future:preview(definition['rule'],definition['timezone'])
        await V12Evidence(self.settings,self.repository).verify(project,[definition['source_refs'],unit['approved_execution_scope_ref']])
        async with self.repository.database.connection() as c:
            row=await (await c.execute('SELECT * FROM assistant.project_runtime_units WHERE project_id=%s AND unit_id=%s',
                (project,unit['runtime_unit_id']))).fetchone()
        if not row or not same_resource_ref(row['effective_definition_ref'],definition['runtime_unit_ref']) or row['desired_state']=='retired':
            raise PermissionError('Schedule target is not its registered effective RuntimeUnit')
        if definition['action']=='run':
            from assistant_kernel.services.fixed_python import data_schema
            manifest=self.python.fixed.read_manifest(project,unit['implementation_manifest_ref'])
            data_schema(definition['inputs'],manifest['io']['input_schema'])
            if len(canonical_json(definition['inputs']))>manifest['resources']['max_input_bytes']:raise ValueError('Scheduled input exceeds its approved bound')
        return definition,unit

    @staticmethod
    def registration(row):
        value={k:row[k] for k in ('project_id','schedule_id','definition_ref','unit_id','state','control_revision','authorization_refs')}
        value.update(next_fire_at=iso(row['next_fire_at']),last_tick_id=str(row['last_tick_id']) if row['last_tick_id'] else None,
            last_control_receipt_ref=row['control_json'].get('last_control_receipt_ref'))
        contracts().validate('ScheduleRegistration',value);return value

    async def locked(self,c,project,schedule_id):
        row=await (await c.execute('SELECT * FROM assistant.project_schedules WHERE project_id=%s AND schedule_id=%s FOR UPDATE',(project,schedule_id))).fetchone()
        if not row:raise FileNotFoundError('Schedule does not exist in this Project')
        return row

    def receipt(self,row,action,sources,occurrence=None):
        value={'project_id':row['project_id'],'schedule_id':row['schedule_id'],'action':action,'control_revision':row['control_revision'],
            'state':row['state'],'definition_ref':row['definition_ref'],'next_fire_at':iso(row['next_fire_at']),
            'authorization_source_refs':sources,'tick_ref':occurrence,'recorded_at':datetime.now(UTC).isoformat()}
        return self.documents.write_scoped_revision(project_id=row['project_id'],scope='project',object_id='schedule-control-'+hashlib.sha256(row['schedule_id'].encode()).hexdigest()[:24],
            document_kind='schedule-control-receipt',document_schema='schedule-control-receipt.v1',payload=value,status='settled',
            actor_type='kernel',actor_id='schedule-controller',reason='Actual authorized schedule control',source_refs=sources+[row['definition_ref']])

    async def record_control_locked(self,c,row,ref,definition=None):
        from assistant_kernel.services.project_changes import ProjectChangeService
        prefix='vault://'+row['project_id']+'/'
        await ProjectChangeService(self.settings,self.repository.database).capture_batch(row['project_id'],batch_id='schedule-control:'+ref['sha256'],roots=[],
            changes=[{'target_uri':'workspace://project/'+ref['uri'].removeprefix(prefix),'document_ref':ref,'operation':'discovered',
                'before_sha256':None,'after_sha256':ref['sha256'],'purpose':'schedule-control-receipt.v1',
                'accepted_head_guard':{'kind':'schedule_control','owner_id':row['schedule_id']}}],
            actor='kernel',reason='Accepted Schedule control and fixed target',reason_source_refs=[ref,row['definition_ref']]+([definition['runtime_unit_ref']] if definition else []),
            exact_time=False,scope_kind='artifact',connection=c)

    async def register(self,project,definition_ref,*,task_id,authorization_source_refs,idempotency_key,expected_control_revision=0):
        task=await self.operator(project,task_id,authorization_source_refs)
        if task['request_json'].get('schedule_actions'):raise PermissionError('Daily control cannot register new schedules or programs')
        contracts().validate('Args_register_schedule',{'definition_ref':definition_ref,'authorization_source_refs':authorization_source_refs,
            'idempotency_key':idempotency_key,'expected_control_revision':expected_control_revision})
        digest=hashlib.sha256(canonical_json([definition_ref,authorization_source_refs])).hexdigest()
        candidate,_=self.definition(project,definition_ref)
        async with self.repository.database.connection() as c:
            previous=await (await c.execute('SELECT * FROM assistant.project_schedules WHERE project_id=%s AND schedule_id=%s',(project,candidate['schedule_id']))).fetchone()
        if previous:
            replay=previous['control_json'].get('commands',{}).get(idempotency_key)
            if replay and replay['digest']==digest:return self.registration(previous)
            raise ValueError('Schedule exists; use its versioned control interface')
        definition,unit=await self.prepare(project,definition_ref)
        async with self.repository.database.connection() as c:
            await RuntimeAdmission.lock(c)
            target=await (await c.execute('SELECT * FROM assistant.project_runtime_units WHERE project_id=%s AND unit_id=%s FOR UPDATE',(project,unit['runtime_unit_id']))).fetchone()
            if not target or not same_resource_ref(target['effective_definition_ref'],definition['runtime_unit_ref']):raise PermissionError('RuntimeUnit changed before registration')
            old=await (await c.execute('SELECT * FROM assistant.project_schedules WHERE project_id=%s AND schedule_id=%s FOR UPDATE',(project,definition['schedule_id']))).fetchone()
            if old:
                command=old['control_json'].get('commands',{}).get(idempotency_key)
                if not command or command['digest']!=digest:raise ValueError('Schedule exists; use its versioned control interface')
                return self.registration(old)
            row=await (await c.execute("""INSERT INTO assistant.project_schedules(project_id,schedule_id,definition_ref,unit_id,authorization_refs)
                VALUES(%s,%s,%s,%s,%s) RETURNING *""",(project,definition['schedule_id'],Jsonb(definition_ref),unit['runtime_unit_id'],Jsonb(authorization_source_refs)))).fetchone()
            receipt=self.receipt(row,'register',authorization_source_refs)
            row['control_json']={'commands':{idempotency_key:{'digest':digest,'receipt_ref':receipt}},'last_control_receipt_ref':receipt}
            await c.execute('UPDATE assistant.project_schedules SET control_json=%s WHERE project_id=%s AND schedule_id=%s',(Jsonb(row['control_json']),project,row['schedule_id']))
            await self.record_control_locked(c,row,receipt,definition)
            await c.commit()
        return self.registration(row)

    async def control(self,project,request,*,task_id):
        contracts().validate('ScheduleControlRequest',request)
        sources=request['authorization_source_refs'];task=await self.operator(project,task_id,sources)
        from assistant_kernel.services.schedule_operations import action_for,verify_update
        operation=action_for(task,request['schedule_id'])
        if operation and (operation['action']!=request['action'] or operation['expected_control_revision']!=request['expected_control_revision']):
            raise PermissionError('Schedule command differs from the confirmed daily operation')
        async with self.repository.database.connection() as c:
            observed=await (await c.execute('SELECT * FROM assistant.project_schedules WHERE project_id=%s AND schedule_id=%s',(project,request['schedule_id']))).fetchone()
        if not observed:raise FileNotFoundError('Schedule does not exist')
        digest=hashlib.sha256(canonical_json(request)).hexdigest()
        fulfilled=(task['request_json'].get('schedule_action_receipts') or {}).get(request['schedule_id']) if operation else None
        if fulfilled and not any(same_resource_ref(x['receipt_ref'],fulfilled) and x['digest']==digest for x in observed['control_json'].get('commands',{}).values()):
            raise PermissionError('This confirmed daily action already completed; replay its original command or confirm a new operation')
        replay=observed['control_json'].get('commands',{}).get(request['idempotency_key'])
        if replay:
            if replay['digest']!=digest:raise ValueError('Schedule command key reused with different parameters')
            return self.documents.read(project,replay['receipt_ref'])['payload']
        action=request['action'];new_ref=request['target_definition_ref']
        if (action=='update')!=(new_ref is not None):raise ValueError('Only update takes a target definition')
        stopping=action in {'pause','retire','cancel_current'}
        definition=unit=None
        if not stopping:
            definition,unit=await self.prepare(project,new_ref or observed['definition_ref'],future=action in {'update','enable','resume'})
            if definition['schedule_id']!=request['schedule_id']:raise ValueError('Update cannot change Schedule identity')
        if action=='update':
            old,_=self.definition(project,observed['definition_ref'])
            verify_update(task,old,definition)
            if not same_resource_ref(old['runtime_unit_ref'],definition['runtime_unit_ref']) or old['action']!=definition['action']:
                raise PermissionError('Changing executable target/action requires a separately reviewed and registered schedule')
        prepared=None
        if action=='run_now' and definition['action']=='run':
            prepared=await self.prepare_execution(project,observed,definition,unit,'manual:'+request['idempotency_key'])
        async with self.repository.database.connection() as c:
            await RuntimeAdmission.lock(c)
            target=await (await c.execute('SELECT * FROM assistant.project_runtime_units WHERE project_id=%s AND unit_id=%s FOR UPDATE',(project,observed['unit_id']))).fetchone()
            row=await self.locked(c,project,request['schedule_id'])
            previous=row['control_json'].get('commands',{}).get(request['idempotency_key'])
            if previous:
                if previous['digest']!=digest:raise ValueError('Schedule command key reused with different parameters')
                return self.documents.read(project,previous['receipt_ref'])['payload']
            if row['control_revision']!=request['expected_control_revision']:raise PermissionError('Schedule control revision is stale')
            if row['state']=='retired' and action!='cancel_current':raise PermissionError('Retired schedules cannot be controlled; use a new identity')
            if not stopping and not same_resource_ref(target['effective_definition_ref'],definition['runtime_unit_ref']):raise PermissionError('Target definition changed')
            now=(await (await c.execute('SELECT clock_timestamp() AS value')).fetchone())['value']
            occurrence=None
            if action=='run_now':
                occurrence=await self.dispatch_locked(c,row,definition,unit,now,origin='manual',manual_key=request['idempotency_key'],prepared=prepared)
            elif action=='cancel_current':
                await self.cancel_current_locked(c,row,sources,target,unit)
                row=await self.locked(c,project,request['schedule_id'])
            else:
                if action=='update':row['definition_ref']=new_ref
                if action in {'enable','resume'} or action=='update' and row['state']=='enabled':
                    if definition['action']=='run' and target['desired_state']!='running':raise PermissionError('Enable the finite RuntimeUnit before its schedule')
                    if target['desired_state'] in {'paused','retired'}:raise PermissionError('Explicitly restore the RuntimeUnit before enabling its schedules')
                    row['state']='enabled';row['next_fire_at']=next_time(definition['rule'],definition['timezone'],now)
                    if row['next_fire_at'] is None:raise ValueError('Choose a future schedule time')
                elif action in {'pause','retire'}:row['state']='paused' if action=='pause' else 'retired';row['next_fire_at']=None
                elif action=='update':row['next_fire_at']=None
                await cancel_unstarted(c,project,row['schedule_id'])
                row['control_revision']+=1;row['authorization_refs']=sources
            receipt=self.receipt(row,action,sources,tick_ref(occurrence['tick_id']) if occurrence else None)
            state=deepcopy(row['control_json']);state.setdefault('commands',{})[request['idempotency_key']]={'digest':digest,'receipt_ref':receipt};state['last_control_receipt_ref']=receipt
            await c.execute('''UPDATE assistant.project_schedules SET definition_ref=%s,state=%s,control_revision=%s,authorization_refs=%s,
                next_fire_at=%s,control_json=%s,updated_at=now() WHERE project_id=%s AND schedule_id=%s''',
                (Jsonb(row['definition_ref']),row['state'],row['control_revision'],Jsonb(row['authorization_refs']),row['next_fire_at'],Jsonb(state),project,row['schedule_id']))
            await self.record_control_locked(c,row,receipt,definition)
            if operation:
                await c.execute("UPDATE assistant.runs SET request_json=jsonb_set(request_json,'{schedule_action_receipts}',COALESCE(request_json->'schedule_action_receipts','{}'::jsonb) || %s) WHERE run_id=%s",
                    (Jsonb({row['schedule_id']:receipt}),task_id))
            await c.commit()
        return self.documents.read(project,receipt)['payload']

    async def cancel_current_locked(self,c,row,sources,target,unit):
        ticks=await (await c.execute("SELECT * FROM assistant.schedule_ticks WHERE project_id=%s AND schedule_id=%s AND settlement_status='pending'",(row['project_id'],row['schedule_id']))).fetchall()
        for tick in ticks:
            if tick['run_id']:
                await c.execute("""UPDATE assistant.runs SET cancel_requested_at=COALESCE(cancel_requested_at,now()),
                    status=CASE WHEN status IN ('queued','waiting_user','blocked') THEN 'cancelled' ELSE status END,
                    ended_at=CASE WHEN status IN ('queued','waiting_user','blocked') THEN now() ELSE ended_at END,updated_at=now()
                    WHERE run_id=%s AND status NOT IN ('succeeded','failed','cancelled','dead_letter')""",(tick['run_id'],))
            elif tick['control_request']:
                from assistant_kernel.services.service_runtime import ServiceRuntime
                command={**tick['control_request'],'runtime_unit_ref':target['effective_definition_ref'],'cause':'manual','tick_ref':None,
                    'action':'retire' if target['desired_state']=='retired' else 'stop','stop_policy':'cancel',
                    'idempotency_key':'schedule-cancel:'+str(tick['tick_id']),'expected_control_revision':target['control_revision'],
                    'expected_instance_ref':None,'expires_at':None,'authorization_source_refs':sources}
                ref=await ServiceRuntime(self.settings,self.repository).control(row['project_id'],command,authenticated_operator=True,connection=c,_cancel_schedule_tick=tick['tick_id'])
                await c.execute("UPDATE assistant.schedule_ticks SET control_request=%s,control_receipt_ref=%s,reason='cancel_requested' WHERE tick_id=%s",(Jsonb(command),Jsonb(ref),tick['tick_id']))

    async def prepare_execution(self,project,row,definition,unit,key):
        return await self.python.prepare_submission(project,{'manifest_ref':unit['implementation_manifest_ref'],'inputs':definition['inputs'],
            'idempotency_key':'schedule:'+row['schedule_id']+':'+key,'execution_kind':'formal','runtime_unit_ref':definition['runtime_unit_ref'],
            'logical_execution_id':None,'resume_owner':None})

    async def dispatch_locked(self,c,row,definition,unit,scheduled_for,*,origin='scheduled',manual_key=None,prepared=None,skip_reason=None):
        project=row['project_id'];sid=row['schedule_id'];key='schedule:'+project+':'+sid+':'+(origin+':'+manual_key if origin=='manual' else instant(scheduled_for).isoformat())
        old=await (await c.execute('SELECT * FROM assistant.schedule_ticks WHERE idempotency_key=%s',(key,))).fetchone()
        if old:return old
        pending=await (await c.execute("SELECT 1 FROM assistant.schedule_ticks WHERE project_id=%s AND schedule_id=%s AND settlement_status='pending' LIMIT 1",(project,sid))).fetchone()
        reason=skip_reason or ('previous_occurrence_unsettled' if pending else None)
        target=await (await c.execute('SELECT * FROM assistant.project_runtime_units WHERE project_id=%s AND unit_id=%s FOR UPDATE',(project,row['unit_id']))).fetchone()
        if not same_resource_ref(target['effective_definition_ref'],definition['runtime_unit_ref']):reason=reason or 'target_version_changed'
        if target['desired_state'] in {'paused','retired'}:reason=reason or 'target_not_enabled'
        instance=None
        if definition['action']!='run':
            instance=await (await c.execute('SELECT * FROM assistant.service_instances WHERE project_id=%s AND unit_id=%s ORDER BY generation DESC LIMIT 1 FOR UPDATE',(project,row['unit_id']))).fetchone()
            if target['control_json'].get('pending_control'):reason=reason or 'service_control_pending'
            if instance and instance['state_json']['work_control']['items']:reason=reason or 'service_busy_or_waiting'
            if instance and instance['observed_state'] in {'unknown','starting','stopping','degraded'}:reason=reason or 'service_state_unavailable'
            if definition['action']=='restart' and (target['desired_state']!='running' or not instance or instance['observed_state']!='ready'):reason=reason or 'maintenance_requires_ready_service'
            if definition['action']=='start' and instance and instance['observed_state']=='ready':reason=reason or 'already_running'
            if definition['action']=='stop' and (not instance or instance['cleanup_status']=='verified'):reason=reason or 'already_stopped'
        elif target['desired_state']!='running':reason=reason or 'target_not_enabled'
        tick=await (await c.execute('''INSERT INTO assistant.schedule_ticks(tick_id,project_id,schedule_id,origin,scheduled_for_utc,
            definition_ref,control_revision,idempotency_key,disposition,reason,settlement_status)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *''',
            (uuid.uuid4(),project,sid,origin,scheduled_for,Jsonb(row['definition_ref']),row['control_revision'],key,
                'skipped' if reason else 'submitted',reason,'skipped' if reason else 'pending'))).fetchone()
        if not reason and definition['action']=='run':
            if prepared is None:raise RuntimeError('Finite schedule needs its prepared fixed submission')
            run,_=await self.python.submit_locked(c,project,prepared,schedule_binding={'schedule_id':sid,'tick_id':str(tick['tick_id']),'control_revision':row['control_revision']})
            await c.execute('UPDATE assistant.schedule_ticks SET run_id=%s WHERE tick_id=%s',(run['run_id'],tick['tick_id']));tick['run_id']=run['run_id']
        elif not reason:
            from assistant_kernel.services.service_runtime import ServiceRuntime
            service=ServiceRuntime(self.settings,self.repository)
            command={'runtime_unit_ref':definition['runtime_unit_ref'],'action':definition['action'],'expected_control_revision':target['control_revision'],
                'idempotency_key':key,'authorization_source_refs':row['authorization_refs']+[unit['approved_execution_scope_ref']],
                'target_definition_ref':None,'stop_policy':'drain','cause':'scheduled','tick_ref':tick_ref(tick['tick_id']),
                'expected_instance_ref':service.snapshot(instance) if instance else None,'expires_at':None}
            await c.execute('UPDATE assistant.schedule_ticks SET control_request=%s WHERE tick_id=%s',(Jsonb(command),tick['tick_id']))
            ref=await service.control_scheduled_locked(c,project,command,definition=unit)
            await c.execute('UPDATE assistant.schedule_ticks SET control_receipt_ref=%s WHERE tick_id=%s',(Jsonb(ref),tick['tick_id']));tick['control_receipt_ref']=ref
        if reason:await self.notify_locked(c,tick)
        await c.execute('UPDATE assistant.project_schedules SET last_tick_id=%s,updated_at=now() WHERE project_id=%s AND schedule_id=%s',(tick['tick_id'],project,sid))
        return tick

    async def notify_locked(self,c,tick):
        from assistant_kernel.services.runtime_notifications import RuntimeNotifications
        key='schedule-result:'+str(tick['tick_id']);route=await RuntimeNotifications(self.settings,self.repository).pm_route(c,tick['project_id'])
        payload={k:v for k,v in self.tick_view({**tick,'settled_at':tick.get('settled_at') or datetime.now(UTC)}).items()
                 if k not in {'run_status','started_at','delivery_status'}}
        receipt=self.documents.write_scoped_revision(project_id=tick['project_id'],scope='project',object_id='tick-'+str(tick['tick_id']),
            document_kind='schedule-tick',document_schema='schedule-tick.v1',payload=payload,status='settled',actor_type='kernel',actor_id='scheduler',
            reason='Actual occurrence outcome; delivery tracked independently',source_refs=[tick_ref(tick['tick_id']),tick['definition_ref']])
        body={'interaction_kind':'schedule_result','pending_pm_route':not bool(route),'tick_ref':tick_ref(tick['tick_id']),'tick_receipt_ref':receipt,
            'text':f"定时任务 {tick['schedule_id']}：{tick['settlement_status']}\n计划时间：{iso(tick['scheduled_for_utc'])}\n"+
                (f"原因：{tick['reason']}\n" if tick['reason'] else '')+f"Tick：{tick['tick_id']}",
            'schedule_id':tick['schedule_id'],'result':tick['settlement_status']}
        message=await self.repository._create_outbound_locked(c,project_id=tick['project_id'],session_id=None,run_id=str(tick['run_id']) if tick.get('run_id') else None,
            channel=route[0] if route else 'internal',chat_id=route[1] if route else 'pending-project-pm',user_id=route[2] if route else None,
            message_type='reply',content=body,reply_to_message_id=None,idempotency_key=key)
        await c.execute('UPDATE assistant.schedule_ticks SET notification_id=%s,settled_at=COALESCE(settled_at,now()) WHERE tick_id=%s',(message['message_id'],tick['tick_id']))
        from assistant_kernel.services.project_changes import ProjectChangeService
        from assistant_kernel.services.operation_records import OperationRecords
        prefix='vault://'+tick['project_id']+'/'
        await ProjectChangeService(self.settings,self.repository.database).capture_batch(tick['project_id'],batch_id='schedule-tick:'+str(tick['tick_id']),roots=[],
            changes=[{'target_uri':'workspace://project/'+receipt['uri'].removeprefix(prefix),'document_ref':receipt,'operation':'discovered',
                'before_sha256':None,'after_sha256':receipt['sha256'],'purpose':'schedule-tick.v1',
                'accepted_head_guard':{'kind':'schedule_tick','owner_id':str(tick['tick_id'])}}],
            actor='kernel',reason='Schedule occurrence and actual execution outcome',reason_source_refs=[receipt,tick['definition_ref']],
            exact_time=False,scope_kind='artifact',connection=c)
        await OperationRecords(self.settings,self.repository).record_event_locked(c,project=tick['project_id'],event_key=key,scene='schedule_execution',
            source_ref=receipt,target_ref=tick['definition_ref'],occurred_at=payload['settled_at'],summary=body['text'],metric='schedule_occurrence')

    async def list(self,project,limit=20,cursor=None):
        contracts().validate('Args_list_schedules',{'limit':limit,'cursor':cursor})
        async with self.repository.database.connection() as c:
            rows=await (await c.execute('SELECT * FROM assistant.project_schedules WHERE project_id=%s AND (%s::text IS NULL OR schedule_id>%s) ORDER BY schedule_id LIMIT %s',(project,cursor,cursor,limit+1))).fetchall()
        from assistant_kernel.services.scheduler import Scheduler
        from assistant_kernel.services.channel_health import read_channel_health
        config,config_ref=await Scheduler(self.settings,self.repository).configuration()
        health=read_channel_health(self.settings,'scheduler')
        result={'project_id':project,'items':[self.registration(x) for x in rows[:limit]],'next_cursor':rows[limit-1]['schedule_id'] if len(rows)>limit else None,
            'as_of':datetime.now(UTC).isoformat(),'scheduler':{'enabled':config['enabled'],'poll_seconds':config['poll_seconds'],
                'late_grace_seconds':config['late_grace_seconds'],'connected':bool(health.get('connected')),
                'process_running':bool(health.get('process_running')),'stale':bool(health.get('stale')),'last_scan_at':health.get('last_scan_at'),
                'configuration_ref':config_ref}}
        return result

    async def status(self,project,sid):
        async with self.repository.database.connection() as c:
            row=await (await c.execute('SELECT * FROM assistant.project_schedules WHERE project_id=%s AND schedule_id=%s',(project,sid))).fetchone()
        if not row:raise FileNotFoundError('Schedule does not exist')
        error=None
        try:definition,_=self.definition(project,row['definition_ref'])
        except (ValueError,PermissionError,FileNotFoundError,KeyError) as exc:definition=None;error=type(exc).__name__
        return {'registration':self.registration(row),'definition':definition,'definition_error':error,'control_reason':row['control_json'].get('pause_reason'),
            'recent_ticks':(await self.ticks(project,sid,10))['items']}

    async def ticks(self,project,sid,limit=20,cursor=None):
        contracts().validate('Args_list_schedule_ticks',{'schedule_id':sid,'limit':limit,'cursor':cursor})
        cursor_time,identity=(cursor.rsplit('|',1) if cursor else (None,None))
        if cursor_time:cursor_time=instant(cursor_time);identity=str(uuid.UUID(identity))
        async with self.repository.database.connection() as c:
            rows=await (await c.execute('''SELECT t.*,r.status AS run_status,
                COALESCE(r.started_at,(u.control_json->'commands'->t.idempotency_key->>'schedule_started_at')::timestamptz) AS started_at,
                m.delivery_status FROM assistant.schedule_ticks t
                JOIN assistant.project_schedules s USING(project_id,schedule_id)
                JOIN assistant.project_runtime_units u ON u.project_id=s.project_id AND u.unit_id=s.unit_id
                LEFT JOIN assistant.runs r ON r.run_id=t.run_id LEFT JOIN assistant.messages m ON m.message_id=t.notification_id
                WHERE t.project_id=%s AND t.schedule_id=%s AND (%s::timestamptz IS NULL OR (t.scheduled_for_utc,t.tick_id)<(%s::timestamptz,%s::uuid))
                ORDER BY t.scheduled_for_utc DESC,t.tick_id DESC LIMIT %s''',
                (project,sid,cursor_time,cursor_time,identity,limit+1))).fetchall()
        return {'items':[self.tick_view(x) for x in rows[:limit]],'next_cursor':iso(rows[limit-1]['scheduled_for_utc'])+'|'+str(rows[limit-1]['tick_id']) if len(rows)>limit else None}

    @staticmethod
    def tick_view(row):
        result={k:row[k] for k in ('project_id','schedule_id','origin','definition_ref','control_revision','disposition','reason','control_receipt_ref','settlement_status','result_ref')}
        result.update(tick_id=str(row['tick_id']),scheduled_for_utc=iso(row['scheduled_for_utc']),admitted_at=iso(row['admitted_at']),settled_at=iso(row['settled_at']),
            run_ref=resource_ref(kind='receipt',identity=str(row['run_id']),uri='db://assistant/runs/'+str(row['run_id']),purpose='scheduled business execution') if row['run_id'] else None,
            notification_id=str(row['notification_id']) if row['notification_id'] else None)
        contracts().validate('ScheduleTick',result)
        return {**result,'run_status':row.get('run_status'),'started_at':iso(row.get('started_at')),'delivery_status':row.get('delivery_status')}
