"""Narrow per-attempt capability, not a Kernel admin/Worker claim token."""
import hashlib,secrets
from fastapi import APIRouter,Header,HTTPException
from assistant_kernel.services.business_models import BusinessModels
from assistant_kernel.services.runtime_admission import RuntimeAdmission,RuntimeCapacityUnavailable


def build_runtime_router(settings,repository):
    router=APIRouter(prefix='/v1/runtime')
    async def owner(run_id,authorization):
        run=await repository.get_run(run_id)
        if not run or run['run_type']!='python' or run['request_json'].get('python_protocol')!='fixed-manifest.v1':
            raise HTTPException(404,'Managed business execution not found')
        stored=run['executor_json'].get('business_capability_sha256','')
        token=(authorization or '').removeprefix('Bearer ')
        if not stored or not secrets.compare_digest(hashlib.sha256(token.encode()).hexdigest(),stored):raise HTTPException(403,'Invalid business capability')
        if run['executor_json'].get('business_capability_attempt')!=run['attempt'] or not await repository.run_claim_is_active(
                str(run['run_id']),str(run['worker_instance_id']),str(run['claim_token'])):
            raise HTTPException(403,'Business execution is no longer active')
        return run
    @router.post('/{run_id}/{operation}')
    async def action(run_id:str,operation:str,request:dict,authorization:str|None=Header(default=None)):
        run=await owner(run_id,authorization)
        try:
            if operation=='model':return await BusinessModels(settings,repository).invoke(run,request)
            if operation in {'reserve-cpu','release-cpu'}:
                return await RuntimeAdmission(settings).cpu_step(repository,run,request,release=operation=='release-cpu')
            from assistant_kernel.services.business_tools import BusinessTools
            if operation=='tool':return await BusinessTools(settings,repository).call(run,request)
            if operation in {'effect-begin','effect-finish'}:
                from assistant_kernel.services.business_tools import BusinessEffects
                from assistant_kernel.services.business_runtime import BusinessRuntime
                runtime=BusinessRuntime(settings,repository)
                manifest=runtime.fixed.read_manifest(run['project_id'],run['request_json']['manifest_ref'])
                if request['key'] not in manifest['expected_effects']:raise PermissionError('Effect key is outside this approved fixed manifest')
                if operation=='effect-begin':return await BusinessEffects(settings,repository).change(run,request['key'],request['description'])
                return await BusinessEffects(settings,repository).change(run,request['key'],{k:v for k,v in request.items() if k!='key'},finish=True)
            if operation=='needs-input':
                from assistant_kernel.services.business_interactions import BusinessInteractions
                return await BusinessInteractions(settings,repository).wait(run,request)
            raise HTTPException(404,'Business operation not found')
        except PermissionError as exc:raise HTTPException(403,str(exc)) from exc
        except RuntimeCapacityUnavailable as exc:raise HTTPException(409,str(exc)) from exc
        except ValueError as exc:raise HTTPException(422,str(exc)) from exc
        except Exception as exc:
            # MCP context managers preserve errors in nested ExceptionGroups.
            cause=exc
            while len(getattr(cause,'exceptions',[]))==1:cause=cause.exceptions[0]
            if isinstance(cause,PermissionError):raise HTTPException(403,str(cause)) from exc
            if isinstance(cause,ValueError):raise HTTPException(422,str(cause)) from exc
            raise
    return router


def build_service_runtime_router(settings,repository):
    router=APIRouter(prefix='/v1/service-runtime')
    @router.post('/{instance_id}/{operation}')
    async def action(instance_id:str,operation:str,request:dict,authorization:str|None=Header(default=None)):
        from assistant_kernel.services.service_runtime import ServiceRuntime
        from assistant_kernel.services.service_work import ServiceWork
        from assistant_kernel.services.business_tools import BusinessTools,BusinessEffects
        services=ServiceRuntime(settings,repository);instance=await services.get(instance_id)
        if not instance:raise HTTPException(404,'Service instance not found')
        stored=instance['state_json'].get('business_capability_sha256','');token=(authorization or '').removeprefix('Bearer ')
        if not stored or not secrets.compare_digest(hashlib.sha256(token.encode()).hexdigest(),stored):raise HTTPException(403,'Invalid service capability')
        if instance['cleanup_status']=='verified':raise HTTPException(403,'Service instance has ended')
        try:
            work=ServiceWork(settings,repository)
            if operation in {'ready','heartbeat','work-begin','work-end','work-wait','work-answers','reserve-cpu','release-cpu'}:
                return await work.action(instance,operation,request)
            logical=request.pop('logical_execution_id',None)
            if not isinstance(logical,str):raise ValueError('Service call needs its work identity')
            logical=work.identity(instance,logical)
            owner=services.owner(instance,logical)
            if operation in {'model','tool','effect-begin'}:await work.consume_step(instance,logical,operation+':'+str(request.get('idempotency_key') or request.get('key')))
            if operation=='model':return await BusinessModels(settings,repository).invoke(owner,request)
            if operation=='tool':return await BusinessTools(settings,repository).call(owner,request)
            if operation in {'effect-begin','effect-finish'}:
                manifest=services.fixed.read_manifest(instance['project_id'],owner['request_json']['manifest_ref'])
                if request['key'] not in manifest['expected_effects']:raise PermissionError('Effect is outside the fixed service manifest')
                return await BusinessEffects(settings,repository).change(owner,request['key'],
                    {k:v for k,v in request.items() if k!='key'} if operation=='effect-finish' else request['description'],finish=operation=='effect-finish')
            raise HTTPException(404,'Unknown service operation')
        except PermissionError as exc:raise HTTPException(403,str(exc)) from exc
        except RuntimeCapacityUnavailable as exc:raise HTTPException(409,str(exc)) from exc
        except ValueError as exc:raise HTTPException(422,str(exc)) from exc
    return router
