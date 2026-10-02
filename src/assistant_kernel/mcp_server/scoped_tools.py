"""Per-invocation stdio tools constrained to one leased Run and Project."""
from __future__ import annotations

import os
import hashlib
import json
from typing import Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from assistant_kernel.config import KernelSettings
from assistant_kernel.mcp_server.error_protocol import install_error_protocol
from assistant_kernel.repositories import CoreRepository
from assistant_kernel.services.project_directory import ProjectDirectoryService
from assistant_kernel.services.project_credentials import ProjectCredentialsStore
from assistant_kernel.services.source_scope import SourceScopeService
from assistant_kernel.services.capability_bundle import CapabilityBundleService
from assistant_kernel.services.stage_handoff import StageHandoffService
from assistant_kernel.services.agent_files import AgentFileLoader
from assistant_kernel.services.workspace_scope import WorkspaceScopeService
from assistant_kernel.services.semantic_map import SemanticMapService
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
capabilities = CapabilityBundleService(settings)
handoffs = StageHandoffService(settings)
agent_files = AgentFileLoader(settings)
volcengine_models = VolcengineGateway(settings)
scope_snapshot_uri = os.environ.get("KERNEL_SCOPE_SNAPSHOT_URI", "").strip()
scope_snapshot_sha256 = os.environ.get("KERNEL_SCOPE_SNAPSHOT_SHA256", "").strip()
if bool(scope_snapshot_uri) != bool(scope_snapshot_sha256):
    raise ValueError("workspace scope snapshot URI and hash must be provided together")
authorized_roots = (
    workspace_scope.load_snapshot(
        project_id, run_id, stage, scope_snapshot_uri, scope_snapshot_sha256,
    )
    if scope_snapshot_uri else workspace_scope.resolve(project_id, run_id, stage)
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
                                and item.native_mode == "enforced"
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
    def get_project_map() -> dict:
        """Return the Project Semantic Map header and compact component catalog."""
        require_scope_initialized()
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
        }


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
            limit=limit,
        )


if "read_map_component" in allowed:
    @server.tool(annotations=READ_ONLY)
    def read_map_component(component_id: str) -> dict:
        """Read one registered component without scanning the source tree."""
        require_scope_initialized()
        for item in semantic_maps.load(project_id).get("components") or []:
            if str(item.get("component_id")) == str(component_id):
                return dict(item)
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
    def read_authorized_file(uri: str, max_bytes: int = 262144) -> dict:
        """Read one UTF-8 file addressed by an authorized workspace URI."""
        require_scope_initialized()
        _, target, _ = workspace_scope.resolve_uri(authorized_roots, uri)
        if not target.is_file():
            raise FileNotFoundError(uri)
        limit = min(max(max_bytes, 1), 2 * 1024 * 1024)
        data = target.read_bytes()
        if len(data) > limit:
            raise ValueError(f"file exceeds max_bytes={limit}")
        return {
            "uri": uri,
            "text": data.decode("utf-8"),
            "sha256": hashlib.sha256(data).hexdigest(),
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
    def propose_source_scope(
        bindings: list[dict], reason: str, target_project_id: str | None = None,
        resume_blocked_run_id: str | None = None,
        authorization_mode: str = "explicit_request",
    ) -> dict:
        """Persist a hash-fixed record for user-requested external workspaces."""
        require_scope_initialized()
        target = str(target_project_id or project_id)
        return source_scopes.propose(
            current_project_id=project_id, run_id=run_id,
            target_project_id=target, bindings=bindings, reason=reason,
            resume_blocked_run_id=resume_blocked_run_id,
            authorization_mode=authorization_mode,
        )


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
    def write_candidate_file(
        target_uri: str, content: str, expected_sha256: str | None = None,
        expected_state: str | None = None,
    ) -> dict:
        """Write a candidate copy for an authorized formal target; never publish it."""
        require_scope_initialized()
        result = workspace_scope.write_candidate(
            authorized_roots, target_uri, content, expected_sha256, expected_state
        )
        candidate_relative = str(result["candidate_uri"])[len("workspace://candidate/"):]
        return {
            **result,
            "candidate_vault_uri": (
                f"vault://{project_id}/runs/{run_id}/candidate/{candidate_relative}"
            ),
            "manifest_vault_uri": f"vault://{project_id}/runs/{run_id}/candidate/manifest.json",
        }


if "delete_candidate_file" in allowed:
    @server.tool(annotations=SCOPED_WRITE)
    def delete_candidate_file(target_uri: str, expected_sha256: str) -> dict:
        """Stage a hash-guarded deletion in the current Run candidate manifest."""
        require_scope_initialized()
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
        blocker = await repository.active_execution_blocker(project_id)
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
        return await repository.list_recent_history(
            project_id=project_id,
            lookback_days=lookback_days,
            limit=limit,
            cursor=cursor,
            channel=channel,
            chat_id=chat_id,
        )


if "list_projects" in allowed:
    @server.tool(annotations=READ_ONLY)
    def list_projects() -> dict:
        """List switchable PM targets without reading their private content."""
        require_scope_initialized()
        if stage is not None:
            raise PermissionError("Stage agents cannot list cross-Project PM targets")
        active = [
            {
                **item,
                "agent_key": f"project:{item['project_id']}:manager",
            }
            for item in projects.list()
        ]
        return {"projects": [{
            "schema_version": "project-card.v1",
            "project_id": "system",
            "display_name": "个人超级助理大管家",
            "purpose": "返回个人超级助理的大管家总入口",
            "status": "active",
            "default_agent": "system-butler",
            "initial_goal": "",
            "boundaries": ["普通交流、一次性问答、Project定位和受控移交"],
            "non_goals": ["代替目标Project执行内部实施"],
            "routing": {
                "aliases": ["大管家", "butler", "总入口"],
                "topics": ["普通问答", "项目定位", "新项目提案"],
                "typical_requests": ["返回大管家", "帮我找到负责这个话题的项目"],
                "exclusions": ["已经明确属于某个现有Project的内部工作"],
                "related_projects": [],
            },
            "agent_key": "system-butler",
        }, *active]}


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
        return {
            "project_id": route[0] if route else str(current["project_id"]),
            "agent_key": route[1] if route else str(current["agent_key"]),
            "channel": str(trigger["channel"]),
            "chat_id": str(trigger["chat_id"]),
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


if "propose_project" in allowed:
    @server.tool(annotations=SCOPED_WRITE)
    def propose_project(
        new_project_id: str, purpose: str, initial_goal: str = "",
        boundaries: list[str] | None = None, non_goals: list[str] | None = None,
        workspace_roots: list[dict] | None = None,
    ) -> dict:
        """Persist a Project proposal and any user-selected recursive parent roots."""
        require_scope_initialized()
        if project_id != "system":
            raise PermissionError("only the system Butler can propose Projects")
        return projects.propose(
            run_id, new_project_id, purpose, initial_goal=initial_goal,
            boundaries=boundaries, non_goals=non_goals,
            workspace_roots=workspace_roots,
        )


if "create_project" in allowed:
    @server.tool(annotations=SCOPED_WRITE)
    async def create_project(
        proposal_uri: str, proposal_sha256: str, handoff_summary: str = ""
    ) -> dict:
        """Create an approved Project and idempotently hand this Run to its PM."""
        require_scope_initialized()
        if project_id != "system":
            raise PermissionError("only the system Butler can create Projects")
        return await projects.create_and_handoff_from_approved_run(
            run_id=run_id, worker_instance_id=worker_instance_id,
            claim_token=claim_token, proposal_uri=proposal_uri,
            expected_sha256=proposal_sha256, handoff_summary=handoff_summary,
        )


if "handoff_to_project" in allowed:
    @server.tool(annotations=SCOPED_WRITE)
    async def handoff_to_project(target_project_id: str, handoff_summary: str) -> dict:
        """Open an active Project in its Discord channel and hand off to its PM."""
        require_scope_initialized()
        if stage is not None:
            raise PermissionError("Stage agents cannot hand off across Projects")
        if target_project_id == project_id:
            raise ValueError("target Project is already the current PM")
        active_targets = {
            str(item["project_id"]) for item in projects.list()
        } | {"system"}
        if target_project_id not in active_targets:
            raise ValueError("target Project is not active in the Project directory")
        target = runtime.describe(target_project_id).vault_root
        if not target.is_dir() or not agent_files.list_agents(target_project_id):
            raise ValueError("target Project is not registered by a valid Vault directory")
        if target_project_id == "system":
            result = await repository.handoff_from_claim(
                source_run_id=run_id, worker_instance_id=worker_instance_id,
                claim_token=claim_token, target_project_id=target_project_id,
                handoff_summary=handoff_summary,
            )
        else:
            result = await projects.handoff_to_existing_project_from_claim(
                run_id=run_id, worker_instance_id=worker_instance_id,
                claim_token=claim_token, target_project_id=target_project_id,
                handoff_summary=handoff_summary,
            )
        response = {
            "project_id": target_project_id,
            "message_id": str(result["message"]["message_id"]),
            "run_id": str(result["run"]["run_id"]),
            "status": str(result["run"]["status"]),
        }
        provisioning = result.get("discord_provisioning")
        if isinstance(provisioning, dict):
            channel_id = str(provisioning.get("channel_id") or "")
            response.update({
                "discord_channel_id": channel_id or None,
                "discord_channel_mention": f"<#{channel_id}>" if channel_id else None,
                "discord_channel_created": bool(provisioning.get("created")),
            })
        return response


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
        """Start a deterministic, approval-gated stage chain in the current Project."""
        if project_id == "system":
            raise PermissionError("the Butler must hand off to a Project before starting a task")
        require_scope_initialized()
        if change_class not in {"minor", "major"}:
            raise ValueError("change_class must be minor or major")
        stages = stages or (
            ["requirements", "implementation", "review"]
            if change_class == "major" else ["implementation", "review"]
        )
        for stage in stages:
            try:
                agent_files.load(project_id, f"project:{project_id}:{stage}")
            except FileNotFoundError as exc:
                raise ValueError(f"Project stage is not configured: {stage}") from exc
        task_metadata: dict = {
            "task_kind": "project_task",
            "requested_skills": requested_skills or [],
        }
        baseline = None
        if target_skill:
            baseline = capabilities.inspect(project_id, target_skill)
            task_metadata = {
                "task_kind": "skill_change",
                "requested_skills": requested_skills or [],
                "capability_change": {
                    "target_skill": baseline["name"],
                    "change_mode": baseline["change_mode"],
                    "baseline": baseline,
                },
            }
        stage_bindings = {}
        for selected_stage in stages:
            config = agent_files.load(
                project_id, f"project:{project_id}:{selected_stage}",
                task_kind=task_metadata["task_kind"],
                requested_skills=requested_skills or [],
            )
            snapshot_uri, snapshot_hash = agent_files.snapshot(project_id, config)
            stage_bindings[selected_stage] = {
                "config_snapshot_uri": snapshot_uri,
                "config_bundle_hash": snapshot_hash,
                "source_files": list(config.source_files),
                "skill_bindings": list(config.skill_bindings),
            }
        task_metadata["stage_bindings"] = stage_bindings
        row = await repository.start_task_from_claim(
            source_run_id=run_id, worker_instance_id=worker_instance_id,
            claim_token=claim_token, goal=goal, stages=stages,
            change_class=change_class,
            task_metadata=task_metadata,
            supersede_run_id=supersede_run_id,
            supersede_reason=supersede_reason,
        )
        return {
            "task_root_run_id": str(row["run_id"]),
            "stage": str(row["stage"]), "status": str(row["status"]),
            "task_kind": task_metadata["task_kind"],
            "capability_change": task_metadata.get("capability_change"),
            "supersedes_run_id": supersede_run_id,
        }


if __name__ == "__main__":
    server.run(transport="stdio")
