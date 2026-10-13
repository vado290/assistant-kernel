from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from assistant_kernel.config import KernelSettings
from assistant_kernel.harness import CodexCliHarness
from assistant_kernel.repositories import CoreRepository
from assistant_kernel.services.agent_files import RouteResolver
from assistant_kernel.services.feishu import ingest_event, load_feishu_config
from assistant_kernel.services.discord import load_discord_config
from assistant_kernel.services.channel_health import read_channel_health
from assistant_kernel.storage import ProjectRuntimeManager


class IntakeMessageRequest(BaseModel):
    frontend: str = Field(min_length=1, max_length=64)
    user_id: str | None = Field(default=None, max_length=256)
    channel_conversation_id: str = Field(min_length=1, max_length=512)
    external_message_id: str = Field(min_length=1, max_length=512)
    text: str = Field(min_length=1, max_length=200000)
    metadata: dict[str, Any] = Field(default_factory=dict)
    project_id: str | None = Field(default=None, max_length=128)
    agent_key: str | None = Field(default=None, max_length=256)
    reply_to_message_id: UUID | None = None
    model: str | None = Field(default=None, max_length=256)
    reasoning_effort: str | None = Field(
        default=None, pattern="^(none|minimal|low|medium|high|xhigh|max|ultra)$"
    )
    context_policy: str | None = Field(
        default=None, pattern="^(model_default|max|custom)$"
    )
    model_context_window_tokens: int | None = Field(default=None, ge=1)
    model_auto_compact_token_limit: int | None = Field(default=None, ge=1)


class PromptConfirmationRequest(BaseModel):
    model_config={'extra':'forbid'}
    project_id: str = Field(min_length=1,max_length=128)
    frontend: str = Field(min_length=1,max_length=128)
    channel_conversation_id: str = Field(min_length=1,max_length=512)
    user_id: str = Field(min_length=1,max_length=256)
    external_event_id: str = Field(min_length=1,max_length=512)
    display_message_id: UUID
    package_sha256: str = Field(pattern='^[0-9a-f]{64}$')
    action: str = Field(pattern='^(confirm|revise|cancel)$')


class CreateRunRequest(BaseModel):
    project_id: str = Field(min_length=1, max_length=128)
    agent_key: str | None = Field(default=None, max_length=256)
    run_type: str = Field(pattern="^(chat|stage|python)$")
    stage: str | None = Field(default=None, max_length=128)
    parent_run_id: UUID | None = None
    trigger_message_id: UUID | None = None
    request: dict[str, Any] = Field(default_factory=dict)
    executor: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, max_length=512)
    shared_write: bool | None = None


class DeliveryClaimRequest(BaseModel):
    worker_id: str = Field(min_length=1, max_length=256)
    worker_instance_id: UUID
    lease_seconds: int = Field(default=60, ge=10, le=600)
    channel: str | None = Field(default=None, max_length=64)


class DeliveryAckRequest(BaseModel):
    worker_instance_id: UUID
    external_message_id: str = Field(min_length=1, max_length=512)
    delivery: dict[str, Any] = Field(default_factory=dict)


class DeliveryFailRequest(BaseModel):
    worker_instance_id: UUID
    error: str = Field(min_length=1, max_length=2000)


class ReconcileRunRequest(BaseModel):
    action: str = Field(pattern="^(retry|fail|cancel)$")
    note: str = Field(min_length=1, max_length=2000)


class UnblockRunRequest(BaseModel):
    note: str = Field(min_length=1, max_length=2000)


def build_v2_router(
    settings: KernelSettings, repository: CoreRepository, auth_dependency: Callable
) -> APIRouter:
    router = APIRouter(
        prefix="/v2",
        tags=["four-table-core"],
        dependencies=[Depends(auth_dependency)],
    )
    runtime = ProjectRuntimeManager(settings.data_root)
    codex = CodexCliHarness(runtime)
    route_resolver = RouteResolver(settings, repository)

    @router.post("/intake/messages", status_code=status.HTTP_202_ACCEPTED)
    async def intake_message(request: IntakeMessageRequest):
        if request.context_policy == "custom" and request.model_context_window_tokens is None:
            raise HTTPException(
                status_code=422,
                detail="custom context_policy requires model_context_window_tokens",
            )
        if (
            request.context_policy not in {None, "custom"}
            and request.model_context_window_tokens is not None
        ):
            raise HTTPException(
                status_code=422,
                detail="model_context_window_tokens is only valid with custom context_policy",
            )
        try:
            project_id, agent_key = await route_resolver.resolve(
                request.frontend,
                request.channel_conversation_id,
                request.user_id,
                request.project_id,
                request.agent_key,
                str(request.reply_to_message_id) if request.reply_to_message_id else None,
            )
        except (ValueError, PermissionError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        from assistant_kernel.services.kernel_management import retired_identity
        if retired_identity(project_id, agent_key):
            raise HTTPException(status_code=410, detail="Butler retired; provide an existing project_id")
        try:
            runtime.ensure(project_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if request.reply_to_message_id:
            original=await repository.get_message(str(request.reply_to_message_id))
            from assistant_kernel.services.question_groups import QuestionGroupService,projection_ref
            if original and projection_ref(original.get('content_json') or {}):
                try:
                    result=await QuestionGroupService(settings,repository).record_text(projection=original,
                        channel=request.frontend,chat_id=request.channel_conversation_id,user_id=str(request.user_id or ''),
                        external_event_id=request.external_message_id,text=request.text)
                except PermissionError as exc:raise HTTPException(status_code=403,detail=str(exc)) from exc
                except ValueError as exc:raise HTTPException(status_code=409,detail=str(exc)) from exc
                return {'project_id':project_id,'agent_key':agent_key,'message_id':result['input_message_id'],
                    'run_id':None,'duplicate':result['duplicate'],'partial_answer':True,'resumed':False,
                    'accepted_into_supervision':result['accepted_into_supervision'],'question_group_ref':result.get('group_ref')}
        result = await repository.ingest_message(
            project_id=project_id,
            agent_key=agent_key,
            channel=request.frontend,
            chat_id=request.channel_conversation_id,
            user_id=request.user_id,
            external_event_id=request.external_message_id,
            external_message_id=request.external_message_id,
            content={"text": request.text, "metadata": request.metadata},
            message_type="answer" if request.reply_to_message_id else "text",
            reply_to_message_id=(str(request.reply_to_message_id) if request.reply_to_message_id else None),
            executor={
                key: value for key, value in {
                    "model": request.model,
                    "reasoning_effort": request.reasoning_effort,
                    "context_policy": request.context_policy,
                    "model_context_window_tokens": request.model_context_window_tokens,
                    "model_auto_compact_token_limit": request.model_auto_compact_token_limit,
                }.items() if value is not None
            },
        )
        return {
            "project_id": project_id,
            "agent_key": agent_key,
            "message_id": str(result["message"]["message_id"]),
            "run_id": str(result["run"]["run_id"]) if result["run"] else None,
            "duplicate": result["duplicate"],
            "resumed": result["resumed"],
            "stale_reply": result.get("stale_reply", False),
            "approval_decision": result.get("approval_decision"),
            "decision_required": result.get("decision_required", False),
            "partial_answer": result.get("partial_answer", False),
        }

    @router.get("/codex/models")
    async def list_codex_models(project_id: str = "kernel"):
        try:
            models = await codex.list_model_catalog(project_id)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "project_id": project_id,
            "default_context_policy": settings.default_codex_context_policy,
            "models": models,
        }

    @router.get("/messages/{message_id}")
    async def get_message(message_id: UUID):
        row = await repository.get_message(str(message_id))
        if row is None:
            raise HTTPException(status_code=404, detail="message not found")
        return row

    @router.post('/prompt-confirmations')
    async def prompt_confirmation(request: PromptConfirmationRequest):
        from assistant_kernel.services.prompt_protocol import PromptProtocol
        try:
            receipt=await PromptProtocol(settings,repository).record_confirmation(
                display_message_id=str(request.display_message_id),project_id=request.project_id,channel=request.frontend,
                chat_id=request.channel_conversation_id,user_id=request.user_id,external_event_id=request.external_event_id,
                action=request.action,expected_package_sha256=request.package_sha256)
        except PermissionError as exc:raise HTTPException(status_code=403,detail=str(exc)) from exc
        except ValueError as exc:raise HTTPException(status_code=409,detail=str(exc)) from exc
        return receipt

    @router.get("/projects/{project_id}/messages")
    async def list_messages(
        project_id: str, channel: str | None = None,
        chat_id: str | None = None, limit: int = 100,
    ):
        return {"messages": await repository.list_messages(project_id, channel, chat_id, limit)}

    @router.post("/runs", status_code=status.HTTP_202_ACCEPTED)
    async def create_run(request: CreateRunRequest):
        if request.run_type == "stage":
            raise HTTPException(status_code=409, detail="Stage execution is retired; use single-agent.v2")
        try:
            runtime.ensure(request.project_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if request.run_type != "python" and request.trigger_message_id is None:
            raise HTTPException(
                status_code=422,
                detail="chat and stage runs require trigger_message_id",
            )
        agent_key = request.agent_key or f"project:{request.project_id}:{request.stage or 'manager'}"
        try:
            return await repository.create_run(
                project_id=request.project_id,
                agent_key=agent_key,
                run_type=request.run_type,
                request={**request.request, **({"shared_write": request.shared_write} if request.shared_write is not None else {})},
                stage=request.stage,
                parent_run_id=str(request.parent_run_id) if request.parent_run_id else None,
                trigger_message_id=str(request.trigger_message_id) if request.trigger_message_id else None,
                executor=request.executor,
                idempotency_key=request.idempotency_key,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.get("/runs/{run_id}")
    async def get_run(run_id: UUID):
        row = await repository.get_run(str(run_id))
        if row is None:
            raise HTTPException(status_code=404, detail="run not found")
        return row

    @router.get("/projects/{project_id}/runs")
    async def list_runs(project_id: str, limit: int = 100):
        return {"runs": await repository.list_runs(project_id, limit)}

    @router.post("/runs/{run_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
    async def cancel_run(run_id: UUID):
        return {"run_id": str(run_id), "cancel_requested": await repository.request_cancel(str(run_id))}

    @router.post("/runs/{run_id}/reconcile")
    async def reconcile_run(run_id: UUID, request: ReconcileRunRequest):
        try:
            new_status = await repository.reconcile_run(
                str(run_id), request.action, request.note
            )
        except (ValueError,PermissionError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"run_id": str(run_id), "status": new_status}

    @router.post("/runs/{run_id}/unblock")
    async def unblock_run(run_id: UUID, request: UnblockRunRequest):
        try:
            new_status = await repository.unblock_run(str(run_id), request.note)
        except (ValueError, PermissionError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"run_id": str(run_id), "status": new_status}

    @router.get("/sessions/{session_id}")
    async def get_session(session_id: UUID):
        row = await repository.get_session(str(session_id))
        if row is None:
            raise HTTPException(status_code=404, detail="session not found")
        return row

    @router.post("/deliveries/claim")
    async def claim_delivery(request: DeliveryClaimRequest):
        message = await repository.claim_delivery(
            request.worker_id, str(request.worker_instance_id),
            request.lease_seconds, request.channel,
        )
        return {"message": message}

    @router.post("/deliveries/{message_id}/ack")
    async def ack_delivery(message_id: UUID, request: DeliveryAckRequest):
        accepted = await repository.acknowledge_delivery(
            str(message_id), str(request.worker_instance_id),
            request.external_message_id, request.delivery,
        )
        return {"message_id": str(message_id), "acknowledged": accepted}

    @router.post("/deliveries/{message_id}/fail")
    async def fail_delivery(message_id: UUID, request: DeliveryFailRequest):
        state = await repository.fail_delivery(
            str(message_id), str(request.worker_instance_id), request.error
        )
        return {"message_id": str(message_id), "delivery_status": state}

    @router.post("/channels/feishu/events", status_code=status.HTTP_202_ACCEPTED)
    async def trusted_feishu_event(payload: dict[str, Any]):
        config = load_feishu_config(settings)
        return await ingest_event(
            settings, payload, config.get("app_id"), allow_explicit_route=True
        )

    @router.get("/channels/feishu/status")
    async def feishu_status():
        config = load_feishu_config(settings)
        health = read_channel_health(settings, "feishu")
        return {
            "configured": bool(config.get("app_id") and config.get("app_secret")),
            "sender_enabled": bool(config.get("enabled")),
            "callback_configured": bool(config.get("verification_token")),
            "receive_mode": config.get("receive_mode") or "disabled",
            "conversation_mode": config.get("mode") or "full",
            "gateway_enabled": bool(
                config.get("enabled") and config.get("receive_mode") == "websocket"
            ),
            "process_running": bool(health.get("process_running") and not health.get("stale")),
            "connected": bool(health.get("connected") and not health.get("stale")),
            "health": health,
            "delivery_backlog": await repository.delivery_backlog("feishu"),
        }

    @router.get("/channels/discord/status")
    async def discord_status():
        config = load_discord_config(settings)
        health = read_channel_health(settings, "discord")
        delivery_health = await repository.delivery_health("discord")
        gateway_latency = health.get("gateway_latency_seconds")
        event_loop_lag = health.get("event_loop_lag_seconds")
        discord_ready = bool(
            health.get("process_running") and health.get("connected")
            and not health.get("stale")
            and (gateway_latency is None or float(gateway_latency) <= 2.0)
            and (event_loop_lag is None or float(event_loop_lag) <= 2.0)
        )
        return {
            "configured": bool(config.get("bot_token")),
            "sender_enabled": bool(config.get("enabled")),
            "receive_mode": "gateway" if config.get("enabled") else "disabled",
            "gateway_enabled": bool(config.get("enabled") and config.get("bot_token")),
            "application_id_present": bool(config.get("application_id")),
            "guild_count": len(config.get("guild_ids") or set()),
            "allowed_channel_count": len(config.get("allowed_channel_ids") or set()),
            "allowed_user_count": len(config.get("allowed_user_ids") or set()),
            "owner_user_count": len(config.get("owner_user_ids") or set()),
            "allow_dm": bool(config.get("allow_dm")),
            "message_content": bool(config.get("message_content", True)),
            "home_channel_id": config.get("home_channel_id"),
            "auto_provision_projects": bool(config.get("auto_provision_projects")),
            "project_category_id": config.get("project_category_id"),
            "archived_channel_count": len(config.get("archived_channel_ids") or set()),
            "process_running": bool(health.get("process_running") and not health.get("stale")),
            "connected": bool(health.get("connected") and not health.get("stale")),
            "discord_ready": discord_ready,
            "degraded": bool(
                not discord_ready
                or int(delivery_health.get("failed_recent_15m") or 0) > 0
            ),
            "health": health,
            "delivery_backlog": await repository.delivery_backlog("discord"),
            "delivery_health": delivery_health,
        }

    return router
