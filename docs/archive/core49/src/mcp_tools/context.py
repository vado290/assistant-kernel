from __future__ import annotations

import hashlib
import json
import os
import uuid
import fnmatch
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field

from assistant_kernel.config import KernelSettings
from assistant_kernel.repositories.database import AssistantDatabase
from assistant_kernel.storage import ProjectRuntimeManager


class ContextTool(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    binding_mode: str
    defaults: dict[str, Any] = Field(default_factory=dict)
    tool_definition_id: str | None = None


class ContextDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str
    invocation_id: str
    agent_config_version_id: str
    agent_id: str
    agent_type: str
    project_id: str | None = None
    runtime_project_id: str
    job_id: str | None = None
    worker_instance_id: str | None = None
    node_run_id: str | None = None
    turn_key: str | None = None
    scope_policy: dict[str, Any]
    effective_scope_sha256: str
    tools: list[ContextTool]
    skill_bindings: list[dict[str, Any]] = Field(default_factory=list)


@dataclass
class ToolAuditState:
    tool_call_id: str
    target_uri: str | None = None
    previous_sha256: str | None = None
    result_sha256: str | None = None


class AgentMcpContext:
    def __init__(self, settings: KernelSettings, document: ContextDocument):
        self.settings = settings
        self.document = document
        self.database = AssistantDatabase(settings)
        self.runtime = ProjectRuntimeManager(settings.data_root)
        self.root = self.runtime.describe(document.runtime_project_id).vault_root
        self._tools = {item.name: item for item in document.tools}

    @property
    def project_id(self) -> str | None:
        return self.document.project_id

    def allows(self, tool_name: str) -> bool:
        return tool_name in self._tools

    def defaults(self, tool_name: str) -> dict[str, Any]:
        item = self._tools.get(tool_name)
        return dict(item.defaults) if item else {}

    def access(self) -> str:
        roots = self.document.scope_policy.get("file_roots") or []
        return str(roots[0].get("access", "read")) if roots else "read"

    def _relative(self, relative_path: str, *, write: bool = False) -> Path:
        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise PermissionError("path must be relative to the bound workspace")
        target = (self.root / relative).resolve()
        resolved_root = self.root.resolve()
        if target != resolved_root and resolved_root not in target.parents:
            raise PermissionError("resolved path escaped the bound workspace")
        normalized = target.relative_to(resolved_root).as_posix()
        denied = [str(item).replace("\\", "/").lower() for item in self.document.scope_policy.get("denied_paths", [])]
        if write:
            denied.extend(
                str(item).replace("\\", "/").lower()
                for item in self.document.scope_policy.get("denied_write_paths", [])
            )
        lowered = normalized.lower()
        if any(
            lowered == rule or lowered.startswith(rule.rstrip("/**") + "/")
            or (rule == ".env" and Path(lowered).name == ".env")
            or fnmatch.fnmatch(lowered, rule)
            for rule in denied
        ):
            raise PermissionError("path is denied by the effective scope")
        return target

    def resolve_read(self, relative_path: str) -> Path:
        return self._relative(relative_path)

    def resolve_write(self, relative_path: str) -> Path:
        if self.access() != "write":
            raise PermissionError("the effective scope is read-only")
        return self._relative(relative_path, write=True)

    async def assert_live(self) -> None:
        if not self.document.job_id:
            return
        async with self.database.connection() as connection:
            row = await (
                await connection.execute(
                    """
                    SELECT j.status,j.worker_instance_id,j.lease_until,j.job_type,
                           m.project_id_snapshot,m.agent_id_snapshot,
                           t.project_id AS task_project_id,n.node_id,n.node_run_id
                    FROM assistant.control_jobs j
                    LEFT JOIN assistant.messages m ON m.message_id=j.message_id
                    LEFT JOIN assistant.tasks t ON t.task_id=j.task_id
                    LEFT JOIN assistant.node_runs n ON n.job_id=j.job_id
                    WHERE j.job_id=%s
                    """,
                    (self.document.job_id,),
                )
            ).fetchone()
            bindings = await (
                await connection.execute(
                    """
                    SELECT b.tool_definition_id,b.binding_mode,b.effective_default_parameters,
                           b.effective_scope_sha256,d.tool_key
                    FROM assistant.invocation_tool_bindings b
                    JOIN assistant.mcp_tool_definitions d
                      ON d.tool_definition_id=b.tool_definition_id
                    WHERE b.invocation_id=%s
                    """,
                    (self.document.invocation_id,),
                )
            ).fetchall()
        if row is None or row["status"] != "running":
            raise PermissionError("the bound job is not running")
        if row["lease_until"] is None or row["lease_until"] <= datetime.now(timezone.utc):
            raise PermissionError("the bound job lease expired")
        if self.document.worker_instance_id and str(row["worker_instance_id"]) != self.document.worker_instance_id:
            raise PermissionError("the bound worker lease changed")
        if row["job_type"] == "conversation_turn":
            if row["project_id_snapshot"] != self.document.project_id or row["agent_id_snapshot"] != self.document.agent_id:
                raise PermissionError("conversation job identity does not match the MCP context")
        elif row["job_type"] == "stage_run":
            if row["task_project_id"] != self.document.project_id:
                raise PermissionError("stage job project does not match the MCP context")
            if self.document.node_run_id and str(row["node_run_id"]) != self.document.node_run_id:
                raise PermissionError("stage node does not match the MCP context")
            expected_agent = f"project:{self.document.project_id}:{row['node_id']}"
            if expected_agent != self.document.agent_id:
                raise PermissionError("stage agent does not match the MCP context")
        expected_tools = {
            str(item["tool_definition_id"]): (
                item["tool_key"], item["binding_mode"], item["effective_default_parameters"] or {},
                item["effective_scope_sha256"],
            )
            for item in bindings
        }
        actual_tools = {
            str(item.tool_definition_id): (
                item.name, item.binding_mode, item.defaults,
                self.document.effective_scope_sha256,
            )
            for item in self.document.tools
            if item.tool_definition_id
        }
        if not expected_tools or actual_tools != expected_tools:
            raise PermissionError("MCP tools or Scope do not match the frozen invocation bindings")

    @asynccontextmanager
    async def audit(
        self, tool_name: str, arguments: dict[str, Any], target_uri: str | None = None
    ) -> AsyncIterator[ToolAuditState]:
        await self.assert_live()
        call_id = str(uuid.uuid4())
        digest = hashlib.sha256(
            json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        state = ToolAuditState(call_id, target_uri=target_uri)
        async with self.database.connection() as connection:
            await connection.execute(
                """
                INSERT INTO assistant.mcp_tool_call_audits(
                    tool_call_id,invocation_id,job_id,agent_id,project_id,tool_key,
                    arguments_sha256,target_uri,status
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'started')
                """,
                (
                    call_id, self.document.invocation_id, self.document.job_id,
                    self.document.agent_id, self.document.project_id, tool_name,
                    digest, target_uri,
                ),
            )
            await connection.commit()
        try:
            yield state
        except Exception as exc:
            async with self.database.connection() as connection:
                await connection.execute(
                    """
                    UPDATE assistant.mcp_tool_call_audits
                    SET status=%s,error_json=%s,previous_sha256=%s,result_sha256=%s,
                        target_uri=COALESCE(%s,target_uri),ended_at=now()
                    WHERE tool_call_id=%s
                    """,
                    (
                        "denied" if isinstance(exc, PermissionError) else "failed",
                        Jsonb({"type": type(exc).__name__, "message": str(exc)[:1000]}),
                        state.previous_sha256, state.result_sha256, state.target_uri, call_id,
                    ),
                )
                await connection.commit()
            raise
        else:
            async with self.database.connection() as connection:
                await connection.execute(
                    """
                    UPDATE assistant.mcp_tool_call_audits
                    SET status='succeeded',previous_sha256=%s,result_sha256=%s,
                        target_uri=COALESCE(%s,target_uri),ended_at=now()
                    WHERE tool_call_id=%s
                    """,
                    (state.previous_sha256, state.result_sha256, state.target_uri, call_id),
                )
                await connection.commit()


def load_agent_mcp_context() -> AgentMcpContext:
    raw = os.environ.get("KERNEL_AGENT_CONTEXT_JSON", "")
    if not raw:
        raise RuntimeError("KERNEL_AGENT_CONTEXT_JSON is required")
    document = ContextDocument.model_validate_json(raw)
    settings = KernelSettings.from_environment()
    expected = hashlib.sha256(
        json.dumps(document.scope_policy, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if expected != document.effective_scope_sha256:
        raise PermissionError("effective scope hash mismatch")
    return AgentMcpContext(settings, document)
