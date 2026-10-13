"""Project operation documents and event-driven notices; no scheduler or automatic analysis."""
import hashlib
from copy import deepcopy
from datetime import datetime,timezone,timedelta
from psycopg.types.json import Jsonb
from assistant_kernel.services.contracts import contracts,canonical_json
from assistant_kernel.services.document_runtime import TaskDocumentService,same_resource_ref
from assistant_kernel.services.v12_evidence import V12Evidence

KINDS={'operation-observation':'observations','improvement-candidate':'candidates','operation-monitor-policy':'policies',
    'improvement-proposal':'proposals','operation-effect-comparison':'comparisons','kernel-notification':'notifications'}
FIELDS={'observation_refs':'operation-observation','monitor_policy_refs':'operation-monitor-policy',
    'improvement_candidates':'improvement-candidate','proposal_refs':'improvement-proposal'}

class OperationRecords:
    def __init__(self,settings,repository):self.settings=settings;self.repository=repository;self.documents=TaskDocumentService(settings)

    @staticmethod
    def prefix(project,kind):return 'operation-record:'+project+':'+KINDS[kind]+':'

    async def write(self,task,kind,payload):
        if kind not in KINDS or kind=='kernel-notification':raise PermissionError('Unknown operation candidate kind')
        if task['request_json'].get('current_stage') not in {'design','build','audit','operate'}:raise PermissionError('This phase does not author operation records')
        contracts().validate(contracts().document_definition(kind+'.v1'),payload)
        await V12Evidence(self.settings,self.repository).verify(task['project_id'],payload,task=task)
        if kind=='operation-effect-comparison':
            if payload['owner_project_id']!=task['project_id']:raise PermissionError('Comparison belongs to another Project')
            if payload['status'] in {'improved','unchanged','regressed'} and any(not payload[x] for x in ('before_refs','after_refs','case_refs','model_environment_refs','input_refs')):
                raise ValueError('A comparable result needs actual before/after cases, inputs and environment; otherwise use unverified/incomparable')
        identity=payload.get('candidate_key') or payload.get('policy_id') or hashlib.sha256(canonical_json(payload)).hexdigest()
        return self.documents.write_scoped_revision(project_id=task['project_id'],scope='project',object_id=kind+'-'+hashlib.sha256(str(identity).encode()).hexdigest()[:24],
            document_kind=kind,document_schema=kind+'.v1',payload=payload,status='submitted',actor_type='pm',actor_id=task['agent_key'],
            reason='Operation candidate; writing never authorizes notifications or implementation changes',source_refs=payload.get('source_refs') or payload.get('evidence_refs') or [])

    async def index_locked(self,c,project,kind,ref,*,owner=None):
        await c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('operation-records:'+project,))
        doc=self.documents.read(project,ref)
        if doc['scope']!='project' or doc['schema_version']!=kind+'.v1':raise PermissionError('Operation record has another scope/schema')
        body=doc['payload'];identity=body.get('candidate_key') or body.get('policy_id') or body.get('alert_key') or ref['sha256']
        key=self.prefix(project,kind)+hashlib.sha256(str(identity).encode()).hexdigest()
        if kind=='improvement-candidate':
            if body['distinct_event_count']!=len(set(body['logical_event_keys'])):raise ValueError('Candidate count differs from distinct original events')
            if body['review_result']=='still_exists' and (not body['reviewed_current_version_ref'] or not body['reviewed_at']):raise ValueError('Candidate has not been reviewed against its current version')
        old=await (await c.execute('SELECT value FROM assistant.kernel_meta WHERE key=%s FOR UPDATE',(key,))).fetchone()
        value={'project_id':project,'ref':ref,'kind':kind,'recorded_at':datetime.now(timezone.utc).isoformat(),
            'active':old['value'].get('active',False) if old else False,
            'logical_event_keys':sorted(set(body.get('logical_event_keys',[]))),'scene':body.get('scene'),'object_id':body.get('candidate_id') or body.get('policy_id')}
        if old:
            for name in ('active_ref','last_approval_at','approval_refs'):
                if name in old['value']:value[name]=old['value'][name]
        if old and same_resource_ref(old['value']['ref'],ref):return key
        await c.execute('INSERT INTO assistant.kernel_meta(key,value) VALUES(%s,%s) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_at=now()',(key,Jsonb(value)))
        from assistant_kernel.services.project_changes import ProjectChangeService
        await ProjectChangeService(self.settings,self.repository.database).capture_batch(project,batch_id='operation-record:'+ref['sha256'],roots=[],
            changes=[{'target_uri':'workspace://project/'+ref['uri'].removeprefix('vault://'+project+'/'),'document_ref':ref,'operation':'discovered',
                'before_sha256':None,'after_sha256':ref['sha256'],'purpose':kind+'.v1','accepted_head_guard':{'kind':'operation_record','owner_id':key}}],
            task_id=str(owner['run_id']) if owner else None,run_id=str(owner['run_id']) if owner else None,actor='kernel',
            reason='Accepted Project operation document; no automatic change approval',reason_source_refs=[ref],scope_kind='artifact',exact_time=False,connection=c)
        return key

    async def record_event_locked(self,c,*,project,event_key,scene,source_ref,target_ref,occurred_at,summary,metric=None):
        if len(event_key)>160:event_key='runtime:'+hashlib.sha256(event_key.encode()).hexdigest()
        metric=metric or 'execution_failure';targets=[target_ref] if target_ref and target_ref.get('sha256') else []
        payload={'target_refs':targets,'operational_baseline_ref':None,'report_window':{'start':occurred_at,'end':occurred_at,'timezone':'UTC'},
            'logical_event_keys':[event_key],'facts':[{'statement':summary,'source_refs':[source_ref],'observed_at':occurred_at}],
            'metrics':[{'metric_id':metric,'value':1,'unit':'logical_event','status':'measured','evidence_refs':[source_ref]}],
            'source_refs':[source_ref],'coverage':{'status':'partial','covered_refs':[source_ref],'missing_refs':[],
                'notes':['This actual event only; not a whole-Project business conclusion']},'source_kind':'run_result','scene':scene,'occurred_at':occurred_at,
            'symptoms':[summary],'impacts':[],'task_refs':[],'turn_refs':[],'run_refs':[],'gaps':[]}
        ref=self.documents.write_scoped_revision(project_id=project,scope='project',object_id='event-'+hashlib.sha256(event_key.encode()).hexdigest()[:24],
            document_kind='operation-observation',document_schema='operation-observation.v1',payload=payload,status='settled',actor_type='kernel',actor_id='runtime-observer',
            reason='Actual runtime event; retries retain the same logical event key',source_refs=[source_ref])
        await self.index_locked(c,project,'operation-observation',ref);await self.evaluate_locked(c,project);return ref

    async def accept_locked(self,c,task,payload,report=None):
        refs=[]
        for field,kind in FIELDS.items():
            for ref in payload.get(field,[]):
                await self.index_locked(c,task['project_id'],kind,ref,owner=task);refs.append(ref)
        for ref in (report or {}).get('effect_comparison_refs',[]):
            await self.index_locked(c,task['project_id'],'operation-effect-comparison',ref,owner=task);refs.append(ref)
        # Evaluating accepted facts is deterministic; it does not create a Task or model call.
        if payload.get('observation_refs'):await self.evaluate_locked(c,task['project_id'])
        return refs

    async def activate_policy(self,project,ref,sources,*,active=True,authenticated_operator=False):
        if not authenticated_operator or not sources:raise PermissionError('Policy activation needs a real Kernel operator instruction')
        await V12Evidence(self.settings,self.repository).verify(project,[ref,*sources])
        policy=self.documents.read(project,ref)['payload']
        if policy['schedule_refs'] or policy['collector_manifest_refs']:raise PermissionError('Scheduled collectors are deferred; this implementation consumes recorded events only')
        async with self.repository.database.connection() as c:
            # Exact subject approval, never a directory grant or a generic old yes.
            approved=False;approved_at=None
            for source in sources:
                if source['kind']!='message':continue
                row=await (await c.execute("SELECT content_json,user_id,direction,created_at FROM assistant.messages WHERE message_id=%s AND project_id=%s",(source['id'],project))).fetchone()
                if row and row['direction']=='inbound' and row['user_id']:
                    body=row['content_json'] or {}
                    match=any(x.get('decision')=='approve' and x.get('policy_action','activate')==('activate' if active else 'deactivate') and same_resource_ref(x.get('document_ref'),ref) for x in body.get('document_approvals',[]))
                    if match:approved=True;approved_at=max(approved_at,row['created_at']) if approved_at else row['created_at']
            if not approved:raise PermissionError('No actual approval of this exact notification policy')
            key=self.prefix(project,'operation-monitor-policy')+hashlib.sha256(policy['policy_id'].encode()).hexdigest()
            previous=await (await c.execute('SELECT value FROM assistant.kernel_meta WHERE key=%s FOR UPDATE',(key,))).fetchone()
            if previous and previous['value'].get('last_approval_at'):
                old=previous['value']
                if approved_at<=datetime.fromisoformat(old['last_approval_at']):
                    if old['active']==active and same_resource_ref(old.get('active_ref'),ref):return {'policy_ref':ref,'active':active,'automatic_llm_calls':False}
                    raise PermissionError('An older approval cannot override a later policy decision')
            key=await self.index_locked(c,project,'operation-monitor-policy',ref)
            await c.execute("UPDATE assistant.kernel_meta SET value=value || %s,updated_at=now() WHERE key=%s",
                (Jsonb({'active':active,'active_ref':ref,'approval_refs':sources,'last_approval_at':approved_at.isoformat()}),key))
            if active:await self.evaluate_locked(c,project)
            await c.commit()
        return {'policy_ref':ref,'active':active,'automatic_llm_calls':False}

    async def evaluate_locked(self,c,project):
        policies=await (await c.execute("SELECT value FROM assistant.kernel_meta WHERE key LIKE %s AND value->>'project_id'=%s AND value->>'active'='true'",(self.prefix(project,'operation-monitor-policy')+'%',project))).fetchall()
        if not policies:return
        observations=await (await c.execute("SELECT value FROM assistant.kernel_meta WHERE key LIKE %s AND value->>'project_id'=%s",(self.prefix(project,'operation-observation')+'%',project))).fetchall()
        for entry in policies:
            policy_ref=entry['value'].get('active_ref') or entry['value']['ref'];policy=self.documents.read(project,policy_ref)['payload']
            for rule in policy['notification_rules']:
                groups={};cutoff=datetime.now(timezone.utc)-timedelta(days=rule['window_days'])
                for observed in observations:
                    ref=observed['value']['ref'];body=self.documents.read(project,ref)['payload']
                    if not body['occurred_at'] or datetime.fromisoformat(body['occurred_at'].replace('Z','+00:00'))<cutoff:continue
                    if not any(x['metric_id']==rule['metric_id'] and x['status']=='measured' for x in body['metrics']):continue
                    targets=body['target_refs']
                    if policy['target_refs'] and not any(any(same_resource_ref(x,y) for y in policy['target_refs']) for x in targets):continue
                    if policy['notification_scope'] and body['scene'] not in policy['notification_scope']:continue
                    identity=hashlib.sha256(canonical_json([targets,rule['metric_id'],body['scene']])).hexdigest()
                    item=groups.setdefault(identity,{'events':set(),'refs':[],'body':body})
                    item['events'].update(body['logical_event_keys']);item['refs'].append(ref)
                for identity,item in groups.items():
                    if len(item['events'])<rule['min_distinct_events']:continue
                    key='operation-alert:'+project+':'+identity
                    if await (await c.execute('SELECT 1 FROM assistant.kernel_meta WHERE key=%s',(key,))).fetchone():continue
                    body=item['body'];text='项目运行出现已批准规则关注的事件：'+rule['metric_id']+'，独立事件 '+str(len(item['events']))+' 次。可发起分析；系统未自动修改程序、Role 或 Skill。'
                    payload={'notification_id':identity,'alert_key':identity,'project_id':project,'policy_ref':policy_ref,'rule_id':rule['rule_id'],
                        'observation_refs':item['refs'],'target_version_refs':body['target_refs'],'decision':'notify','reason':'First approved distinct-event rule match',
                        'decided_at':datetime.now(timezone.utc).isoformat(),'fact_summary':text,'supersedes_notification_ref':None,'kind':'operation_rule',
                        'candidate_or_event_refs':item['refs'],'report_window':body['report_window'],'evidence_refs':body['source_refs'],'applicability':'current'}
                    ref=self.documents.write_scoped_revision(project_id=project,scope='project',object_id='notification-'+identity[:24],document_kind='kernel-notification',
                        document_schema='kernel-notification.v1',payload=payload,status='settled',actor_type='kernel',actor_id='operation-rules',reason='Deterministic approved event rule',source_refs=item['refs'])
                    await self.index_locked(c,project,'kernel-notification',ref)
                    await c.execute('INSERT INTO assistant.kernel_meta(key,value) VALUES(%s,%s)',(key,Jsonb({'project_id':project,'ref':ref,'events':sorted(item['events'])})))
                    from assistant_kernel.services.runtime_notifications import RuntimeNotifications
                    routing=RuntimeNotifications(self.settings,self.repository)
                    route=await routing.pm_route(c,project,role='supervisor',channel=policy['route']['notification_channel']) or await routing.pm_route(c,project,channel=policy['route']['notification_channel'])
                    await self.repository._create_outbound_locked(c,project_id=project,session_id=None,run_id=None,channel=route[0] if route else 'internal',
                        chat_id=route[1] if route else 'pending-project-operations',user_id=route[2] if route else None,message_type='reply',
                        content={'interaction_kind':'operation_notice','notice_ref':ref,'policy_ref':policy_ref,'pending_operation_route':not bool(route),
                            'notification_channel':policy['route']['notification_channel'],'text':text},reply_to_message_id=None,idempotency_key=key)

    async def query(self,project,request):
        contracts().validate('OperationsQuery',request);kind=request['kind'];prefix='operation-record:'+project+':'+kind+':'
        if kind=='schedules':
            from assistant_kernel.services.schedule_support import installed
            async with self.repository.database.connection() as c:
                ready=await installed(c)
                rows=await (await c.execute('''SELECT schedule_id,definition_ref FROM assistant.project_schedules
                    WHERE project_id=%s AND (%s::text IS NULL OR schedule_id>%s) AND updated_at>=now()-%s*interval '1 day'
                    AND (%s::text IS NULL OR schedule_id=%s) ORDER BY schedule_id LIMIT %s''',
                    (project,request['cursor'],request['cursor'],request['lookback_days'],request['object_id'],request['object_id'],request['limit']+1))).fetchall() if ready else []
            refs=[row['definition_ref'] for row in rows[:request['limit']]]
            return {'project_id':project,'kind':kind,'items':refs,'next_cursor':rows[request['limit']-1]['schedule_id'] if len(rows)>request['limit'] else None,
                'as_of':datetime.now(timezone.utc).isoformat(),'coverage':{'status':'complete' if ready else 'unknown','covered_refs':refs,'missing_refs':[],
                    'notes':['Current registered schedules updated within the requested window; query schedule_status for lifecycle and ticks'] if ready else ['Scheduler database migration is not installed']}}
        async with self.repository.database.connection() as c:
            rows=await (await c.execute("SELECT key,value FROM assistant.kernel_meta WHERE key LIKE %s AND value->>'project_id'=%s AND (%s::text IS NULL OR key>%s) AND updated_at>=now()-%s*interval '1 day' AND (%s::text IS NULL OR value->>'object_id'=%s) ORDER BY key LIMIT %s",
                (prefix+'%',project,request['cursor'],request['cursor'],request['lookback_days'],request['object_id'],request['object_id'],request['limit']+1))).fetchall()
        refs=[x['value']['ref'] for x in rows[:request['limit']]]
        return {'project_id':project,'kind':kind,'items':refs,'next_cursor':rows[request['limit']-1]['key'] if len(rows)>request['limit'] else None,
            'as_of':datetime.now(timezone.utc).isoformat(),'coverage':{'status':'complete','covered_refs':refs,'missing_refs':[],
                'notes':['Accepted Project operation records within the requested window']}}

    async def route_pending(self):
        from assistant_kernel.services.runtime_notifications import RuntimeNotifications
        routing=RuntimeNotifications(self.settings,self.repository)
        async with self.repository.database.connection() as c:
            rows=await (await c.execute("SELECT * FROM assistant.messages WHERE direction='outbound' AND channel='internal' AND delivery_status='pending' AND content_json->>'pending_operation_route'='true' FOR UPDATE SKIP LOCKED LIMIT 100")).fetchall()
            for row in rows:
                channel=row['content_json']['notification_channel']
                route=await routing.pm_route(c,row['project_id'],role='supervisor',channel=channel) or await routing.pm_route(c,row['project_id'],channel=channel)
                if route:await c.execute("UPDATE assistant.messages SET channel=%s,chat_id=%s,user_id=%s,content_json=jsonb_set(content_json,'{pending_operation_route}','false'::jsonb) WHERE message_id=%s",(*route,row['message_id']))
            await c.commit()

    async def delivery_current(self,c,message):
        decision=await self.delivery_target(c,message)
        return decision['active'] and decision['route']==(message['channel'],message['chat_id'],message['user_id'])

    async def delivery_target(self,c,message):
        ref=message['content_json']['policy_ref'];policy=self.documents.read(message['project_id'],ref)['payload']
        key=self.prefix(message['project_id'],'operation-monitor-policy')+hashlib.sha256(policy['policy_id'].encode()).hexdigest()
        row=await (await c.execute('SELECT value FROM assistant.kernel_meta WHERE key=%s',(key,))).fetchone()
        if not row or not row['value'].get('active') or not same_resource_ref(row['value'].get('active_ref') or row['value']['ref'],ref):return {'active':False,'route':None}
        from assistant_kernel.services.runtime_notifications import RuntimeNotifications
        routing=RuntimeNotifications(self.settings,self.repository);channel=policy['route']['notification_channel']
        route=await routing.pm_route(c,message['project_id'],role='supervisor',channel=channel) or await routing.pm_route(c,message['project_id'],channel=channel)
        return {'active':True,'route':route}
