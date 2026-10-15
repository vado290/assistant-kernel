"""Shared executor helpers: agent files, prompt text, documents, JSON output and the model call."""
from __future__ import annotations

import asyncio
import json
import re
import uuid
from pathlib import Path
from typing import Any

import yaml

from assistant_kernel.harness.base import AgentTurnRequest, AgentTurnResult
from assistant_kernel.services.agent_files import AgentFileLoader, FileAgentConfig
from assistant_kernel.services.document_runtime import TaskDocumentService
from assistant_kernel.storage import ProjectRuntimeManager

PROMPTS = Path(__file__).parents[2] / "prompts" / "core"
PROFILE_FOR_STAGE = {"requirements": "requirements", "design": "design", "build": "build",
                     "review": "audit", "operate": "operate"}

# Tools available in the MVP (plan D3); a project's own tool list can only narrow this.
MVP_TOOLS = frozenset({
    "get_authorized_workspace", "list_authorized_tree", "read_authorized_file",
    "write_candidate_file", "delete_candidate_file",
    "get_project_map", "query_project_map", "read_map_component", "refresh_project_map",
    "read_project_document", "list_recent_history", "list_project_changes", "read_change_diff",
    "vault_list", "vault_read_text",
    "volcengine_model_status", "volcengine_generate", "volcengine_ocr",
})
CANDIDATE_TOOLS = frozenset({"write_candidate_file", "delete_candidate_file"})
READ_ONLY_TOOLS = MVP_TOOLS - CANDIDATE_TOOLS - {"refresh_project_map", "volcengine_generate", "volcengine_ocr"}


def prompt_text(name: str) -> str:
    return (PROMPTS / f"{name}.md").read_text(encoding="utf-8").strip()


def load_agent(settings, project_id: str, role: str, stage: str | None = None) -> FileAgentConfig:
    """Load the project's live agent files (plan D2: no pinned package versions)."""
    loader = AgentFileLoader(settings)
    agent = "manager" if role == "pm" else "supervisor"
    config = loader.load(project_id, f"project:{project_id}:{agent}")
    profile_name = PROFILE_FOR_STAGE.get(stage or "")
    if role == "pm" and profile_name:
        path = loader.runtime.ensure(project_id).vault_root / "agents" / "pm" / "profiles" / f"{profile_name}.yaml"
        if path.is_file():
            config = loader.apply_phase_profile(config, yaml.safe_load(path.read_text(encoding="utf-8")) or {})
    return config


def parse_json_object(text: str) -> dict[str, Any] | None:
    """Read the single JSON object a model was asked to return (tolerates a code fence)."""
    stripped = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", (text or "").strip())
    start, end = stripped.find("{"), stripped.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(stripped[start:end + 1])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def write_document(settings, project_id: str, task_id: str, run_id: str, kind: str,
                   payload: dict[str, Any], actor: str) -> dict[str, Any]:
    ref = TaskDocumentService(settings).write_revision(
        project_id=project_id, task_id=task_id, document_kind=kind.replace("_", "-"),
        document_schema=f"core-{kind.replace('_', '-')}.v1", payload=payload, status="submitted",
        actor_type=actor, actor_id=actor, run_id=run_id, reason=f"{kind} submitted in run {run_id}")
    return {"kind": kind, "sha256": ref["sha256"], "uri": ref["uri"], "ref": ref}


def read_document(settings, project_id: str, document: dict[str, Any]) -> str:
    """Markdown body of a stored core document ('' when unreadable)."""
    try:
        payload = TaskDocumentService(settings).read(project_id, document["ref"])["payload"]
    except (KeyError, OSError, ValueError, PermissionError):
        return ""
    return str(payload.get("markdown") or "")


def render_documents(settings, task: dict[str, Any]) -> str:
    titles = {"task_contract": "需求文档", "solution_design": "设计文档",
              "implementation_report": "实施报告", "review_report": "Review 报告"}
    parts = []
    for kind, title in titles.items():
        document = task["documents"].get(kind)
        if document:
            parts.append(f"### {title}（sha {document['sha256'][:12]}）\n\n{read_document(settings, task['project_id'], document)}")
    validation = task["documents"].get("validation")
    if validation:
        parts.append(f"### Kernel 验证结果\n\n{'通过' if validation.get('passed') else '未通过'}\n\n{validation.get('report') or ''}")
    return "\n\n".join(parts) or "（暂无已生效文档）"


def workspace_snapshot(settings, project_id: str, task_id: str, variant: str, *,
                       candidates: bool) -> tuple[str, str]:
    """Pin the authorized roots for one invocation; a build snapshot is also what the publisher checks against."""
    from assistant_kernel.services.workspace_scope import WorkspaceScopeService
    uri, sha = WorkspaceScopeService(settings).snapshot(
        project_id, task_id, None, candidate_enabled=candidates, variant=variant, shared_agreement=True)
    if candidates:
        plan = ProjectRuntimeManager(settings.data_root).ensure(project_id).vault_root / "runs" / task_id / \
            "resolved" / "execution-plan-build.json"
        plan.parent.mkdir(parents=True, exist_ok=True)
        plan.write_text(json.dumps({"schema_version": "core-build-scope.v1", "bindings": {
            "workspace_scope_snapshot_uri": uri, "workspace_scope_snapshot_sha256": sha}}), "utf-8")
    return uri, sha


async def call_model(ctx, run: dict[str, Any], config: FileAgentConfig, prompt: str, *,
                     task_id: str | None, stage: str | None, session_id: str | None,
                     tools: frozenset[str], sandbox: str, candidates: bool = False) -> AgentTurnResult:
    """One Codex invocation with the project's model settings and the scoped Kernel tools."""
    settings, harness = ctx.services["settings"], ctx.services["harness"]
    project_id = run["project_id"]
    invocation_id = f"{run['run_id']}-a{run['attempt']}-{uuid.uuid4().hex[:8]}"
    allowed = tuple(sorted(tools & set(config.allowed_tools)))
    model, effort, window, compact = config.model, config.reasoning_effort, None, None
    if hasattr(harness, "resolve_model_profile"):
        profile = await harness.resolve_model_profile(
            project_id=project_id, provider=config.provider or settings.default_codex_provider,
            model=config.model or settings.default_codex_model, reasoning_effort=config.reasoning_effort,
            context_policy=config.context_policy,
            custom_context_window_tokens=config.model_context_window_tokens,
            custom_auto_compact_token_limit=config.model_auto_compact_token_limit)
        model, effort = profile.model, profile.reasoning_effort
        window, compact = profile.model_context_window_tokens, profile.model_auto_compact_token_limit
    scope_context: dict[str, str] = {}
    if allowed:
        owner = str(task_id or run["run_id"])
        scope_uri, scope_sha = workspace_snapshot(settings, project_id, owner, invocation_id, candidates=candidates)
        scope_context = {
            "ASSISTANT_DATA_ROOT": str(settings.data_root), "POSTGRES_DB": settings.postgres_database,
            "KERNEL_SCOPE_PROJECT_ID": project_id, "KERNEL_SCOPE_RUN_ID": str(run["run_id"]),
            "KERNEL_SCOPE_TASK_ID": owner, "KERNEL_SCOPE_CLAIM_TOKEN": str(run["claim_token"]),
            "KERNEL_SCOPE_WORKER_INSTANCE_ID": str(run["worker_id"]), "KERNEL_SCOPE_CORE": "1",
            "KERNEL_SCOPE_ALLOWED_TOOLS": ",".join(allowed), "KERNEL_SCOPE_STAGE": stage or "",
            "KERNEL_SCOPE_SNAPSHOT_URI": scope_uri, "KERNEL_SCOPE_SNAPSHOT_SHA256": scope_sha,
            "KERNEL_SCOPE_EFFECTIVE_PERMISSIONS_JSON": "{}", "KERNEL_SCOPE_CONTEXT_VIEW_JSON": "{}",
        }
    from assistant_kernel.core.executors.auth_sync import sync
    await asyncio.to_thread(sync, settings, project_id, direction="in")
    try:
        return await harness.run_turn(AgentTurnRequest(
            project_id=project_id, prompt=prompt,
            cwd=ProjectRuntimeManager(settings.data_root).ensure(project_id).vault_root,
            physical_session_id=session_id, provider=config.provider, model=model, reasoning_effort=effort,
            context_policy=config.context_policy, model_context_window_tokens=window,
            model_auto_compact_token_limit=compact, sandbox=sandbox, timeout_seconds=config.timeout_seconds,
            invocation_id=invocation_id, cancel_event=ctx.cancel, scoped_tools=allowed, scope_context=scope_context))
    finally:
        await asyncio.to_thread(sync, settings, project_id, direction="out")


async def call_for_json(ctx, run, config, prompt, **kwargs) -> tuple[dict[str, Any] | None, AgentTurnResult]:
    """Call the model; if the final message is not a JSON object, ask once in the same session to fix it."""
    result = await call_model(ctx, run, config, prompt, **kwargs)
    parsed = parse_json_object(result.final_text)
    if parsed is None and result.physical_session_id:
        fix = "你上一条消息不是一个合法的 JSON 对象。请严格按输出要求，只输出那个 JSON 对象，不要做任何新的工作。"
        result = await call_model(ctx, run, config, fix, **{**kwargs, "session_id": result.physical_session_id})
        parsed = parse_json_object(result.final_text)
    return parsed, result
