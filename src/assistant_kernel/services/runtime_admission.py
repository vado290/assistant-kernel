"""Short PostgreSQL admission transactions; reservations live on their real owners."""
from __future__ import annotations

from copy import deepcopy
import math
from typing import Any

from assistant_kernel.services.system_config import STATE_KEY, SystemConfigService
from assistant_kernel.services.contracts import contracts

ADMISSION_LOCK = 'kernel-runtime-admission:v1'
LEGACY_LIMITS = {'max_finite_runs':4,'max_cpu_work_items':4,'max_cpu_slots':8,
    'max_managed_model_invocations':8,'max_service_instances':4,'max_declared_memory_mb':None}
LEGACY_PYTHON = {'cpu_pool_size':2,'numeric_threads':1,'io_workers':8,'timeout_seconds':300,'max_timeout_seconds':3600}


def tighten_limits(global_limits,overrides):
    """An accepted Project policy can reduce capacity, never increase it."""
    if not isinstance(overrides,dict) or set(overrides)-set(global_limits):raise ValueError('Unknown Project resource limit')
    result=deepcopy(global_limits)
    for key,value in overrides.items():
        if value is not None and (isinstance(value,bool) or not isinstance(value,int) or value<1):
            raise ValueError('Project capacity must be a positive integer or an unbounded global memory limit')
        if value is None:
            if key!='max_declared_memory_mb' or global_limits[key] is not None:raise ValueError('Project cannot remove a global limit')
        else:result[key]=value if global_limits[key] is None else min(value,global_limits[key])
    return result


class RuntimeCapacityUnavailable(RuntimeError):
    """No model/process was launched; queue this claim without spending a retry."""


def declared_resource_keys(request:dict[str,Any])->list[str]:
    keys=request.get('resource_keys') or []
    if not isinstance(keys,list) or any(not isinstance(x,str) or not x.strip() or len(x)>512 for x in keys):
        raise ValueError('resource_keys must be bounded nonempty strings')
    return sorted(set(keys))


def resources_overlap(left,right):
    return any(a==b or a.startswith('path:') and b.startswith('path:') and
        (a.startswith(b.rstrip('/')+'/') or b.startswith(a.rstrip('/')+'/')) for a in left for b in right)


def finite_demand(request:dict[str,Any],defaults:dict[str,Any])->dict[str,int|None]:
    resources=request.get('runtime_resources')
    # The legacy auxiliary adapter uses the approved Python defaults. It never
    # claims observed CPU usage or hard OS isolation for an undeclared script.
    resources=resources if isinstance(resources,dict) else {'mode':'cpu','cpu_budget':defaults['cpu_pool_size']*defaults['numeric_threads']}
    mode=resources.get('mode','sequential')
    if mode not in {'cpu','io','sequential'}:raise ValueError('Unknown Python resource mode')
    slots=resources.get('cpu_budget',defaults['numeric_threads'])
    memory=resources.get('expected_peak_memory_mb')
    if isinstance(slots,bool) or not isinstance(slots,(int,float)) or not math.isfinite(slots) or slots<0 or (mode!='io' and slots==0):
        raise ValueError('CPU reservation requires a positive declared budget')
    threads=resources.get('numeric_threads',defaults['numeric_threads'])
    pool=resources.get('cpu_pool_size',defaults['cpu_pool_size'] if mode=='cpu' else 0)
    if isinstance(threads,bool) or not isinstance(threads,int) or threads<1:raise ValueError('Invalid numeric threads')
    if isinstance(pool,bool) or not isinstance(pool,int) or pool<0:raise ValueError('Invalid CPU pool size')
    if (mode!='io' or pool>0) and max(1,pool)*threads>slots:raise ValueError('CPU pool and library threads exceed the declared budget')
    if memory is not None and (isinstance(memory,bool) or not isinstance(memory,int) or memory<1):raise ValueError('Invalid declared memory')
    declared_slots=resources.get('declared_cpu_slots',math.ceil(slots))
    if isinstance(declared_slots,bool) or not isinstance(declared_slots,int) or declared_slots<math.ceil(slots):
        raise ValueError('Declared CPU slots cannot be below the fixed CPU budget')
    return {'finite_runs':1,'cpu_work_items':int(mode!='io' or pool>0),'cpu_slots':declared_slots,'declared_memory_mb':memory}


class RuntimeAdmission:
    def __init__(self,settings):self.settings=settings;self.config=SystemConfigService(settings)

    @staticmethod
    async def lock(connection):
        await connection.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(ADMISSION_LOCK,))

    async def configuration_locked(self,connection):
        row=await (await connection.execute('SELECT value FROM assistant.kernel_meta WHERE key=%s FOR SHARE',(STATE_KEY,))).fetchone()
        if not row:
            return deepcopy(LEGACY_LIMITS),deepcopy(LEGACY_PYTHON),None
        state=row['value'];config=self.config._config(state['desired_ref'])
        return config['resource_limits'],config['python_defaults'],state['desired_ref']

    async def project_limits_locked(self,connection,project_id,global_limits):
        from assistant_kernel.services.published_agents import PublishedAgentService
        row=await (await connection.execute('SELECT value FROM assistant.kernel_meta WHERE key=%s',
            (PublishedAgentService.key(project_id),))).fetchone()
        if not row:return deepcopy(global_limits),None
        ref=row['value']['package_ref'];policy=PublishedAgentService(self.settings).read_package(project_id,ref)['project_policy']
        return tighten_limits(global_limits,policy.get('resource_limits') or {}),ref

    @staticmethod
    async def occupied_locked(connection):
        return await (await connection.execute("""SELECT run_id,project_id,run_type,status,mutex_key,request_json FROM assistant.runs
            WHERE status IN ('running','reconciling') OR (run_type='python' AND executor_json ? 'python_attempt'
              AND executor_json#>>'{python_attempt,process_identity,pid}' IS NOT NULL
              AND COALESCE(executor_json#>>'{python_attempt,cleanup,status}','unknown')<>'verified')""")).fetchall()

    @staticmethod
    async def service_occupied_locked(connection):
        exists=await (await connection.execute("SELECT to_regclass('assistant.service_instances') AS name")).fetchone()
        if not exists['name']:return []
        return await (await connection.execute("SELECT * FROM assistant.service_instances WHERE cleanup_status<>'verified'")).fetchall()

    async def finite_reservation_locked(self,connection,run,occupied,limits,defaults,config_ref):
        request=run['request_json'] or {};keys=declared_resource_keys(request)
        services=await self.service_occupied_locked(connection)
        if any(resources_overlap(keys,s['state_json'].get('resource_keys',[])) for s in services):return None
        for key in keys:
            await connection.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('runtime-resource:'+key,))
        unknown_shared=run.get('mutex_key')=='shared-write:global'
        for other in occupied:
            other_keys=declared_resource_keys(other['request_json'] or {})
            if (resources_overlap(keys,other_keys) or (keys and other['mutex_key']=='shared-write:global')
                    or (unknown_shared and other_keys)):return None
        if run['run_type']!='python':return {'resource_keys':keys,'units':{},'resolved_config_ref':config_ref}
        demand=finite_demand(request,defaults);used={k:0 for k in demand}
        for service in services:
            if limits['max_declared_memory_mb'] is not None:return None # Service manifest has no proven memory bound.
            for item in service['state_json'].get('cpu_reservations',{}).values():
                used['cpu_work_items']+=1;used['cpu_slots']+=item['slots']
        if limits['max_declared_memory_mb'] is not None and demand['declared_memory_mb'] is None:return None
        for other in occupied:
            if other['run_type']!='python':continue
            reservation=(other['request_json'] or {}).get('capacity_reservation')
            units=reservation['units'] if reservation else finite_demand(other['request_json'] or {},defaults)
            if limits['max_declared_memory_mb'] is not None and units.get('declared_memory_mb') is None:return None
            for key in used:used[key]+=int(units.get(key) or 0)
        names={'finite_runs':'max_finite_runs','cpu_work_items':'max_cpu_work_items',
            'cpu_slots':'max_cpu_slots','declared_memory_mb':'max_declared_memory_mb'}
        if any(limits[names[k]] is not None and used[k]+int(v or 0)>limits[names[k]] for k,v in demand.items()):return None
        local,policy_ref=await self.project_limits_locked(connection,run['project_id'],limits)
        project_used={k:0 for k in demand}
        for service in services:
            if service['project_id']==run['project_id']:
                for item in service['state_json'].get('cpu_reservations',{}).values():project_used['cpu_work_items']+=1;project_used['cpu_slots']+=item['slots']
        for other in occupied:
            if other['run_type']!='python' or other['project_id']!=run['project_id']:continue
            units=((other['request_json'] or {}).get('capacity_reservation') or {}).get('units') or finite_demand(other['request_json'] or {},defaults)
            if local['max_declared_memory_mb'] is not None and units.get('declared_memory_mb') is None:return None
            for key in project_used:project_used[key]+=int(units.get(key) or 0)
        if local['max_declared_memory_mb'] is not None and demand['declared_memory_mb'] is None:return None
        if any(local[names[k]] is not None and project_used[k]+int(v or 0)>local[names[k]] for k,v in demand.items()):return None
        return {'resource_keys':keys,'units':demand,'resolved_config_ref':config_ref,
            'project_policy_ref':policy_ref,'python_defaults':defaults,'basis':'accepted_configuration' if config_ref else 'legacy_auxiliary_defaults'}

    async def model_reservation_locked(self,connection,project_id,invocation_id):
        await self.lock(connection)
        duplicate=await (await connection.execute('SELECT status FROM assistant.model_invocations WHERE invocation_id=%s',(invocation_id,))).fetchone()
        if duplicate:raise PermissionError('An invocation identity cannot launch a second model call')
        limits,_,ref=await self.configuration_locked(connection)
        count=await (await connection.execute("SELECT count(*) AS occupied FROM assistant.model_invocations WHERE status='running' OR metadata_json->>'outcome_status'='unknown'")).fetchone()
        if count['occupied']>=limits['max_managed_model_invocations']:raise RuntimeCapacityUnavailable('Managed model capacity is occupied')
        local,policy_ref=await self.project_limits_locked(connection,project_id,limits)
        project_count=await (await connection.execute("SELECT count(*) AS occupied FROM assistant.model_invocations WHERE project_id=%s AND (status='running' OR metadata_json->>'outcome_status'='unknown')",(project_id,))).fetchone()
        if project_count['occupied']>=local['max_managed_model_invocations']:raise RuntimeCapacityUnavailable('Project model capacity is occupied')
        return {'project_id':project_id,'invocation_id':invocation_id,'units':{'model_invocations':1},'resolved_config_ref':ref,
            'project_policy_ref':policy_ref,'occupied_before':count['occupied'],'limit':limits['max_managed_model_invocations']}

    async def cpu_step(self,repository,run,request,*,release=False):
        """Subdivide the finite Run's already-held peak CPU capacity; no global double count."""
        from psycopg.types.json import Jsonb
        from assistant_kernel.services.document_runtime import resource_ref,same_resource_ref
        contracts().validate('RuntimeCapacityRequest',request)
        if request['resource_kind']!='cpu_work_item' or request['expected_instance_generation'] is not None:
            raise ValueError('Finite CPU steps need a finite owner, not a service generation')
        owner=resource_ref(kind='receipt',identity=str(run['run_id']),uri='db://assistant/runs/'+str(run['run_id']),purpose='finite execution')
        if not same_resource_ref(request['owner_ref'],owner) or request['logical_execution_id']!=run['request_json'].get('logical_execution_id'):
            raise PermissionError('CPU work belongs to another Run/logical item')
        async with repository.database.connection() as c:
            row=await (await c.execute("SELECT * FROM assistant.runs WHERE run_id=%s AND status='running' AND worker_instance_id=%s AND claim_token=%s AND cancel_requested_at IS NULL AND lease_until>now() FOR UPDATE",
                (run['run_id'],run['worker_instance_id'],run['claim_token']))).fetchone()
            if not row:raise PermissionError('CPU parent claim is no longer active')
            reservation=row['request_json'].get('capacity_reservation') or {}
            if not reservation.get('resolved_config_ref'):raise PermissionError('CPU reuse needs the accepted configuration reservation')
            state=deepcopy(row['executor_json']);steps=state.setdefault('runtime_cpu_steps',{});key=request['idempotency_key'];prior=steps.get(key)
            if prior and prior['request']!=request:raise ValueError('CPU step key was reused with different work')
            status=('duplicate' if prior['active'] else 'released') if prior else 'reserved'
            if release:
                if not prior:raise ValueError('CPU step has no reservation to release')
                prior['active']=False;status='released'
            elif not prior:
                used=sum(x['request']['cpu_slots'] for x in steps.values() if x['active'])
                if used+request['cpu_slots']>reservation['units']['cpu_slots']:status='queued'
                else:steps[key]={'request':request,'active':True}
            await c.execute('UPDATE assistant.runs SET executor_json=%s,updated_at=now() WHERE run_id=%s',(Jsonb(state),run['run_id']));await c.commit()
        result={'reservation_id':str(run['run_id'])+':'+key,'owner_ref':request['owner_ref'],
            'logical_execution_id':request['logical_execution_id'],'status':status,'resolved_config_ref':reservation['resolved_config_ref'],
            'units':{'cpu_slots':request['cpu_slots'],'model_invocations':0},'usage_ref':None,'cleanup_ref':None,
            'reason':'Reuses this finite Run peak reservation; no second global CPU allocation'}
        contracts().validate('RuntimeCapacityReceipt',result);return result
