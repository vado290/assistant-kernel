from __future__ import annotations

import json
import secrets as secret_compare
import uuid

import httpx
import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from assistant_kernel import __version__
from assistant_kernel.api.v2 import build_v2_router
from assistant_kernel.api.volcengine import build_volcengine_router
from assistant_kernel.config import KernelSettings
from assistant_kernel.model_providers import CatalogChatProvider, QwenProvider
from assistant_kernel.repositories import CoreRepository
from assistant_kernel.services.context_budget import ContextBudgetManager, TurnUsage
from assistant_kernel.services.channel_health import (
    read_channel_health, read_worker_health,
)
from assistant_kernel.storage import ProjectRuntimeManager


class EmbeddingRequest(BaseModel):
    texts: list[str]
    dimensions: int = 1024
    provider: str = "dashscope"


class RerankRequest(BaseModel):
    query: str
    documents: list[str]
    top_n: int | None = None
    provider: str = "dashscope"


class ContextBudgetRequest(BaseModel):
    usage: TurnUsage
    context_window_tokens: int = Field(gt=0)


class PythonExecutionRequest(BaseModel):
    project_id: str
    relative_script_path: str
    args: list[str] = Field(default_factory=list, max_length=100)
    timeout_seconds: int = Field(default=300, ge=1, le=3600)
    parent_run_id: str | None = None


class ChatRequest(BaseModel):
    provider: str
    model: str
    prompt: str
    system_prompt: str = ""
    max_output_tokens: int = Field(default=2048, ge=1, le=131072)
    temperature: float | None = Field(default=None, ge=0, le=2)


settings = KernelSettings.from_environment()
repository = CoreRepository(settings)
runtime_manager = ProjectRuntimeManager(settings.data_root)
app = FastAPI(title="Assistant Kernel", version=__version__)


def require_kernel_token(authorization: str | None = Header(default=None)) -> None:
    expected = settings.load_provider_secrets().internal.kernel_api_token.get_secret_value()
    if not expected:
        raise HTTPException(status_code=503, detail="kernel_api_token is not configured")
    supplied = authorization[7:] if authorization and authorization.startswith("Bearer ") else ""
    if not supplied or not secret_compare.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="invalid bearer token")


app.include_router(build_v2_router(settings, repository, require_kernel_token))
app.include_router(build_volcengine_router(settings, require_kernel_token))


@app.post("/channels/feishu/callback")
async def feishu_callback(payload: dict):
    from assistant_kernel.services.feishu import ingest_event, load_feishu_config, verify_callback

    config = load_feishu_config(settings)
    try:
        verify_callback(config, payload)
        if payload.get("type") == "url_verification":
            return {"challenge": payload.get("challenge")}
        await ingest_event(settings, payload, config.get("app_id"))
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if (payload.get("event") or {}).get("action"):
        return {"toast": {"type": "info", "content": "已收到，正在处理。"}}
    return {}


def qwen_provider() -> QwenProvider:
    config = settings.load_provider_secrets().providers.get("dashscope")
    if config is None:
        raise HTTPException(status_code=503, detail="dashscope provider is not configured")
    return QwenProvider(config)


def catalog_provider(provider: str) -> CatalogChatProvider:
    normalized = {"qwen": "dashscope", "doubao": "volcengine"}.get(provider, provider)
    config = settings.load_provider_secrets().providers.get(normalized)
    if config is None:
        raise HTTPException(status_code=404, detail="provider is not configured")
    return CatalogChatProvider(normalized, config)


@app.get("/health/live")
async def health_live() -> dict:
    return {"status": "ok", "version": __version__}


@app.get("/health/ready")
async def health_ready() -> dict:
    secrets = settings.load_provider_secrets()
    qwen = secrets.providers.get("dashscope")
    database_ready = await repository.database.ping()
    schema_version = None
    tables: list[str] = []
    reconciliation = {
        "reconciling_count": 0,
        "oldest_reconciling_seconds": 0,
        "reconciliation_cards_pending": 0,
    }
    execution = {"queued": 0, "running": 0, "oldest_queued_seconds": 0}
    discord_delivery = {
        "pending": 0, "delivering": 0, "failed_recent_15m": 0,
        "oldest_pending_delivery_seconds": 0,
    }
    if database_ready:
        try:
            schema_version = await repository.schema_version()
            tables = await repository.table_names()
            reconciliation = await repository.reconciliation_health()
            execution = await repository.execution_health()
            discord_delivery = await repository.delivery_health("discord")
        except Exception:
            database_ready = False
    schema_ready = schema_version == "core5.v6" and tables == [
        "kernel_meta", "messages", "model_invocations", "project_grants", "runs", "sessions"
    ]
    maintenance = read_channel_health(settings, "maintenance")
    maintenance_ready = bool(
        maintenance.get("process_running")
        and maintenance.get("connected")
        and not maintenance.get("stale")
    )
    discord_health = read_channel_health(settings, "discord")
    worker_health = read_worker_health(settings)
    gateway_latency = discord_health.get("gateway_latency_seconds")
    event_loop_lag = discord_health.get("event_loop_lag_seconds")
    discord_ready = bool(
        discord_health.get("process_running")
        and discord_health.get("connected")
        and not discord_health.get("stale")
        and (gateway_latency is None or float(gateway_latency) <= 2.0)
        and (event_loop_lag is None or float(event_loop_lag) <= 2.0)
    )
    execution_ready = bool(
        int(worker_health.get("fresh_count") or 0) > 0
        and int(execution.get("oldest_queued_seconds") or 0) <= 300
    )
    degraded_reasons: list[str] = []
    if not discord_health.get("connected") or discord_health.get("stale"):
        degraded_reasons.append("discord_disconnected")
    elif not discord_ready:
        degraded_reasons.append("discord_gateway_lag")
    if int(worker_health.get("fresh_count") or 0) == 0:
        degraded_reasons.append("run_worker_missing")
    if int(execution.get("oldest_queued_seconds") or 0) > 300:
        degraded_reasons.append("queued_run_stale")
    if int(discord_delivery.get("failed_recent_15m") or 0) > 0:
        degraded_reasons.append("discord_delivery_failed")
    return {
        "status": (
            "ready" if database_ready and schema_ready and maintenance_ready
            else "not_ready"
        ),
        "database": database_ready,
        "schema_version": schema_version,
        "tables": tables,
        "maintenance_ready": maintenance_ready,
        "maintenance": maintenance,
        "discord_ready": discord_ready,
        "execution_ready": execution_ready,
        "degraded": bool(degraded_reasons),
        "degraded_reasons": degraded_reasons,
        "discord": discord_health,
        "run_workers": worker_health,
        "execution": execution,
        "discord_delivery": discord_delivery,
        **reconciliation,
        "dashscope_embedding_configured": bool(qwen and QwenProvider(qwen).configured()),
        "dashscope_rerank_configured": bool(qwen and QwenProvider(qwen).rerank_configured()),
    }


@app.get("/version")
async def version() -> dict:
    return {"version": __version__, "schema_version": "core5.v6"}


@app.post("/v1/projects/{project_id}/runtime", dependencies=[Depends(require_kernel_token)])
async def ensure_project_runtime(project_id: str) -> dict:
    try:
        runtime = runtime_manager.ensure(project_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "project_id": project_id,
        "vault_uri": f"vault://{project_id}/",
        "codex_home": str(runtime.codex_home),
    }


@app.post("/v1/embeddings", dependencies=[Depends(require_kernel_token)])
async def embeddings(request: EmbeddingRequest) -> dict:
    if request.provider != "dashscope":
        raise HTTPException(status_code=501, detail="provider adapter is not implemented")
    try:
        result = await qwen_provider().embed(request.texts, request.dimensions)
        return result.model_dump(exclude={"provider_raw_data"})
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=502, detail="DashScope embedding request failed") from exc


@app.post("/v1/rerank", dependencies=[Depends(require_kernel_token)])
async def rerank(request: RerankRequest) -> dict:
    if request.provider != "dashscope":
        raise HTTPException(status_code=501, detail="provider adapter is not implemented")
    try:
        result = await qwen_provider().rerank(request.query, request.documents, request.top_n)
        return result.model_dump(exclude={"provider_raw_data"})
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=502, detail="DashScope rerank request failed") from exc


@app.post("/v1/context-budget/evaluate", dependencies=[Depends(require_kernel_token)])
async def evaluate_context_budget(request: ContextBudgetRequest) -> dict:
    return ContextBudgetManager(
        settings.context_soft_ratio, settings.context_hard_ratio
    ).evaluate(request.usage, request.context_window_tokens).model_dump()


@app.get("/v1/models", dependencies=[Depends(require_kernel_token)])
async def list_models() -> dict:
    return {
        name: {
            "enabled": provider.enabled,
            "provider_type": provider.provider_type,
            "models": list(provider.chat_models),
        }
        for name, provider in settings.load_provider_secrets().providers.items()
    }


@app.get("/v1/credentials/{category}/{name}", dependencies=[Depends(require_kernel_token)])
async def get_credential(category: str, name: str) -> dict:
    allowed = {"remote_hosts", "tunnels", "external_mappings", "databases", "websites", "services"}
    if category not in allowed:
        raise HTTPException(status_code=422, detail="unsupported credential category")
    document = json.loads(settings.service_credentials_path.read_text(encoding="utf-8-sig"))
    record = (document.get(category) or {}).get(name)
    if record is None:
        raise HTTPException(status_code=404, detail="credential record not found")
    return {"category": category, "name": name, "record": record}


@app.post("/v1/chat", dependencies=[Depends(require_kernel_token)])
async def chat(request: ChatRequest) -> dict:
    try:
        result = await catalog_provider(request.provider).chat(
            request.prompt, request.model, request.system_prompt,
            request.max_output_tokens, request.temperature,
        )
        return result.model_dump()
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=502, detail="model provider request failed") from exc


@app.post("/v1/python-executions", dependencies=[Depends(require_kernel_token)])
async def submit_python_execution(request: PythonExecutionRequest) -> dict:
    try:
        runtime = runtime_manager.ensure(request.project_id)
        source = runtime_manager.resolve_vault_file(request.project_id, request.relative_script_path)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not source.is_file() or source.suffix.lower() != ".py":
        raise HTTPException(status_code=422, detail="relative_script_path must be an existing .py file")
    source_uri = f"vault://{request.project_id}/{source.relative_to(runtime.vault_root).as_posix()}"
    return await repository.create_run(
        project_id=request.project_id,
        agent_key=f"project:{request.project_id}:python",
        run_type="python",
        parent_run_id=request.parent_run_id,
        request={
            "source_uri": source_uri,
            "args": request.args,
            "timeout_seconds": request.timeout_seconds,
        },
        idempotency_key=f"python:{uuid.uuid4()}",
    )


@app.get("/v1/python-executions/{execution_id}", dependencies=[Depends(require_kernel_token)])
async def get_python_execution(execution_id: str) -> dict:
    row = await repository.get_run(execution_id)
    if row is None or row["run_type"] != "python":
        raise HTTPException(status_code=404, detail="execution not found")
    return row


@app.post("/v1/python-executions/{execution_id}/cancel", dependencies=[Depends(require_kernel_token)])
async def cancel_python_execution(execution_id: str) -> dict:
    return {"execution_id": execution_id, "cancel_requested": await repository.request_cancel(execution_id)}


def run() -> None:
    uvicorn.run(app, host=settings.api_host, port=settings.api_port)


if __name__ == "__main__":
    run()
