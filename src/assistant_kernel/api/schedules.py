"""Authenticated Project schedule management; shares the scoped-tool service."""
from typing import Any
from uuid import UUID
from fastapi import APIRouter,Depends,HTTPException
from pydantic import BaseModel,ConfigDict,Field
from assistant_kernel.services.schedules import Schedules
from assistant_kernel.services.schedule_time import preview


class PreviewRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    rule:dict[str,Any]
    timezone:str='Asia/Shanghai'


class RegisterRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    task_id:UUID
    definition_ref:dict[str,Any]
    expected_control_revision:int=0
    idempotency_key:str=Field(min_length=1,max_length=256)
    authorization_source_refs:list[dict[str,Any]]


class ControlRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    task_id:UUID
    action:str
    expected_control_revision:int
    idempotency_key:str=Field(min_length=1,max_length=256)
    authorization_source_refs:list[dict[str,Any]]
    target_definition_ref:dict[str,Any]|None=None


def schedules_router(settings,repository,authorize):
    router=APIRouter(prefix='/v1/projects/{project_id}/schedules',dependencies=[Depends(authorize)])
    service=Schedules(settings,repository)

    async def result(awaitable):
        try:return await awaitable
        except FileNotFoundError as exc:raise HTTPException(404,str(exc)) from exc
        except PermissionError as exc:raise HTTPException(403,str(exc)) from exc
        except ValueError as exc:raise HTTPException(422,str(exc)) from exc

    @router.post('/preview')
    async def preview_rule(project_id:str,request:PreviewRequest):
        try:return preview(request.rule,request.timezone)
        except (ValueError,KeyError) as exc:raise HTTPException(422,str(exc)) from exc

    @router.post('')
    async def register(project_id:str,request:RegisterRequest):
        args=request.model_dump();args['task_id']=str(args['task_id'])
        return await result(service.register(project_id,**args))

    @router.get('')
    async def listing(project_id:str,limit:int=20,cursor:str|None=None):
        return await result(service.list(project_id,limit,cursor))

    @router.get('/{schedule_id}')
    async def status(project_id:str,schedule_id:str):
        return await result(service.status(project_id,schedule_id))

    @router.post('/{schedule_id}/control')
    async def control(project_id:str,schedule_id:str,request:ControlRequest):
        args=request.model_dump();task_id=str(args.pop('task_id'));args['schedule_id']=schedule_id
        return await result(service.control(project_id,args,task_id=task_id))

    @router.get('/{schedule_id}/ticks')
    async def ticks(project_id:str,schedule_id:str,limit:int=20,cursor:str|None=None):
        return await result(service.ticks(project_id,schedule_id,limit,cursor))

    return router
