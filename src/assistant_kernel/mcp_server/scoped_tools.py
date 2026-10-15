"""Per-invocation stdio tools constrained to one leased Run and Project."""
from __future__ import annotations

import os
import hashlib
import json
from typing import Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from assistant_kernel.config import KernelSettings
from assistant_kernel.mcp_server.error_protocol import install_error_protocol, v12_tool_errors
from assistant_kernel.repositories import CoreRepository
from assistant_kernel.services.project_directory import ProjectDirectoryService
from assistant_kernel.services.project_credentials import ProjectCredentialsStore
from assistant_kernel.services.source_scope import SourceScopeService
from assistant_kernel.services.capability_bundle import CapabilityBundleService
from assistant_kernel.services.stage_handoff import StageHandoffService
from assistant_kernel.services.agent_files import AgentFileLoader
from assistant_kernel.services.workspace_scope import WorkspaceScopeService
from assistant_kernel.services.semantic_map import SemanticMapService
from assistant_kernel.services.project_changes import ProjectChangeService
from assistant_kernel.model_providers import VolcengineGateway
from assistant_kernel.storage import ProjectRuntimeManager


settings = KernelSettings.from_environment()
repository = CoreRepository(settings)
runtime = ProjectRuntimeManager(settings.data_root)
projects = ProjectDirectoryService(settings, repository)
source_scopes = SourceScopeService(settings, repository)
project_credentials = ProjectCredentialsStore(settings.data_root)
project_id = os.environ["KERNEL_SCOPE_PROJECT_ID"]
run_id = os.environ["KERNEL_SCOPE_RUN_ID"]
claim_token = os.environ["KERNEL_SCOPE_CLAIM_TOKEN"]
worker_instance_id = os.environ["KERNEL_SCOPE_WORKER_INSTANCE_ID"]
stage = os.environ.get("KERNEL_SCOPE_STAGE") or None
allowed = {
    item.strip() for item in os.environ.get("KERNEL_SCOPE_ALLOWED_TOOLS", "").split(",")
    if item.strip()
}
workspace_scope = WorkspaceScopeService(settings)
semantic_maps = SemanticMapService(settings)
project_changes = ProjectChangeService(settings)
capabilities = CapabilityBundleService(settings)
handoffs = StageHandoffService(settings)
agent_files = AgentFileLoader(settings)
volcengine_models = VolcengineGateway(settings)
scope_snapshot_uri = os.environ.get("KERNEL_SCOPE_SNAPSHOT_URI", "").strip()
scope_snapshot_sha256 = os.environ.get("KERNEL_SCOPE_SNAPSHOT_SHA256", "").strip()
if bool(scope_snapshot_uri) != bool(scope_snapshot_sha256):
    raise ValueError("workspace scope snapshot URI and hash must be provided together")
# Rebuilt core (KERNEL_SCOPE_CORE=1): leases live in core.runs, and the workspace snapshot and
# candidate area belong to the task, so candidate files accumulate across build turns.
core_mode = os.environ.get("KERNEL_SCOPE_CORE") == "1"
scope_owner = (os.environ.get("KERNEL_SCOPE_TASK_ID") or run_id) if core_mode else run_id
snapshot_stage = None if core_mode else stage


async def check_lease() -> None:
    if core_mode:
        from assistant_kernel.core.db import Database, one
        async with Database(settings).tx() as c:
            held = await one(c, """SELECT 1 FROM core.runs WHERE run_id=%s AND claim_token=%s
                                   AND status='running' AND NOT cancel_requested""", (run_id, claim_token))
        if held is None:
            raise PermissionError("this invocation's run is no longer active")
        return
    await repository.claimed_run_source(run_id=run_id, worker_instance_id=worker_instance_id, claim_token=claim_token)


authorized_roots = (
    workspace_scope.load_snapshot(
        project_id, scope_owner, snapshot_stage, scope_snapshot_uri, scope_snapshot_sha256,
    )
    if scope_snapshot_uri else workspace_scope.resolve(project_id, scope_owner, snapshot_stage)
)
effective_permissions = json.loads(
    os.environ.get("KERNEL_SCOPE_EFFECTIVE_PERMISSIONS_JSON", "{}")
)
if not isinstance(effective_permissions, dict):
    raise ValueError("effective Run permissions must be an object")
context_view = json.loads(os.environ.get("KERNEL_SCOPE_CONTEXT_VIEW_JSON", "{}"))
if not isinstance(context_view, dict):
    raise ValueError("Run context view must be an object")
context_roots = {
    str(item.get("key")): item
    for item in (context_view.get("roots") or [])
    if isinstance(item, dict) and str(item.get("key") or "")
}


def context_tree_arguments() -> tuple[dict[str, tuple[str, ...]], dict[str, tuple[str, ...]]]:
    selected = {
        key: tuple(str(path) for path in (item.get("initial_paths") or []))
        for key, item in context_roots.items()
    }
    excludes = {
        key: tuple(str(pattern) for pattern in (item.get("soft_excludes") or []))
        for key, item in context_roots.items()
    }
    return selected, excludes
scope_initialized = False
READ_ONLY = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
SCOPED_WRITE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
MODEL_CALL = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True
)


server = MCPServer(
    name="kernel-scoped-tools",
    instructions=(
        "These tools are fixed to the current Project and leased Run. Targets outside "
        "that scope are rejected. A successful tool result is not human approval."
    ),
)

install_error_protocol(server)


def safe_file(relative_path: str):
    return runtime.resolve_vault_file(project_id, relative_path)


def require_scope_initialized() -> None:
    if not scope_initialized:
        raise PermissionError("call get_authorized_workspace before using scoped tools")




async def current_project_task_source() -> tuple[dict, str]:
    """Resolve Kernel-owned Root and original user source from this leased invocation."""
    await repository.claimed_run_source(run_id=run_id, worker_instance_id=worker_instance_id,
                                         claim_token=claim_token)
    current = await repository.get_run(run_id)
    if current is None or str(current["project_id"]) != project_id:
        raise PermissionError("current Task belongs to another Project")
    request = dict(current.get("request_json") or {})
    task_id = str(request.get("task_root_run_id") or request.get("task_id") or run_id)
    task = await repository.get_run(task_id)
    if task is None or str(task["project_id"]) != project_id:
        raise PermissionError("Root Task belongs to another Project")
    task_request = dict(task.get("request_json") or {})
    source_id = str(task_request.get("source_message_id") or task.get("trigger_message_id") or "")
    source = await repository.get_message(source_id)
    if source is None or str(source["project_id"]) != project_id or source.get("direction") != "inbound":
        raise PermissionError("Task needs an original user Message in this Project")
    return task, source_id


if any(name in allowed for name in {
    "get_system_config", "validate_system_config", "update_system_config", "restore_system_config",
}):
    from assistant_kernel.services.system_config import SystemConfigService
    system_configuration = SystemConfigService(settings)


if "get_system_config" in allowed:
    @server.tool(annotations=READ_ONLY)
    @v12_tool_errors()
    async def get_system_config(view: str = "status", paths: list[str] | None = None) -> dict:
        """Read desired or actually adopted values; no credentials are returned."""
        require_scope_initialized()
        await repository.claimed_run_source(run_id=run_id, worker_instance_id=worker_instance_id,
                                            claim_token=claim_token)
        return await system_configuration.view(view=view, paths=paths or [], management=project_id == "kernel")


if "validate_system_config" in allowed:
    @server.tool(annotations=READ_ONLY)
    async def validate_system_config(candidate: dict) -> dict:
        raise PermissionError("This management tool is retired; no configuration was changed")


if "update_system_config" in allowed:
    @server.tool(annotations=SCOPED_WRITE)
    async def update_system_config(changes: list[dict], expected_revision: str, reason: str) -> dict:
        raise PermissionError("This management tool is retired; no configuration was changed")


if "restore_system_config" in allowed:
    @server.tool(annotations=SCOPED_WRITE)
    async def restore_system_config(revision: str, expected_revision: str) -> dict:
        raise PermissionError("This management tool is retired; no configuration was changed")


if "read_project_document" in allowed:
    @server.tool(annotations=READ_ONLY)
    @v12_tool_errors()
    async def read_project_document(ref: dict, max_bytes: int = 262144) -> dict:
        """Read a pinned Project document with identity, byte and contract checks."""
        require_scope_initialized()
        await check_lease()
        from assistant_kernel.services.document_runtime import TaskDocumentService, validate_resource_ref
        normalized = validate_resource_ref(ref)
        prefix = f"vault://{project_id}/"
        if normalized["kind"] != "document" or not str(normalized["uri"]).startswith(prefix):
            raise PermissionError("document is not in the current Project")
        if not 1 <= max_bytes <= 2097152:
            raise ValueError("max_bytes must be 1..2097152")
        path = runtime.resolve_vault_file(project_id, normalized["uri"][len(prefix):])
        if path.stat().st_size > max_bytes:
            raise ValueError("document exceeds the requested read limit; request an appropriate bounded source")
        document = TaskDocumentService(settings).read(project_id, normalized)
        return {"ref":normalized,"document_schema":document["schema_version"],
                "actual_scope":document.get("scope", "task"),"payload":document["payload"],
                "file_sha256":normalized["sha256"],"size":path.stat().st_size,
                "coverage":{"status":"complete","covered_refs":[normalized],"missing_refs":[],"notes":[]}}


if "volcengine_model_status" in allowed:
    @server.tool(annotations=READ_ONLY)
    def volcengine_model_status() -> dict:
        """Return shared Volcengine capability status and fixed model names, never keys."""
        require_scope_initialized()
        return volcengine_models.status()


if "volcengine_generate" in allowed:
    @server.tool(annotations=MODEL_CALL)
    async def volcengine_generate(
        prompt: str,
        system_prompt: str = "",
        max_tokens: int = 4096,
        thinking: Literal["enabled", "disabled", "auto"] = "disabled",
    ) -> dict:
        """Call the shared low-cost Volcengine text model through the Kernel Worker."""
        require_scope_initialized()
        result = await volcengine_models.generate(
            prompt,
            system_prompt=system_prompt,
            max_tokens=max_tokens,
            thinking=thinking,
        )
        return result.model_dump()


if "volcengine_ocr" in allowed:
    @server.tool(annotations=MODEL_CALL)
    async def volcengine_ocr(
        image_url: str,
        prompt: str = "Extract all visible text faithfully and return Markdown only.",
        max_tokens: int = 8192,
    ) -> dict:
        """Extract visible text from one HTTPS or data-URI image through the shared model."""
        require_scope_initialized()
        result = await volcengine_models.ocr(
            image_url, prompt=prompt, max_tokens=max_tokens,
        )
        return result.model_dump()


if "get_authorized_workspace" in allowed:
    @server.tool(annotations=READ_ONLY)
    def get_authorized_workspace(
        max_depth: int = 2, limit: int = 80,
        include_resource_guide: bool = False,
    ) -> dict:
        """Initialize this invocation and return roots plus the compact per-Run context view."""
        global scope_initialized
        selected_paths, soft_excludes = context_tree_arguments()
        entries, truncated = workspace_scope.tree(
            authorized_roots, max_depth=min(max_depth, 3), limit=min(limit, 100),
            selected_paths=selected_paths, soft_excludes=soft_excludes,
        )
        scope_initialized = True
        result = {
            "project_id": project_id,
            "run_id": run_id,
            "stage": stage,
            "roots": [
                {
                    "key": item.key,
                    "path": str(item.path),
                    "access": item.access,
                    "mcp_access": item.access,
                    "native_access": (
                        "none" if item.key == "project" or item.native_mode == "mcp_only"
                        else "write" if item.access == "write" and (
                            item.key in {"candidate", "run_work"} or item.write_mode == "direct"
                        ) else "read"
                    ),
                    "effective_access": (
                        "write" if item.access == "write" and (
                            item.key in {"candidate", "run_work"} or (
                                item.write_mode == "direct"
                                and item.native_mode in {"enforced", "behavioral"}
                                and effective_permissions.get("workspace_file_access") == "read_write_execute"
                            )
                        )
                        else "read"
                    ),
                    "description": item.description,
                    "includes": list(item.includes),
                    "excludes": list(item.excludes),
                    "publish_allowed": item.publish_allowed,
                    "grant_id": item.grant_id,
                    "write_mode": item.write_mode,
                    "native_mode": item.native_mode,
                    "recursive": item.recursive,
                    "permission_package": item.permission_package,
                }
                for item in authorized_roots
            ],
            "file_tree": entries,
            "truncated": truncated,
            "context_view": context_view,
            "allowed_tools": sorted(allowed),
            "stage_permissions": effective_permissions or {
                "workspace_file_access": (
                    "read_write_execute"
                    if any(item.access == "write" for item in authorized_roots)
                    else "project_only"
                ),
                "formal_write": any(item.access == "write" for item in authorized_roots),
                "network": True,
                "account_connect": False,
                "trade": False,
            },
        }
        if include_resource_guide:
            result["resource_guide"] = workspace_scope.resource_guide(project_id)
        return result


if "get_project_map" in allowed:
    @server.tool(annotations=READ_ONLY)
    async def get_project_map(file_limit: int = 100) -> dict:
        """Return the Project Semantic Map header and compact component catalog."""
        require_scope_initialized()
        refresh_error = None
        try:
            await project_changes.refresh_map(project_id, roots=authorized_roots, run_id=run_id)
        except Exception as exc:
            semantic_maps.mark_stale(project_id, reason=f"source reconciliation failed: {type(exc).__name__}")
            refresh_error = {"type": type(exc).__name__, "message": str(exc)[:300]}
        payload = semantic_maps.load(project_id)
        by_key = {item.key: item for item in authorized_roots}
        diagnostics = []
        for component in payload.get("components") or []:
            expected = component.get("source_sha256")
            expected_values = (
                [str(item) for item in expected]
                if isinstance(expected, list) else [str(expected)] if expected else []
            )
            for index, uri in enumerate(component.get("source_refs") or []):
                uri = str(uri)
                if not uri.startswith("workspace://"):
                    continue
                parts = uri[len("workspace://"):].split("/", 1)
                root = by_key.get(parts[0])
                target = (
                    root.path / parts[1] if root is not None and len(parts) > 1
                    else root.path if root is not None else None
                )
                status = "missing"
                actual = None
                if target is not None and target.is_dir():
                    status = "present_directory"
                elif target is not None and target.is_file():
                    actual = hashlib.sha256(target.read_bytes()).hexdigest()
                    expected_sha = expected_values[index] if index < len(expected_values) else None
                    status = "current" if not expected_sha or expected_sha == actual else "stale"
                diagnostics.append({
                    "component_id": component.get("component_id"),
                    "uri": uri, "status": status,
                    "expected_sha256": (
                        expected_values[index] if index < len(expected_values) else None
                    ),
                    "actual_sha256": actual,
                })
        return {
            "schema_version": payload.get("schema_version"),
            "project_id": project_id,
            "base_pack_version": payload.get("base_pack_version"),
            "map_schema_version": payload.get("map_schema_version"),
            "authority": payload.get("authority") or {},
            "components": [
                {
                    "component_id": item.get("component_id"),
                    "type": item.get("type"),
                    "purpose": item.get("purpose"),
                    "criticality": item.get("criticality"),
                }
                for item in payload.get("components") or []
            ],
            "unclassified_sources": payload.get("unclassified_sources") or [],
            "source_diagnostics": diagnostics,
            "files": (payload.get("files") or [])[:min(max(file_limit, 1), 1000)],
            "file_count": len(payload.get("files") or []),
            "files_truncated": len(payload.get("files") or []) > min(max(file_limit, 1), 1000),
            "map_ref": semantic_maps.current_ref(project_id),
            "refresh_error": refresh_error,
        }


if "refresh_project_map" in allowed:
    @server.tool(annotations=SCOPED_WRITE)
    async def refresh_project_map(source_uris: list[str] | None = None) -> dict:
        """Reconcile only this Project's derived Map and unknown source-change evidence."""
        require_scope_initialized()
        for uri in source_uris or []:
            workspace_scope.resolve_uri(authorized_roots, uri)
        return await project_changes.refresh_map(project_id, roots=authorized_roots, run_id=run_id,
                                                source_uris=source_uris)


if "list_project_changes" in allowed:
    @server.tool(annotations=READ_ONLY)
    async def list_project_changes(
        lookback_days: int = 30, limit: int = 50, cursor: str | None = None,
        root_key: str | None = None, path: str | None = None,
    ) -> dict:
        """Read recent evidence in this fixed Project; reasons do not grant authorization."""
        require_scope_initialized()
        if root_key and root_key not in {item.key for item in authorized_roots}:
            raise PermissionError("workspace root is outside the authorized scope")
        return await project_changes.list_project_changes(
            project_id, lookback_days=lookback_days, limit=limit, cursor=cursor,
            root_key=root_key, path=path)


if "read_change_diff" in allowed:
    @server.tool(annotations=READ_ONLY)
    async def read_change_diff(change_id: str, max_bytes: int = 262144) -> dict:
        """Read a stored SHA-checked diff for a change registered in the current Project."""
        require_scope_initialized()
        return await project_changes.read_change_diff(project_id, change_id, max_bytes=max_bytes)


if "get_public_background_fields" in allowed:
    @server.tool(annotations=READ_ONLY)
    def get_public_background_fields(fields: list[str] | None = None) -> dict:
        """Read confirmed public fields; Project-local intent hypotheses are excluded."""
        require_scope_initialized()
        from assistant_kernel.services.public_background import PublicBackgroundService
        return PublicBackgroundService(settings.data_root,settings).read_project_context(project_id, fields=fields)


if "project_task_status" in allowed:
    @server.tool(annotations=READ_ONLY)
    async def project_task_status() -> dict:
        """Read this Project's foreground, suspended and queued work."""
        require_scope_initialized()
        from assistant_kernel.services.task_queue import ProjectTaskQueue
        return await ProjectTaskQueue(settings, repository).status(project_id)


if "queue_project_task" in allowed:
    @server.tool(annotations=SCOPED_WRITE)
    async def queue_project_task(goal: str, priority: str = "normal", task_kind: str = "delivery") -> dict:
        """Queue work from this invocation's original user source; no cross-Project target."""
        require_scope_initialized()
        _, source_id = await current_project_task_source()
        from assistant_kernel.services.task_queue import ProjectTaskQueue
        return await ProjectTaskQueue(settings, repository).queue(
            project_id, goal, source_id, priority=priority, task_kind=task_kind)


if "request_operation_report" in allowed:
    @server.tool(annotations=SCOPED_WRITE)
    async def request_operation_report(report_window: dict) -> dict:
        """Request one normal queued report for a stable approved operating window."""
        require_scope_initialized()
        _, source_id = await current_project_task_source()
        from assistant_kernel.services.operations import ProjectOperations
        return await ProjectOperations(settings, repository).request_report(project_id, report_window, source_id)


if "run_project_action" in allowed:
    @server.tool(annotations=SCOPED_WRITE)
    async def run_project_action(action_id: str, inputs: dict, idempotency_key: str) -> dict:
        """Dispatch only an approved fixed runbook script; command/source overrides are impossible."""
        require_scope_initialized()
        task, _ = await current_project_task_source()
        from assistant_kernel.services.operations import ProjectOperations
        return await ProjectOperations(settings, repository).run_action(
            project_id, str(task["run_id"]), action_id, inputs, idempotency_key)


if 'submit_project_python' in allowed:
    @server.tool(annotations=SCOPED_WRITE)
    async def submit_project_python(manifest_ref:dict,inputs:dict,idempotency_key:str,execution_kind:str,
        runtime_unit_ref:dict|None=None,logical_execution_id:str|None=None,resume_owner:dict|None=None)->dict:
        """Use the fixed finite manifest and actual current invocation authority."""
        require_scope_initialized()
        await repository.claimed_run_source(run_id=run_id,worker_instance_id=worker_instance_id,claim_token=claim_token)
        current=await repository.get_run(run_id)
        from assistant_kernel.services.project_python import ProjectPython
        return await ProjectPython(settings,repository).submit(project_id,{
            'manifest_ref':manifest_ref,'inputs':inputs,'idempotency_key':idempotency_key,'execution_kind':execution_kind,
            'runtime_unit_ref':runtime_unit_ref,'logical_execution_id':logical_execution_id,'resume_owner':resume_owner},source_run=current)


if 'preview_schedule' in allowed:
    @server.tool(annotations=READ_ONLY)
    async def preview_schedule(rule:dict,timezone:str='Asia/Shanghai')->dict:
        """Validate time rules and show the next five actual instants; never register or enable."""
        require_scope_initialized()
        from assistant_kernel.services.schedule_time import preview
        return preview(rule,timezone)

if 'register_schedule' in allowed:
    @server.tool(annotations=SCOPED_WRITE)
    async def register_schedule(definition_ref:dict,idempotency_key:str,authorization_source_refs:list[dict],expected_control_revision:int=0)->dict:
        """Register a fixed schedule in confirmed Operate; registration never enables it."""
        require_scope_initialized();task,_=await current_project_task_source()
        from assistant_kernel.services.schedules import Schedules
        return await Schedules(settings,repository).register(project_id,definition_ref,task_id=str(task['run_id']),
            idempotency_key=idempotency_key,authorization_source_refs=authorization_source_refs,expected_control_revision=expected_control_revision)

if 'control_schedule' in allowed:
    @server.tool(annotations=SCOPED_WRITE)
    async def control_schedule(schedule_id:str,action:str,expected_control_revision:int,idempotency_key:str,
                               authorization_source_refs:list[dict],target_definition_ref:dict|None=None)->dict:
        """Update, enable, pause, resume, retire, run_now or cancel_current with real Operate authority."""
        require_scope_initialized();task,_=await current_project_task_source()
        from assistant_kernel.services.schedules import Schedules
        return await Schedules(settings,repository).control(project_id,{'schedule_id':schedule_id,'action':action,
            'expected_control_revision':expected_control_revision,'idempotency_key':idempotency_key,
            'authorization_source_refs':authorization_source_refs,'target_definition_ref':target_definition_ref},task_id=str(task['run_id']))

if 'list_schedules' in allowed:
    @server.tool(annotations=READ_ONLY)
    async def list_schedules(limit:int=20,cursor:str|None=None)->dict:
        require_scope_initialized()
        from assistant_kernel.services.schedules import Schedules
        return await Schedules(settings,repository).list(project_id,limit,cursor)

if 'schedule_status' in allowed:
    @server.tool(annotations=READ_ONLY)
    async def schedule_status(schedule_id:str)->dict:
        require_scope_initialized()
        from assistant_kernel.services.schedules import Schedules
        return await Schedules(settings,repository).status(project_id,schedule_id)

if 'list_schedule_ticks' in allowed:
    @server.tool(annotations=READ_ONLY)
    async def list_schedule_ticks(schedule_id:str,limit:int=20,cursor:str|None=None)->dict:
        require_scope_initialized()
        from assistant_kernel.services.schedules import Schedules
        return await Schedules(settings,repository).ticks(project_id,schedule_id,limit,cursor)

if 'register_runtime_unit' in allowed:
    @server.tool(annotations=SCOPED_WRITE)
    async def register_runtime_unit(definition_ref:dict,review_ref:dict,publication_ref:dict,expected_control_revision:int=0)->dict:
        """Register an actually accepted reviewed/published definition; registration never enables it."""
        require_scope_initialized()
        task,_=await current_project_task_source()
        if task['request_json'].get('current_stage')!='operate':raise PermissionError('Registration follows publication in Operate')
        from assistant_kernel.services.runtime_units import RuntimeUnits
        service=RuntimeUnits(settings,repository)
        return service.registration(await service.register(project_id,definition_ref,review_ref=review_ref,
            publication_ref=publication_ref,expected_control_revision=expected_control_revision))


if 'write_runtime_document' in allowed:
    @server.tool(annotations=SCOPED_WRITE)
    async def write_runtime_document(document_kind:str,payload:dict)->dict:
        """Write a fixed runtime candidate envelope; never grant execution or accepted status."""
        require_scope_initialized()
        task,_=await current_project_task_source()
        from assistant_kernel.services.runtime_documents import write_runtime_document as write
        return write(settings,task,document_kind,payload)


if 'resolve_runtime_config' in allowed:
    @server.tool(annotations=READ_ONLY)
    async def resolve_runtime_config(bindings:list[dict])->dict:
        require_scope_initialized();task,_=await current_project_task_source()
        from assistant_kernel.services.runtime_documents import resolve_runtime_config as resolve
        return await resolve(settings,task,bindings)


if 'list_runtime_units' in allowed:
    @server.tool(annotations=READ_ONLY)
    async def list_runtime_units(limit:int=20,cursor:str|None=None)->dict:
        require_scope_initialized()
        from assistant_kernel.services.runtime_units import RuntimeUnits
        return await RuntimeUnits(settings,repository).list(project_id,limit,cursor)


if 'runtime_unit_status' in allowed:
    @server.tool(annotations=READ_ONLY)
    async def runtime_unit_status(runtime_unit_ref:dict)->dict:
        require_scope_initialized()
        from assistant_kernel.services.runtime_units import RuntimeUnits
        return await RuntimeUnits(settings,repository).view(project_id,runtime_unit_ref)


if 'control_runtime_unit' in allowed:
    @server.tool(annotations=SCOPED_WRITE)
    async def control_runtime_unit(runtime_unit_ref:dict,action:str,expected_control_revision:int,idempotency_key:str,
            authorization_source_refs:list[dict],target_definition_ref:dict|None,stop_policy:str,cause:str,tick_ref:None,
            expected_instance_ref:dict|None,expires_at:str|None)->dict:
        request={'runtime_unit_ref':runtime_unit_ref,'action':action,'expected_control_revision':expected_control_revision,
            'idempotency_key':idempotency_key,'authorization_source_refs':authorization_source_refs,'target_definition_ref':target_definition_ref,
            'stop_policy':stop_policy,'cause':cause,'tick_ref':tick_ref,'expected_instance_ref':expected_instance_ref,'expires_at':expires_at}
        require_scope_initialized();task,_=await current_project_task_source()
        state=task['request_json'];confirmation=state.get('prompt_confirmation_message_id')
        if state.get('current_stage')!='operate' or not confirmation or not state.get('confirmed_turn_package_ref'):
            raise PermissionError('Runtime control requires this actual confirmed Operate Turn')
        if not any(x.get('uri')=='db://assistant/messages/'+confirmation for x in request.get('authorization_source_refs',[])):
            raise PermissionError('Control must include the current concrete Prompt confirmation, not only an old execution scope')
        from assistant_kernel.services.runtime_units import RuntimeUnits
        from assistant_kernel.services.document_runtime import TaskDocumentService
        ref=await RuntimeUnits(settings,repository).control(project_id,request,authenticated_operator=True)
        return TaskDocumentService(settings).read(project_id,ref)['payload']


if 'write_operation_document' in allowed:
    @server.tool(annotations=SCOPED_WRITE)
    async def write_operation_document(document_kind:str,payload:dict)->dict:
        require_scope_initialized();task,_=await current_project_task_source()
        from assistant_kernel.services.operation_records import OperationRecords
        return await OperationRecords(settings,repository).write(task,document_kind,payload)


if 'query_project_operations' in allowed:
    @server.tool(annotations=READ_ONLY)
    async def query_project_operations(kind:str,object_id:str|None=None,lookback_days:int=7,limit:int=50,cursor:str|None=None)->dict:
        require_scope_initialized()
        from assistant_kernel.services.operation_records import OperationRecords
        return await OperationRecords(settings,repository).query(project_id,{'kind':kind,'object_id':object_id,'lookback_days':lookback_days,'limit':limit,'cursor':cursor})


if 'set_operation_monitor_policy' in allowed:
    @server.tool(annotations=SCOPED_WRITE)
    async def set_operation_monitor_policy(policy_ref:dict,authorization_source_refs:list[dict],active:bool)->dict:
        require_scope_initialized();task,_=await current_project_task_source()
        if task['request_json'].get('current_stage')!='operate':raise PermissionError('Policy adoption belongs to Operate')
        from assistant_kernel.services.operation_records import OperationRecords
        return await OperationRecords(settings,repository).activate_policy(project_id,policy_ref,authorization_source_refs,active=active,authenticated_operator=True)


if "query_project_map" in allowed:
    @server.tool(annotations=READ_ONLY)
    def query_project_map(
        task_kind: str, query: str = "", component_ids: list[str] | None = None,
        limit: int = 20,
    ) -> dict:
        """Select relevant components by task profile, terms and explicit IDs."""
        require_scope_initialized()
        return semantic_maps.query(
            project_id, task_kind=str(task_kind or "chat"),
            query_terms=[str(query or "")], component_ids=component_ids or [],
            limit=limit, roots=authorized_roots,
        )


if "read_map_component" in allowed:
    @server.tool(annotations=READ_ONLY)
    def read_map_component(component_id: str) -> dict:
        """Read one registered component without scanning the source tree."""
        require_scope_initialized()
        for item in semantic_maps.load(project_id).get("components") or []:
            if str(item.get("component_id")) == str(component_id):
                return {**dict(item), "source_diagnostics": semantic_maps.validate_sources(
                    project_id, roots=authorized_roots, component_ids=[str(component_id)])}
        raise FileNotFoundError(f"semantic component is not registered: {component_id}")


if "list_authorized_tree" in allowed:
    @server.tool(annotations=READ_ONLY)
    def list_authorized_tree(
        root_key: str = "project", relative_path: str = "", max_depth: int = 2,
        limit: int = 200, include_soft_excluded: bool = False,
    ) -> dict:
        """Browse one authorized root/path on demand; this never expands authorization."""
        require_scope_initialized()
        if root_key not in {item.key for item in authorized_roots}:
            raise PermissionError("workspace root is outside the authorized scope")
        root_context = context_roots.get(root_key) or {}
        soft_excludes = {} if include_soft_excluded else {
            root_key: tuple(str(item) for item in (root_context.get("soft_excludes") or []))
        }
        entries, truncated = workspace_scope.tree(
            authorized_roots, max_depth=max_depth, limit=limit,
            selected_paths={root_key: (relative_path,)}, soft_excludes=soft_excludes,
        )
        return {
            "root_key": root_key, "relative_path": relative_path,
            "files": entries, "truncated": truncated,
            "soft_filters_applied": not include_soft_excluded,
        }


if "read_authorized_file" in allowed:
    @server.tool(annotations=READ_ONLY)
    async def read_authorized_file(uri: str, max_bytes: int = 262144) -> dict:
        """Read one UTF-8 file addressed by an authorized workspace URI."""
        require_scope_initialized()
        read_root, target, _ = workspace_scope.resolve_uri(authorized_roots, uri)
        if not target.is_file():
            raise FileNotFoundError(uri)
        limit = min(max(max_bytes, 1), 2 * 1024 * 1024)
        data = target.read_bytes()
        if len(data) > limit:
            raise ValueError(f"file exceeds max_bytes={limit}")
        digest = hashlib.sha256(data).hexdigest()
        _, _, relative = workspace_scope.resolve_uri(authorized_roots, uri)
        if not semantic_maps._skip_file(relative, project_vault=read_root.key == "project"):
            registered = next((item for item in semantic_maps.load(project_id).get("files") or []
                               if item.get("uri") == uri), None)
            if registered is None or registered.get("sha256") != digest:
                semantic_maps.mark_stale(project_id, reason="source file changed on read")
                try:
                    await project_changes.refresh_map(project_id, roots=authorized_roots, run_id=run_id,
                                                      source_uris=[uri])
                except Exception:
                    # The verified file read remains usable; the index explicitly stays stale.
                    pass
        return {
            "uri": uri,
            "text": data.decode("utf-8"),
            "sha256": digest,
            "size": len(data),
        }


if "list_project_credentials" in allowed:
    @server.tool(annotations=READ_ONLY)
    def list_project_credentials() -> dict:
        """List current-Project credential names and field names without values."""
        require_scope_initialized()
        return project_credentials.list_metadata(project_id)


if "get_project_credential" in allowed:
    @server.tool(annotations=READ_ONLY)
    def get_project_credential(credential_key: str) -> dict:
        """Read one plaintext credential from the current Project."""
        require_scope_initialized()
        return project_credentials.get(project_id, credential_key)


if "request_project_credential" in allowed:
    @server.tool(annotations=SCOPED_WRITE)
    def request_project_credential(
        credential_key: str, description: str, fields: list[dict],
    ) -> dict:
        """Ask in the current chat and register its one-to-one credential card."""
        require_scope_initialized()
        return project_credentials.request(
            project_id=project_id, run_id=run_id, credential_key=credential_key,
            description=description, fields=fields,
        )


if "delete_project_credential" in allowed:
    @server.tool(annotations=SCOPED_WRITE)
    def delete_project_credential(credential_key: str) -> dict:
        """Delete one current-Project credential after an explicit user request."""
        require_scope_initialized()
        return project_credentials.delete(project_id, credential_key)


if "list_source_discovery_roots" in allowed:
    @server.tool(annotations=READ_ONLY)
    def list_source_discovery_roots() -> dict:
        """List Kernel-approved roots that may be inspected as metadata only."""
        require_scope_initialized()
        return source_scopes.list_roots()


if "inspect_source_path" in allowed:
    @server.tool(annotations=READ_ONLY)
    def inspect_source_path(
        discovery_id: str, relative_path: str = "", max_depth: int = 2,
        limit: int = 200, name_query: str = "",
    ) -> dict:
        """Inspect names, counts, size, extensions and Git state without file contents."""
        require_scope_initialized()
        return source_scopes.inspect(
            discovery_id, relative_path, max_depth=max_depth,
            limit=limit, name_query=name_query,
        )


if "propose_source_scope" in allowed:
    @server.tool(annotations=SCOPED_WRITE)
    async def propose_source_scope(
        bindings: list[dict], reason: str, target_project_id: str | None = None,
        resume_blocked_run_id: str | None = None,
        authorization_mode: str = "explicit_request",
    ) -> dict:
        """Persist a hash-fixed record for user-requested external workspaces."""
        require_scope_initialized()
        from assistant_kernel.services.directory_assessment import configured_parameters
        parameters=await configured_parameters(settings)
        target = str(target_project_id or project_id)
        import asyncio
        result=await asyncio.to_thread(source_scopes.propose,
            current_project_id=project_id, run_id=run_id,
            target_project_id=target, bindings=bindings, reason=reason,
            resume_blocked_run_id=resume_blocked_run_id,
            authorization_mode=authorization_mode,
            assessment_parameters=parameters,
        )
        from assistant_kernel.services.directory_assessment import record_configuration_use
        result['configuration_use_ref']=await record_configuration_use(settings,result.get('directory_assessments',[]))
        return result


if "activate_source_scope" in allowed:
    @server.tool(annotations=SCOPED_WRITE)
    async def activate_source_scope(
        proposal_uri: str, proposal_sha256: str,
    ) -> dict:
        """Activate an explicitly requested external workspace from the next Run."""
        require_scope_initialized()
        return await source_scopes.activate(
            current_project_id=project_id, run_id=run_id,
            worker_instance_id=worker_instance_id, claim_token=claim_token,
            proposal_uri=proposal_uri, expected_sha256=proposal_sha256,
        )


if "revoke_project_grant" in allowed:
    @server.tool(annotations=SCOPED_WRITE)
    async def revoke_project_grant(grant_id: str) -> dict:
        """Revoke one current-Project grant named by a user-triggered Run."""
        require_scope_initialized()
        return await repository.revoke_project_grant_from_claim(
            source_run_id=run_id, worker_instance_id=worker_instance_id,
            claim_token=claim_token, grant_id=grant_id,
        )


if "write_candidate_file" in allowed:
    @server.tool(annotations=SCOPED_WRITE)
    async def write_candidate_file(
        target_uri: str, content: str, expected_sha256: str | None = None,
        expected_state: str | None = None,
    ) -> dict:
        """Write a candidate copy for an authorized formal target; never publish it."""
        require_scope_initialized()
        if core_mode:
            await check_lease()
        result = workspace_scope.write_candidate(
            authorized_roots, target_uri, content, expected_sha256, expected_state
        )
        candidate_relative = str(result["candidate_uri"])[len("workspace://candidate/"):]
        return {
            **result,
            "candidate_vault_uri": (
                f"vault://{project_id}/runs/{scope_owner}/candidate/{candidate_relative}"
            ),
            "manifest_vault_uri": f"vault://{project_id}/runs/{scope_owner}/candidate/manifest.json",
        }


if "delete_candidate_file" in allowed:
    @server.tool(annotations=SCOPED_WRITE)
    async def delete_candidate_file(target_uri: str, expected_sha256: str) -> dict:
        """Stage a hash-guarded deletion in the current Run candidate manifest."""
        require_scope_initialized()
        if core_mode:
            await check_lease()
        return workspace_scope.delete_candidate(
            authorized_roots, target_uri, expected_sha256
        )


if "get_stage_handoff" in allowed:
    @server.tool(annotations=READ_ONLY)
    async def get_stage_handoff() -> dict:
        """Return the immutable upstream input bundle for this leased Stage Run."""
        require_scope_initialized()
        current = await repository.get_run(run_id)
        if current is None:
            raise FileNotFoundError("current Run was not found")
        payload, uri, digest = handoffs.materialize(current)
        return {**payload, "handoff_uri": uri, "handoff_sha256": digest}


if "list_stage_artifacts" in allowed:
    @server.tool(annotations=READ_ONLY)
    async def list_stage_artifacts() -> dict:
        """List the exact artifacts bound to this Stage, including stable IDs and hashes."""
        require_scope_initialized()
        current = await repository.get_run(run_id)
        if current is None:
            raise FileNotFoundError("current Run was not found")
        return {
            "artifacts": handoffs.normalize_inputs(
                project_id, dict(current.get("request_json") or {})
            )
        }


if "read_stage_artifact" in allowed:
    @server.tool(annotations=READ_ONLY)
    async def read_stage_artifact(artifact_id: str, max_bytes: int = 262144) -> dict:
        """Read one hash-verified artifact already bound to this Stage Run."""
        require_scope_initialized()
        current = await repository.get_run(run_id)
        if current is None:
            raise FileNotFoundError("current Run was not found")
        return handoffs.read_bound_artifact(current, artifact_id, max_bytes)


if "vault_list" in allowed:
    @server.tool(annotations=READ_ONLY)
    def vault_list(relative_path: str = "", limit: int = 200) -> dict:
        """List files inside the current Project Vault."""
        require_scope_initialized()
        root = safe_file(relative_path)
        if not root.exists():
            return {"files": []}
        if not root.is_dir():
            raise ValueError("relative_path must identify a directory")
        base = runtime.ensure(project_id).vault_root
        files = []
        for item in sorted(root.rglob("*")):
            if item.is_file():
                files.append(str(item.relative_to(base)).replace("\\", "/"))
                if len(files) >= min(max(limit, 1), 1000):
                    break
        return {"project_id": project_id, "files": files}


if "vault_read_text" in allowed:
    @server.tool(annotations=READ_ONLY)
    def vault_read_text(relative_path: str, max_bytes: int = 262144) -> dict:
        """Read one UTF-8 file inside the current Project Vault."""
        require_scope_initialized()
        target = safe_file(relative_path)
        if not target.is_file():
            raise FileNotFoundError(relative_path)
        limit = min(max(max_bytes, 1), 2 * 1024 * 1024)
        data = target.read_bytes()
        if len(data) > limit:
            raise ValueError(f"file exceeds max_bytes={limit}")
        return {
            "uri": f"vault://{project_id}/{relative_path}",
            "text": data.decode("utf-8"),
        }


if "run_status" in allowed:
    @server.tool(annotations=READ_ONLY)
    async def run_status(target_run_id: str | None = None) -> dict:
        """Read one durable Run in the current Project and its execution blocker."""
        require_scope_initialized()
        selected = target_run_id or run_id
        row = await repository.get_run(selected)
        if row is None:
            return {"run_id": selected, "status": "not_found"}
        if str(row["project_id"]) != project_id:
            raise PermissionError("Run belongs to another Project")
        result = dict(row)
        blocker = await repository.active_execution_blocker(project_id, selected)
        if (
            str(row.get("status") or "") == "queued"
            and blocker is not None
            and str(blocker["run_id"]) != str(row["run_id"])
        ):
            result["blocked_by"] = {
                "run_id": str(blocker["run_id"]),
                "status": str(blocker["status"]),
                "stage": str(blocker.get("stage") or blocker.get("run_type") or "execution"),
            }
        return result


if "list_recent_history" in allowed:
    @server.tool(annotations=READ_ONLY)
    async def list_recent_history(
        lookback_days: int = 365,
        limit: int = 50,
        cursor: str | None = None,
        scope: Literal["current_conversation", "project"] = "current_conversation",
    ) -> dict:
        """List recent messages in this fixed Project; no semantic or cross-Project search."""
        require_scope_initialized()
        if core_mode:
            from assistant_kernel.core.db import Database
            async with Database(settings).tx() as c:
                rows = await (await c.execute(
                    """SELECT message_id, created_at, direction, role, body->>'text' AS text FROM core.messages
                       WHERE project_id=%s AND body ? 'text' AND created_at > now() - make_interval(days => %s)
                       ORDER BY created_at DESC LIMIT %s""",
                    (project_id, min(max(lookback_days, 1), 365), min(max(limit, 1), 200)))).fetchall()
            return {"items": [{**r, "message_id": str(r["message_id"]), "created_at": r["created_at"].isoformat()}
                              for r in reversed(rows)], "next_cursor": None}
        channel = None
        chat_id = None
        if scope == "current_conversation":
            current = await repository.get_run(run_id)
            if current is None or not current.get("trigger_message_id"):
                raise FileNotFoundError("current Run trigger was not found")
            trigger = await repository.get_message(str(current["trigger_message_id"]))
            if trigger is None:
                raise FileNotFoundError("current Run trigger message was not found")
            channel = str(trigger["channel"])
            chat_id = str(trigger["chat_id"])
        history = await repository.list_recent_history(
            project_id=project_id,
            lookback_days=lookback_days,
            limit=limit,
            cursor=cursor,
            channel=channel,
            chat_id=chat_id,
        )
        from assistant_kernel.services.public_background import PublicBackgroundService
        revoked = PublicBackgroundService(settings.data_root,settings).revoked_source_message_ids()
        history["items"] = [item for item in history.get("items") or []
                            if str(item.get("message_id") or "") not in revoked]
        history["withdrawn_sources_filtered"] = True
        # Preserve the repository's pagination boundary, including an empty filtered page.
        return history


if "list_projects" in allowed:
    @server.tool(annotations=READ_ONLY)
    def list_projects() -> dict:
        require_scope_initialized()
        return {"projects": [{**item, "agent_key": "project:"+item["project_id"]+":manager"} for item in projects.list()]}


if "list_project_agents" in allowed:
    @server.tool(annotations=READ_ONLY)
    def list_project_agents(feishu_only: bool = False) -> dict:
        """List published Agents discovered from the current Project registry."""
        require_scope_initialized()
        if project_id == "system":
            return {"project_id": project_id, "agents": []}
       
        agents = agent_files.list_agents(project_id)
        return {
            "project_id": project_id,
            "agents": [
                {
                    "agent_id": item.agent_id,
                    "agent_key": item.agent_key,
                    "kind": item.kind,
                    "status": item.status,
                    "display_name": item.display_name,
                    "aliases": list(item.aliases),
                    "entrypoints": item.entrypoints,
                }
                for item in agents
                if item.status == "active"
                and (not feishu_only or item.entrypoints.get("feishu_slash", False))
            ],
        }


if "current_conversation_route" in allowed:
    @server.tool(annotations=READ_ONLY)
    async def current_conversation_route() -> dict:
        """Return the durable route for the channel participant that triggered this Run."""
        require_scope_initialized()
        current = await repository.get_run(run_id)
        if current is None or not current.get("trigger_message_id"):
            raise FileNotFoundError("current Run trigger was not found")
        trigger = await repository.get_message(str(current["trigger_message_id"]))
        if trigger is None:
            raise FileNotFoundError("current Run trigger message was not found")
        route = await repository.resolve_route_binding(
            str(trigger["channel"]), str(trigger["chat_id"]), trigger.get("user_id")
        )
        python_context=None
        if 'submit_project_python' in allowed:
            await repository.claimed_run_source(run_id=run_id,worker_instance_id=worker_instance_id,claim_token=claim_token)
            async with repository.database.connection() as connection:
                active=await (await connection.execute("SELECT invocation_id FROM assistant.model_invocations WHERE run_id=%s AND status='running'",(run_id,))).fetchall()
            state=current['request_json'];confirmation=state.get('prompt_confirmation_message_id')
            if len(active)==1 and confirmation and state.get('confirmed_turn_package_ref'):
                from assistant_kernel.services.document_runtime import resource_ref
                invocation=active[0]['invocation_id']
                python_context={'creating_task_ref':resource_ref(kind='receipt',identity=run_id,uri='db://assistant/runs/'+run_id,purpose='current actual Task'),
                    'creating_invocation_ref':resource_ref(kind='receipt',identity=invocation,uri='db://assistant/model_invocations/'+invocation,purpose='current actual model invocation'),
                    'authorization_ref':resource_ref(kind='message',identity=confirmation,uri='db://assistant/messages/'+confirmation,purpose='actual current Prompt confirmation'),
                    'turn_boundary_ref':state['confirmed_turn_package_ref']}
        return {
            "project_id": route[0] if route else str(current["project_id"]),
            "agent_key": route[1] if route else str(current["agent_key"]),
            "channel": str(trigger["channel"]),
            "chat_id": str(trigger["chat_id"]),
            "python_execution_context": python_context,
        }


if "handoff_to_agent" in allowed:
    @server.tool(annotations=SCOPED_WRITE)
    async def handoff_to_agent(target_agent: str, handoff_summary: str) -> dict:
        """Switch this Project conversation to any published PM-handoff-enabled Agent."""
        require_scope_initialized()
        if project_id == "system" or stage is not None:
            raise PermissionError("only a Project PM or business Agent can hand off to an Agent")
        target = agent_files.resolve_agent(project_id, target_agent)
        if not target.entrypoints.get("pm_handoff", False):
            raise PermissionError("target Agent does not allow PM handoff")
        current = await repository.get_run(run_id)
        if current is None:
            raise FileNotFoundError("current Run was not found")
        if target.agent_key == current["agent_key"]:
            raise ValueError("target Agent is already handling this Run")
        result = await repository.handoff_from_claim(
            source_run_id=run_id,
            worker_instance_id=worker_instance_id,
            claim_token=claim_token,
            target_project_id=project_id,
            target_agent_key=target.agent_key,
            handoff_summary=handoff_summary,
        )
        return {
            "project_id": project_id,
            "agent_id": target.agent_id,
            "agent_key": target.agent_key,
            "message_id": str(result["message"]["message_id"]),
            "run_id": str(result["run"]["run_id"]),
            "status": str(result["run"]["status"]),
        }






if "handoff_to_project" in allowed:
    @server.tool(annotations=SCOPED_WRITE)
    async def handoff_to_project(target_project_id: str, handoff_summary: str, target_agent_id: str | None = None) -> dict:
        require_scope_initialized()
        if target_agent_id not in {None, "pm"}:
            raise ValueError("Only an existing Project PM is a handoff destination")
        from assistant_kernel.services.kernel_management import require_active_identity
        require_active_identity(target_project_id, "project:"+target_project_id+":manager")
        result = await projects.handoff_to_existing_project_from_claim(
            run_id=run_id, worker_instance_id=worker_instance_id, claim_token=claim_token,
            target_project_id=target_project_id, handoff_summary=handoff_summary)
        return {"project_id":target_project_id,"run_id":str(result["run"]["run_id"]),
                "status":str(result["run"]["status"]),"message_id":str(result["message"]["message_id"])}


if "start_project_task" in allowed:
    @server.tool(annotations=SCOPED_WRITE)
    async def start_project_task(
        goal: str,
        change_class: str = "major",
        stages: list[str] | None = None,
        target_skill: str | None = None,
        requested_skills: list[str] | None = None,
        supersede_run_id: str | None = None,
        supersede_reason: str | None = None,
    ) -> dict:
        raise PermissionError("Stage execution is retired; use single-agent.v2")


if __name__ == "__main__":
    server.run(transport="stdio")

# Fixed historical packages receive a retirement error, never an executable creation path.
if "propose_project" in allowed:
    @server.tool(annotations=SCOPED_WRITE)
    async def propose_project(project_id: str, purpose: str, initial_goal: str = "", boundaries: list[str] | None = None,
                              non_goals: list[str] | None = None, workspace_roots: list[dict] | None = None) -> dict:
        raise PermissionError("Project creation is retired; no proposal was written")

if "create_project" in allowed:
    @server.tool(annotations=SCOPED_WRITE)
    async def create_project(proposal_uri: str, proposal_sha256: str, handoff_summary: str = "") -> dict:
        raise PermissionError("Project creation is retired; no Project was created")
