from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from assistant_kernel.config import KernelSettings
from assistant_kernel.services.agent_files import FileAgentConfig
from assistant_kernel.services.permission_policy import PermissionPolicyCompiler
from assistant_kernel.services.semantic_map import SemanticMapService
from assistant_kernel.services.workspace_scope import WorkspaceScopeService
from assistant_kernel.storage import ProjectRuntimeManager


SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SAFE_ENV = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")


class ProjectResourceResolver:
    """Resolve one published Agent configuration into an immutable Run plan."""

    def __init__(self, settings: KernelSettings):
        self.settings = settings
        self.runtime = ProjectRuntimeManager(settings.data_root)
        self.workspace_scopes = WorkspaceScopeService(settings)

    @staticmethod
    def _canonical_bytes(value: Any) -> bytes:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
        ).encode("utf-8")

    @classmethod
    def _sha256(cls, value: Any) -> str:
        payload = value if isinstance(value, bytes) else cls._canonical_bytes(value)
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _bundle_entries(root: Path) -> list[dict[str, Any]]:
        entries = []
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(root)
            if any(part in {"__pycache__", ".pytest_cache", ".git"} for part in relative.parts):
                continue
            payload = path.read_bytes()
            entries.append({
                "path": relative.as_posix(),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size": len(payload),
            })
        return entries

    def compatibility_hash(
        self,
        config: FileAgentConfig,
        *,
        provider: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        context_policy: str | None = None,
        model_context_window_tokens: int | None = None,
        model_auto_compact_token_limit: int | None = None,
        scope_compatibility_hash: str | None = None,
        sandbox: str | None = None,
    ) -> str:
        core_skill = config.skill_bindings[0] if config.skill_bindings else None
        return self._sha256({
            "agent_key": config.agent_key,
            "role_text": config.role_text,
            "core_skill": (
                {"name": core_skill["name"], "bundle_hash": core_skill["bundle_hash"]}
                if core_skill else None
            ),
            "scope_profile": config.scope_profile,
            "scope_compatibility_hash": scope_compatibility_hash,
            "network_access": True,
            "sandbox": sandbox or config.sandbox,
            "output_schema": config.output_schema,
            "provider": provider,
            "model": model,
            "reasoning_effort": reasoning_effort,
            "context_policy": context_policy,
            "model_context_window_tokens": model_context_window_tokens,
            "model_auto_compact_token_limit": model_auto_compact_token_limit,
        })

    def _snapshot_mcp_bundle(
        self, project_id: str, server_id: str, source_root: Path, bundle_hash: str,
    ) -> tuple[Path, str]:
        runtime = self.runtime.ensure(project_id)
        snapshot_root = runtime.vault_root / "runtime" / "mcp-snapshots" / bundle_hash / server_id
        if not snapshot_root.exists():
            temporary = snapshot_root.with_name(snapshot_root.name + ".tmp")
            if temporary.exists():
                shutil.rmtree(temporary)
            temporary.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source_root, temporary)
            temporary.replace(snapshot_root)
        return snapshot_root, (
            f"vault://{project_id}/runtime/mcp-snapshots/{bundle_hash}/{server_id}/"
        )

    def resolve_mcp_servers(
        self, project_id: str, config: FileAgentConfig,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        runtime = self.runtime.ensure(project_id)
        plan_servers: list[dict[str, Any]] = []
        harness_servers: list[dict[str, Any]] = []
        exposed_tools: dict[str, str] = {}
        for binding in config.mcp_servers:
            server_id = str(binding.get("server_id") or "")
            if not SAFE_ID.fullmatch(server_id) or server_id == "kernel_scoped":
                raise ValueError(f"invalid or reserved MCP server_id: {server_id}")
            source_root = runtime.vault_root / "mcp" / server_id
            manifest_path = source_root / "mcp-manifest.yaml"
            if not manifest_path.is_file():
                raise FileNotFoundError(f"published MCP manifest is missing: {server_id}")
            manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
            if not isinstance(manifest, dict):
                raise ValueError(f"MCP manifest must be an object: {server_id}")
            if str(manifest.get("server_id") or "") != server_id:
                raise ValueError(f"MCP manifest server_id mismatch: {server_id}")
            transport = str(manifest.get("transport") or "")
            if transport not in {"stdio", "streamable-http"}:
                raise ValueError(f"unsupported MCP transport: {transport}")
            declared_tools = []
            tool_effects: dict[str, str] = {}
            for item in manifest.get("tools") or []:
                if not isinstance(item, dict) or not str(item.get("name") or ""):
                    raise ValueError(f"invalid MCP Tool declaration: {server_id}")
                name = str(item["name"])
                declared_tools.append(name)
                tool_effects[name] = str(item.get("effect") or "read")
            enabled_tools = [str(item) for item in binding.get("enabled_tools") or []]
            if not enabled_tools:
                raise ValueError(f"MCP binding must explicitly enable Tools: {server_id}")
            unknown = sorted(set(enabled_tools) - set(declared_tools))
            if unknown:
                raise ValueError(f"MCP enabled_tools are not declared by {server_id}: {', '.join(unknown)}")
            for name in enabled_tools:
                if tool_effects[name] in {"external", "trade"}:
                    raise PermissionError(
                        f"Project MCP action grant is not implemented for {server_id}/{name}; "
                        "a file Scope cannot authorize external or trading effects"
                    )
            for name in enabled_tools:
                owner = exposed_tools.setdefault(name, server_id)
                if owner != server_id:
                    raise ValueError(f"MCP Tool name is exposed by multiple Servers: {name}")

            entries = self._bundle_entries(source_root)
            bundle_hash = self._sha256(entries)
            snapshot_root, snapshot_uri = self._snapshot_mcp_bundle(
                project_id, server_id, source_root, bundle_hash,
            )
            required = bool(binding.get("required", manifest.get("required", True)))
            startup_timeout = int(binding.get(
                "startup_timeout_sec", manifest.get("startup_timeout_sec", 10)
            ))
            tool_timeout = int(binding.get(
                "tool_timeout_sec", manifest.get("tool_timeout_sec", 60)
            ))
            common = {
                "server_id": server_id,
                "transport": transport,
                "lifecycle": str(manifest.get("lifecycle") or (
                    "per_run" if transport == "stdio" else "shared"
                )),
                "enabled_tools": enabled_tools,
                "required": required,
                "startup_timeout_sec": startup_timeout,
                "tool_timeout_sec": tool_timeout,
                "manifest_sha256": self._sha256(manifest_path.read_bytes()),
                "bundle_hash": bundle_hash,
                "snapshot_uri": snapshot_uri,
                "files": entries,
            }
            if transport == "stdio":
                entrypoint = str(manifest.get("entrypoint") or "server.py")
                entrypoint_path = self.runtime.resolve_below(snapshot_root, entrypoint)
                if not entrypoint_path.is_file() or entrypoint_path.suffix != ".py":
                    raise ValueError(f"STDIO MCP entrypoint must be a Python file: {server_id}")
                plan_servers.append({**common, "entrypoint": entrypoint})
                harness_servers.append({
                    **common,
                    "command": sys.executable,
                    "args": ["-m", "assistant_kernel.mcp_server.project_launcher"],
                    "cwd": str(snapshot_root),
                    "env": {
                        "KERNEL_PROJECT_MCP_ROOT": str(snapshot_root),
                        "KERNEL_PROJECT_MCP_ENTRYPOINT": entrypoint,
                        "KERNEL_PROJECT_MCP_SERVER_ID": server_id,
                    },
                })
            else:
                endpoint_ref = str(manifest.get("endpoint_ref") or "")
                if not endpoint_ref.startswith("env://"):
                    raise ValueError(
                        f"HTTP MCP endpoint_ref must use env:// in Runtime v2: {server_id}"
                    )
                endpoint_env = endpoint_ref[len("env://"):]
                if not SAFE_ENV.fullmatch(endpoint_env):
                    raise ValueError(f"invalid HTTP MCP endpoint environment name: {server_id}")
                url = os.environ.get(endpoint_env, "").strip()
                if not url.startswith(("http://", "https://")):
                    raise RuntimeError(f"HTTP MCP endpoint is not configured: {endpoint_env}")
                parsed_url = urlsplit(url)
                if parsed_url.username or parsed_url.password or parsed_url.query or parsed_url.fragment:
                    raise ValueError(f"HTTP MCP endpoint must not contain credentials or query data: {server_id}")
                bearer_env = str(manifest.get("bearer_token_env_var") or "") or None
                if bearer_env and not SAFE_ENV.fullmatch(bearer_env):
                    raise ValueError(f"invalid HTTP MCP bearer environment name: {server_id}")
                plan_servers.append({
                    **common,
                    "endpoint_ref": endpoint_ref,
                    "endpoint_sha256": self._sha256(url.encode("utf-8")),
                    "bearer_token_env_var": bearer_env,
                })
                harness_servers.append({
                    **common,
                    "url": url,
                    "bearer_token_env_var": bearer_env,
                })
        return plan_servers, harness_servers

    @staticmethod
    def mcp_prompt_summary(plan_servers: list[dict[str, Any]]) -> str:
        lines = []
        for item in plan_servers:
            if item["transport"] == "stdio":
                detail = "Project-local STDIO MCP; per-Run process; no network port."
            else:
                detail = (
                    "Shared Streamable HTTP MCP; network/service dependent; "
                    "reconcile uncertain external results before retrying."
                )
            lines.append(
                f"- {item['server_id']}: {detail} Enabled tools: "
                + ", ".join(item["enabled_tools"])
            )
        return "\n".join(lines)

    def materialize(
        self, *, run: dict[str, Any], config: FileAgentConfig,
        config_snapshot_uri: str, config_snapshot_sha256: str,
        provider: str | None, model: str | None, reasoning_effort: str,
        timeout_seconds: int, session: dict[str, Any],
        context_policy: str = "model_default",
        model_context_window_tokens: int | None = None,
        model_auto_compact_token_limit: int | None = None,
        effective_context_window_tokens: int | None = None,
        catalog_context_window_tokens: int | None = None,
        catalog_max_context_window_tokens: int | None = None,
        effective_context_window_percent: int | None = None,
        scope_compatibility_hash: str | None = None,
        sandbox: str | None = None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]], str, str]:
        project_id = str(run["project_id"])
        run_id = str(run["run_id"])
        runtime = self.runtime.ensure(project_id)
        request = dict(run.get("request_json") or {})
        variant = str(request.get("execution_plan_variant") or "").strip() or None
        suffix = f"-{variant}" if variant else ""
        existing_path = (
            runtime.vault_root / "runs" / run_id / "resolved" /
            f"execution-plan{suffix}.json"
        )
        existing_uri = (
            f"vault://{project_id}/runs/{run_id}/resolved/"
            f"execution-plan{suffix}.json"
        )
        if existing_path.is_file():
            existing_file_sha = hashlib.sha256(existing_path.read_bytes()).hexdigest()
            existing_plan, existing_harness = self.load_materialized(
                project_id, existing_uri, existing_file_sha,
            )
            bindings = existing_plan.get("bindings") or {}
            if (
                bindings.get("config_snapshot_uri") != config_snapshot_uri
                or bindings.get("config_snapshot_sha256") != config_snapshot_sha256
            ):
                raise RuntimeError("existing ResolvedExecutionPlan conflicts with the pinned Agent")
            return existing_plan, existing_harness, existing_uri, existing_file_sha
        plan_servers, harness_servers = self.resolve_mcp_servers(project_id, config)
        stage = str(run.get("stage") or "") or None
        candidate_only = bool(
            request.get("candidate_only")
            or request.get("interaction_kind") == "child_task"
        )
        audit_mode = bool(request.get("audit_mode"))
        candidate_enabled = bool(
            (
            stage == "implementation"
            or request.get("interaction_kind") == "child_task"
            or (
                request.get("task_protocol") == "single-agent.v2"
                and request.get("permission_profile_name") == "candidate-write"
            )
            )
        )
        formal_read_only = bool(
            candidate_only or config.kind == "manager"
            or stage in {"requirements", "review"}
        )
        scope_snapshot_uri, scope_snapshot_sha256 = self.workspace_scopes.snapshot(
            project_id, run_id, stage, candidate_only=formal_read_only,
            candidate_enabled=candidate_enabled, variant=variant,
        )
        snapshot_roots = self.workspace_scopes.load_snapshot(
            project_id, run_id, stage, scope_snapshot_uri, scope_snapshot_sha256,
        )
        context_view = self.workspace_scopes.context_view(
            project_id, snapshot_roots, request=request,
        )
        semantic_context = SemanticMapService(self.settings).materialize_context(
            project_id, run_id,
            task_kind=str(
                request.get("task_kind") or variant or stage
                or request.get("current_stage") or config.kind or "chat"
            ),
            query_terms=[
                str(request.get("goal") or request.get("task_goal") or ""),
                str(request.get("summary") or ""),
            ],
            component_ids=[
                str(item) for item in request.get("component_ids") or []
            ],
            variant=variant,
            accepted_documents=dict(request.get("accepted_documents") or {}),
        )
        active_file_grant_ids = sorted(
            str(item.grant_id) for item in snapshot_roots if item.grant_id
        )
        network_enabled = True
        native_profile, native_profile_sha256, native_may_write = (
            PermissionPolicyCompiler(
                PermissionPolicyCompiler.current_runtime_root(self.settings.workspace_root)
            ).materialize(
                runtime.codex_home,
                f"{run_id}_{variant}" if variant else run_id,
                snapshot_roots,
                network_enabled=network_enabled,
            )
        )
        has_writable_workspace = any(
            item.access == "write" and item.write_mode == "direct"
            and item.native_mode == "enforced"
            and item.key not in {"candidate", "run_work"} for item in snapshot_roots
        )
        effective_sandbox = (
            "workspace-write" if native_may_write else "read-only"
        ) if native_profile else (sandbox or config.sandbox)
        effective_workspace_write = (
            has_writable_workspace and effective_sandbox == "workspace-write"
        )
        stage_permissions = {
            "project_workspace_grant": (
                "read_write_execute" if has_writable_workspace else "project_only"
            ),
            "workspace_file_access": (
                "read_write_execute" if effective_workspace_write else "read_execute"
            ),
            "formal_write": False,
            "direct_workspace_write": effective_workspace_write,
            "run_work_write": project_id != "system",
            "network": network_enabled,
            "account_connect": True,
            "trade": True,
        }
        plan = {
            "schema_version": "execution-plan.v1",
            "run_id": run_id,
            "project_id": project_id,
            "agent_key": config.agent_key,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "target": {
                "kind": config.kind,
                "id": config.agent_id,
            },
            "trigger": {
                "kind": str((request.get("trigger") or {}).get("kind") or "message"),
                "source_message_id": str(run.get("trigger_message_id") or "") or None,
                "idempotency_key": run.get("idempotency_key"),
            },
            "bindings": {
                "config_snapshot_uri": config_snapshot_uri,
                "config_snapshot_sha256": config_snapshot_sha256,
                "config_source_files": list(config.source_files),
                "skill_bindings": list(config.skill_bindings),
                "scope_profile": config.scope_profile,
                "scope_compatibility_hash": scope_compatibility_hash,
                "workspace_scope_snapshot_uri": scope_snapshot_uri,
                "workspace_scope_snapshot_sha256": scope_snapshot_sha256,
                "context_view": context_view,
                "semantic_context": semantic_context,
                "work_mode_bundle": dict(request.get("work_mode_bundle") or {}),
                "work_mode_bundle_sha256": request.get("work_mode_bundle_sha256"),
                "context_profile": request.get("context_profile"),
                "permission_profile_name": request.get("permission_profile_name"),
                "active_file_grant_ids": active_file_grant_ids,
                "native_permission_profile": native_profile,
                "native_permission_profile_sha256": native_profile_sha256,
                "stage_permissions": stage_permissions,
                "mcp_servers": plan_servers,
            },
            "runtime": {
                "harness": "codex_cli",
                "provider": provider,
                "model": model,
                "reasoning_effort": reasoning_effort,
                "context_policy": context_policy,
                "model_context_window_tokens": model_context_window_tokens,
                "model_auto_compact_token_limit": model_auto_compact_token_limit,
                "effective_context_window_tokens": effective_context_window_tokens,
                "catalog_context_window_tokens": catalog_context_window_tokens,
                "catalog_max_context_window_tokens": catalog_max_context_window_tokens,
                "effective_context_window_percent": effective_context_window_percent,
                "sandbox": effective_sandbox,
                "may_write_files": native_may_write,
                "may_write_shared": has_writable_workspace,
                "timeout_seconds": timeout_seconds,
                "max_attempts": int(run.get("max_attempts") or 3),
                "candidate_only": candidate_only,
                "plan_variant": variant,
                "multi_agent_enabled": bool(request.get("multi_agent_enabled", False)),
                "max_concurrent_subagents": min(max(int(
                    request.get("max_concurrent_subagents") or 3
                ), 1), 3),
            },
            "validation_policy": dict(request.get("validation_policy") or {
                "required_scopes": ["structure"]
            }),
            "session": {
                "logical_session_id": str(session["logical_session_id"]),
                "native_session_id": session.get("native_session_id"),
                "compatibility_hash": self.compatibility_hash(
                    config,
                    provider=provider,
                    model=model,
                    reasoning_effort=reasoning_effort,
                    context_policy=context_policy,
                    model_context_window_tokens=model_context_window_tokens,
                    model_auto_compact_token_limit=model_auto_compact_token_limit,
                    scope_compatibility_hash=scope_compatibility_hash,
                    sandbox=sandbox or config.sandbox,
                ),
            },
        }
        plan["plan_sha256"] = self._sha256(plan)
        resolved_root = runtime.vault_root / "runs" / run_id / "resolved"
        resolved_root.mkdir(parents=True, exist_ok=True)
        plan_path = resolved_root / f"execution-plan{suffix}.json"
        encoded = json.dumps(plan, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
        if plan_path.exists() and plan_path.read_bytes() != encoded:
            raise RuntimeError("ResolvedExecutionPlan is immutable for one Run")
        if not plan_path.exists():
            temporary = plan_path.with_suffix(".tmp")
            temporary.write_bytes(encoded)
            temporary.replace(plan_path)
        resources_path = resolved_root / f"bound-resources{suffix}.json"
        resources = {
            "schema_version": "bound-resources.v1",
            "config": {"uri": config_snapshot_uri, "sha256": config_snapshot_sha256},
            "skills": list(config.skill_bindings),
            "workspace_scope": {
                "uri": scope_snapshot_uri,
                "sha256": scope_snapshot_sha256,
            },
            "context_view": context_view,
            "semantic_context": semantic_context,
            "stage_permissions": stage_permissions,
            "mcp_servers": plan_servers,
        }
        resources_payload = json.dumps(
            resources, ensure_ascii=False, sort_keys=True, indent=2
        ).encode("utf-8")
        if resources_path.exists() and resources_path.read_bytes() != resources_payload:
            raise RuntimeError("bound resources are immutable for one Run")
        if not resources_path.exists():
            temporary = resources_path.with_suffix(".tmp")
            temporary.write_bytes(resources_payload)
            temporary.replace(resources_path)
        uri = (
            f"vault://{project_id}/runs/{run_id}/resolved/"
            f"execution-plan{suffix}.json"
        )
        return plan, harness_servers, uri, hashlib.sha256(encoded).hexdigest()

    def load_materialized(
        self, project_id: str, uri: str, expected_sha256: str,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        prefix = f"vault://{project_id}/"
        if not uri.startswith(prefix):
            raise ValueError("ResolvedExecutionPlan belongs to another Project")
        path = self.runtime.resolve_vault_file(project_id, uri[len(prefix):])
        payload = path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != expected_sha256:
            raise ValueError("ResolvedExecutionPlan file hash differs from the pinned Run")
        plan = json.loads(payload.decode("utf-8"))
        if plan.get("schema_version") != "execution-plan.v1":
            raise ValueError("unsupported ResolvedExecutionPlan schema")
        if str(plan.get("project_id")) != project_id:
            raise ValueError("ResolvedExecutionPlan Project mismatch")
        claimed_hash = str(plan.get("plan_sha256") or "")
        unhashed = dict(plan)
        unhashed.pop("plan_sha256", None)
        if claimed_hash != self._sha256(unhashed):
            raise ValueError("ResolvedExecutionPlan content hash is invalid")
        return plan, self.harness_bindings(project_id, plan)

    def harness_bindings(
        self, project_id: str, plan: dict[str, Any],
    ) -> list[dict[str, Any]]:
        bindings = []
        prefix = f"vault://{project_id}/"
        for item in (plan.get("bindings") or {}).get("mcp_servers") or []:
            server = dict(item)
            transport = str(server.get("transport") or "")
            snapshot_uri = str(server.get("snapshot_uri") or "")
            if not snapshot_uri.startswith(prefix):
                raise ValueError("MCP snapshot belongs to another Project")
            snapshot_root = self.runtime.resolve_vault_file(
                project_id, snapshot_uri[len(prefix):].rstrip("/")
            )
            if transport == "stdio":
                entrypoint = str(server.get("entrypoint") or "")
                target = self.runtime.resolve_below(snapshot_root, entrypoint)
                if not target.is_file():
                    raise FileNotFoundError(f"pinned STDIO MCP entrypoint is missing: {entrypoint}")
                bindings.append({
                    **server,
                    "command": sys.executable,
                    "args": ["-m", "assistant_kernel.mcp_server.project_launcher"],
                    "cwd": str(snapshot_root),
                    "env": {
                        "KERNEL_PROJECT_MCP_ROOT": str(snapshot_root),
                        "KERNEL_PROJECT_MCP_ENTRYPOINT": entrypoint,
                        "KERNEL_PROJECT_MCP_SERVER_ID": str(server["server_id"]),
                    },
                })
                continue
            if transport != "streamable-http":
                raise ValueError(f"unsupported MCP transport in plan: {transport}")
            endpoint_ref = str(server.get("endpoint_ref") or "")
            endpoint_env = endpoint_ref[len("env://"):] if endpoint_ref.startswith("env://") else ""
            if not SAFE_ENV.fullmatch(endpoint_env):
                raise ValueError("pinned HTTP MCP endpoint_ref is invalid")
            url = os.environ.get(endpoint_env, "").strip()
            if self._sha256(url.encode("utf-8")) != str(server.get("endpoint_sha256") or ""):
                raise RuntimeError("HTTP MCP endpoint changed after the Run plan was pinned")
            bindings.append({**server, "url": url})
        return bindings

    def write_prompt_bundle(
        self, project_id: str, run_id: str, invocation_id: str, prompt: str,
    ) -> tuple[str, str]:
        runtime = self.runtime.ensure(project_id)
        path = (
            runtime.vault_root / "runs" / run_id / "resolved" / "prompts" /
            f"{invocation_id}.md"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = prompt.encode("utf-8")
        if path.exists() and path.read_bytes() != payload:
            raise RuntimeError("resolved prompt bundle is immutable for one Run")
        if not path.exists():
            temporary = path.with_suffix(".tmp")
            temporary.write_bytes(payload)
            temporary.replace(path)
        return (
            f"vault://{project_id}/runs/{run_id}/resolved/prompts/{invocation_id}.md",
            hashlib.sha256(payload).hexdigest(),
        )
