"""Explicit approved manual actions and report requests; no timer or scheduler callback."""
from __future__ import annotations

from typing import Any, Callable
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from assistant_kernel.config import KernelSettings
from assistant_kernel.repositories import CoreRepository
from assistant_kernel.services.operations import ProjectOperations


class ActionRequest(BaseModel):
    task_id: UUID
    action_id: str = Field(min_length=1, max_length=128)
    inputs: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=1, max_length=256)


class ReportRequest(BaseModel):
    report_window: dict[str, Any]
    source_message_id: UUID


def operations_router(settings: KernelSettings, repository: CoreRepository,
                      authorize: Callable) -> APIRouter:
    router = APIRouter(prefix="/v2/projects", dependencies=[Depends(authorize)])
    operations = ProjectOperations(settings, repository)

    @router.post("/{project_id}/operations/actions", status_code=202)
    async def run_action(project_id: str, request: ActionRequest):
        try:
            return await operations.run_action(project_id, str(request.task_id), request.action_id,
                                               request.inputs, request.idempotency_key)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/{project_id}/operations/reports", status_code=202)
    async def request_report(project_id: str, request: ReportRequest):
        try:
            return await operations.request_report(project_id, request.report_window,
                                                   str(request.source_message_id))
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    return router
