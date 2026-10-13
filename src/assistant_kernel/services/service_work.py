"""All service work, including I/O and waiting input, shares the instance admission gate."""
import hashlib,json
from pathlib import Path
from copy import deepcopy
from datetime import datetime,timezone
from psycopg.types.json import Jsonb
from assistant_kernel.services.service_runtime import ServiceRuntime,stamp,now
from assistant_kernel.services.runtime_admission import RuntimeAdmission,finite_demand
from assistant_kernel.services.contracts import canonical_json,contracts
from assistant_kernel.services.fixed_python import data_schema
from assistant_kernel.services.question_groups import QuestionGroupService
from assistant_kernel.services.document_runtime import resource_ref,same_resource_ref

class ServiceWork:
    def __init__(self,settings,repository):
        self.settings=settings;self.repository=repository;self.services=ServiceRuntime(settings,repository)

    @staticmethod
    def identity(instance,key):
        if not isinstance(key,str) or not key or len(key)>160:raise ValueError('Work requires a bounded stable key')
        prefix='service-work:'+hashlib.sha256((instance['project_id']+':'+instance['unit_id']).encode()).hexdigest()[:16]+':'
        return key if key.startswith(prefix) and len(key)==len(prefix)+32 else prefix+hashlib.sha256(key.encode()).hexdigest()[:32]

    async def action(self,instance,operation,request):
        logical=request.get('logical_execution_id');services=self.services
        definition=services.units.read_definition(instance['project_id'],instance['definition_ref'])
        manifest=services.manifest(instance)
        if operation in {'ready','heartbeat'}:
            async with self.repository.database.connection() as c:
                row=await services.get(instance['instance_id'],c,True)
                if row['cleanup_status']=='verified' or row['observed_state'] in {'stopping','stopped','failed'}:raise PermissionError('Instance is stopping or terminal')
                state=deepcopy(row['state_json']);state['ready_event']=stamp()
                await c.execute('UPDATE assistant.service_instances SET state_json=%s,updated_at=now() WHERE instance_id=%s',(Jsonb(state),row['instance_id']));await c.commit()
            if operation=='ready':await services.probe(await services.get(instance['instance_id']),manifest)
            current=await services.get(instance['instance_id'])
            return {'recorded':True,'generation':instance['generation'],'ready':current['observed_state']=='ready' and current['state_json']['work_control']['gate']=='open'}
        logical=self.identity(instance,logical);request={**request,'logical_execution_id':logical}
        if operation in {'reserve-cpu','release-cpu'}:return await self.cpu(instance,request,release=operation=='release-cpu')
        async with self.repository.database.connection() as c:
            row=await services.get(instance['instance_id'],c,True);state=deepcopy(row['state_json']);items=state['work_control']['items'];records=state['work_records']
            if row['cleanup_status']=='verified':raise PermissionError('Instance is terminal')
            if operation=='work-begin':
                if state['work_control']['gate']!='open' or row['observed_state']!='ready':raise PermissionError('Service work gate is closed')
                data_schema(request.get('input'),manifest['io']['input_schema']);digest=hashlib.sha256(canonical_json(request.get('input'))).hexdigest()
                previous=records.get(logical)
                if previous:
                    if previous['input_sha256']!=digest:raise ValueError('Work key was reused with different input')
                    if previous['status']=='completed':return {'duplicate':True,'result':previous['result'],'receipt_ref':previous['receipt_ref']}
                    raise PermissionError('Existing work is active, waiting or unresolved; do not replay it')
                prior=await (await c.execute("SELECT state_json->'work_records'->%s AS work FROM assistant.service_instances WHERE project_id=%s AND unit_id=%s AND instance_id<>%s AND state_json->'work_records' ? %s ORDER BY generation DESC LIMIT 1",
                    (logical,row['project_id'],row['unit_id'],row['instance_id'],logical))).fetchone()
                if prior:
                    record=prior['work']
                    if record['input_sha256']!=digest:raise ValueError('Old work key has different input')
                    if record['status']=='completed':return {'duplicate':True,'result':record['result'],'receipt_ref':record['receipt_ref']}
                    raise PermissionError('Previous generation still owns this unfinished work')
                if len(items)>=manifest['resources']['io_limit']:raise PermissionError('Service work concurrency is occupied')
                item={'logical_execution_id':logical,'definition_ref':row['definition_ref'],'state':'active','checkpoint_ref':None,
                    'prior_effect_refs':[],'admitted_at':stamp(),'reservation_ids':[]};contracts().validate('ServiceWorkItem',item)
                items[logical]=item;records[logical]={'input_sha256':digest,'input':request.get('input'),'status':'active','steps':0,'runtime_effects':{}}
                result={'duplicate':False,'logical_execution_id':logical,'definition_ref':row['definition_ref']}
            elif logical not in items:raise PermissionError('This service generation does not own the active work')
            elif operation=='work-end':
                elapsed=(now()-datetime.fromisoformat(items[logical]['admitted_at'])).total_seconds()-records[logical].get('waiting_seconds',0)
                if elapsed>manifest['resources']['work_item_timeout_seconds']:raise PermissionError('Work time budget exceeded; do not report a successful settlement')
                active=await (await c.execute("SELECT 1 FROM assistant.model_invocations WHERE project_id=%s AND metadata_json->>'logical_execution_id'=%s AND (status='running' OR metadata_json->>'outcome_status'='unknown') LIMIT 1",(row['project_id'],logical))).fetchone()
                if active or any(x['logical_execution_id']==logical for x in state['cpu_reservations'].values()):raise PermissionError('Work still owns model/CPU resources')
                if items[logical]['state']=='waiting_input':raise PermissionError('Work still waits for an answer')
                effects=records[logical]['runtime_effects']
                if any(x['status']!='succeeded' for x in effects.values()):raise PermissionError('Work has unresolved effects')
                data_schema(request.get('result'),manifest['io']['output_schema'])
                output=Path(state.get('output_root',services.root(row)))/'work'/hashlib.sha256(logical.encode()).hexdigest();required=[]
                from assistant_kernel.services.fixed_python import safe_relative
                for name in manifest['io']['required_outputs']:
                    path=output/str(safe_relative(name))
                    if not path.is_file():raise ValueError('Service work lacks a required output')
                    required.append({'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
                prepared={'manifest':{**manifest,'ownership':{'project_id':row['project_id']}}}
                checks=services.fixed.check_outputs(prepared,request.get('result'),output)
                if checks['business_status']=='failed':raise ValueError('Service work failed its fixed business assertions')
                ref=services.blob(row['project_id'],'service-work-result',{'instance_id':str(row['instance_id']),'logical_execution_id':logical,
                    'definition_ref':row['definition_ref'],'result':request.get('result'),'required_outputs':required,'checks':checks,'effect_refs':[x['receipt_ref'] for x in effects.values()]})
                records[logical].update(status='completed',result=request.get('result'),receipt_ref=ref,checks=checks);items.pop(logical)
                result={'completed':True,'receipt_ref':ref}
            elif operation=='work-wait':
                if any(x['logical_execution_id']==logical for x in state['cpu_reservations'].values()):raise PermissionError('Release CPU before waiting for input')
                pending=await (await c.execute("SELECT 1 FROM assistant.model_invocations WHERE project_id=%s AND metadata_json->>'logical_execution_id'=%s AND (status='running' OR metadata_json->>'outcome_status'='unknown') LIMIT 1",(row['project_id'],logical))).fetchone()
                if pending or any(x['status']!='succeeded' for x in records[logical]['runtime_effects'].values()):raise PermissionError('Settle model calls and effects before waiting for input')
                if records[logical].get('question_group_id'):return {'group_id':records[logical]['question_group_id']}
                checkpoint=request.get('checkpoint_ref')
                if checkpoint:services.fixed.source_path(row['project_id'],checkpoint,services.fixed.scopes.resolve(row['project_id'],str(row['instance_id']),'build'))
                design=services.documents.read(row['project_id'],definition['design_ref']);task=await self.repository.get_run(design['task_id'])
                source=await self.repository.get_message(str(task['trigger_message_id']))
                records[logical]['question_epoch']=records[logical].get('question_epoch',0)+1
                owner={'run_id':None,'service_instance_id':str(row['instance_id']),'logical_execution_id':logical,'project_id':row['project_id'],'question_epoch':records[logical]['question_epoch'],
                  'request_json':{'task_id':str(task['run_id'])},'interaction_seq':0}
                runtime_owner={'runtime_unit_ref':row['definition_ref'],'definition_ref':row['definition_ref'],
                    'logical_execution_ref':resource_ref(kind='receipt',identity=logical,uri='db://assistant/service_instances/'+str(row['instance_id'])+'#work/'+hashlib.sha256(logical.encode()).hexdigest(),purpose='actual service work'),
                    'business_thread_id':logical,'checkpoint_ref':checkpoint,'prior_effect_refs':[x['receipt_ref'] for x in records[logical]['runtime_effects'].values() if x.get('receipt_ref')],
                    'control_ref':state['work_control']['control_ref']}
                questions=QuestionGroupService(self.settings,self.repository)
                routes=await questions.resolve_routes_locked(c,owner=owner,source=source,interaction_scope='runtime_business')
                group=await questions.create_locked(c,owner=owner,questions=request['questions'],source_message_id=str(source['message_id']),routes=routes,
                    interaction_scope='runtime_business',blocking=True,runtime_owner=runtime_owner)
                items[logical].update(state='waiting_input',checkpoint_ref=checkpoint);records[logical].update(question_group_id=str(group['message_id']),wait_started_at=stamp())
                result={'group_id':str(group['message_id'])}
            elif operation=='work-answers':
                group_id=records[logical].get('question_group_id') or records[logical].get('last_question_group_id')
                group=await (await c.execute('SELECT content_json FROM assistant.messages WHERE message_id=%s',(group_id,))).fetchone() if group_id else None
                if not group or group['content_json']['group']['status']!='answered':return {'status':'waiting_input'}
                group=group['content_json']['group']
                if not same_resource_ref(group['runtime_owner']['definition_ref'],row['definition_ref']):raise PermissionError('Answers belong to another fixed definition')
                if items[logical]['state']=='waiting_input':
                    records[logical]['waiting_seconds']=records[logical].get('waiting_seconds',0)+(now()-datetime.fromisoformat(records[logical].pop('wait_started_at'))).total_seconds()
                    records[logical]['last_question_group_id']=records[logical].pop('question_group_id')
                items[logical]['state']='active';result={'status':'answered','answers':{x['question_id']:x['value'] for x in group['accepted_answers']}}
            else:raise ValueError('Unsupported service work operation')
            await c.execute('UPDATE assistant.service_instances SET state_json=%s,updated_at=now() WHERE instance_id=%s',(Jsonb(state),row['instance_id']));await c.commit()
            return result

    async def cpu(self,instance,request,*,release=False):
        request={**request,'logical_execution_id':self.identity(instance,request['logical_execution_id'])}
        logical=request['logical_execution_id'];key=request['idempotency_key'];admission=RuntimeAdmission(self.settings)
        contracts().validate('RuntimeCapacityRequest',request)
        if request['expected_instance_generation']!=instance['generation'] or request['owner_ref']['uri']!='db://assistant/service_instances/'+str(instance['instance_id']):raise PermissionError('CPU reservation belongs to another instance generation')
        manifest=self.services.manifest(instance)
        if request['cpu_slots']>manifest['resources']['cpu_pool_size']*manifest['resources']['numeric_threads']:raise PermissionError('CPU request exceeds the declared service pool')
        if isinstance(request.get('cpu_slots'),bool) or not isinstance(request.get('cpu_slots'),int) or request['cpu_slots']<1:raise ValueError('CPU slots must be positive')
        async with self.repository.database.connection() as c:
            await admission.lock(c);row=await self.services.get(instance['instance_id'],c,True);state=deepcopy(row['state_json'])
            if logical not in state['work_control']['items']:raise PermissionError('CPU owner is not admitted')
            reservations=state['cpu_reservations'];identity=logical+':'+key;prior=reservations.get(identity)
            if prior and prior['slots']!=request['cpu_slots']:raise ValueError('CPU key changed its reservation')
            if release:reservations.pop(identity,None);status='released'
            elif prior:status='duplicate'
            else:
                if row['observed_state']!='ready' or state['work_control']['items'][logical]['state']!='active':raise PermissionError('CPU work owner is not active')
                elapsed=(now()-datetime.fromisoformat(state['work_control']['items'][logical]['admitted_at'])).total_seconds()-state['work_records'][logical].get('waiting_seconds',0)
                if elapsed>manifest['resources']['work_item_timeout_seconds']:raise PermissionError('CPU admission exceeded this work item time budget')
                limits,defaults,config_ref=await admission.configuration_locked(c);local,_=await admission.project_limits_locked(c,row['project_id'],limits)
                runs=await admission.occupied_locked(c);services=await admission.service_occupied_locked(c)
                used={'cpu_slots':0,'cpu_work_items':0};local_used=used.copy()
                for run in runs:
                    if run['run_type']!='python':continue
                    units=((run['request_json'] or {}).get('capacity_reservation') or {}).get('units') or finite_demand(run['request_json'],defaults)
                    for name in used:
                        used[name]+=units[name]
                        if run['project_id']==row['project_id']:local_used[name]+=units[name]
                for service in services:
                    for item in service['state_json'].get('cpu_reservations',{}).values():
                        used['cpu_slots']+=item['slots'];used['cpu_work_items']+=1
                        if service['project_id']==row['project_id']:local_used['cpu_slots']+=item['slots'];local_used['cpu_work_items']+=1
                status='queued' if any(used[k]+n>limits['max_'+k] or local_used[k]+n>local['max_'+k] for k,n in {'cpu_slots':request['cpu_slots'],'cpu_work_items':1}.items()) else 'reserved'
                if status=='reserved':reservations[identity]={'slots':request['cpu_slots'],'logical_execution_id':logical,'admitted_at':stamp()}
            await c.execute('UPDATE assistant.service_instances SET state_json=%s,updated_at=now() WHERE instance_id=%s',(Jsonb(state),row['instance_id']));await c.commit()
            return {'status':status,'reservation_id':identity,'cpu_slots':request['cpu_slots']}

    async def consume_step(self,instance,logical,step_key):
        logical=self.identity(instance,logical)
        definition=self.services.units.read_definition(instance['project_id'],instance['definition_ref']);manifest=self.services.manifest(instance)
        async with self.repository.database.connection() as c:
            row=await self.services.get(instance['instance_id'],c,True);state=deepcopy(row['state_json']);item=state['work_control']['items'].get(logical)
            if not item or item['state']!='active':raise PermissionError('Service work is not active')
            if (now()-datetime.fromisoformat(item['admitted_at'])).total_seconds()-state['work_records'][logical].get('waiting_seconds',0)>manifest['resources']['work_item_timeout_seconds']:
                raise PermissionError('Work item time budget is exhausted')
            work=state['work_records'][logical]
            keys=work.setdefault('step_keys',[])
            if step_key in keys:return
            if work['steps']>=manifest['resources']['max_steps_per_work_item']:raise PermissionError('Work item step budget is exhausted')
            work['steps']+=1;keys.append(step_key)
            await c.execute('UPDATE assistant.service_instances SET state_json=%s WHERE instance_id=%s',(Jsonb(state),row['instance_id']));await c.commit()
