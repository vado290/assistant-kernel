from __future__ import annotations

import hashlib
import json
import os
import uuid
import difflib
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator
from psycopg.types.json import Jsonb

from assistant_kernel.repositories.agent_config import AgentConfigRepository
from assistant_kernel.repositories.database import AssistantDatabase
from assistant_kernel.storage import ProjectRuntimeManager


class HarnessDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    harness: str = "codex_exec"
    provider: str | None = None
    model: str | None = None
    reasoning_effort: str = "medium"
    timeout_seconds: int = Field(default=600, ge=30, le=1800)


class SkillBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill: str
    version: str | None = None


class ToolBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: str
    version: str | None = None
    defaults: dict[str, Any] = Field(default_factory=dict)


class SkillBindings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    required: list[SkillBinding] = Field(default_factory=list)
    allowed: list[SkillBinding] = Field(default_factory=list)
    denied: list[str] = Field(default_factory=list)


class ToolBindings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    required: list[ToolBinding] = Field(default_factory=list)
    allowed: list[ToolBinding] = Field(default_factory=list)
    denied: list[str] = Field(default_factory=list)


class UpdatePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation: Literal["next_invocation", "next_physical_session"] = "next_invocation"
    active_task: Literal["pinned_until_stage_end", "next_invocation"] = "pinned_until_stage_end"


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["agent-config.v1"] = "agent-config.v1"
    agent_id: str
    config_version: int = Field(default=1, ge=1)
    role: dict[str, Any] = Field(default_factory=dict)
    scope: dict[str, Any]
    harness_defaults: HarnessDefaults = Field(default_factory=HarnessDefaults)
    skills: SkillBindings = Field(default_factory=SkillBindings)
    tools: ToolBindings = Field(default_factory=ToolBindings)
    update_policy: UpdatePolicy = Field(default_factory=UpdatePolicy)

    @model_validator(mode="after")
    def distinct_bindings(self) -> "AgentConfig":
        if len(self.skills.required) != 1:
            raise ValueError("an Agent profile must load exactly one required Skill")
        skill_required = {item.skill for item in self.skills.required}
        skill_allowed = {item.skill for item in self.skills.allowed}
        if skill_required & skill_allowed or (skill_required | skill_allowed) & set(self.skills.denied):
            raise ValueError("a skill cannot be required/allowed/denied at the same time")
        tool_required = {item.tool for item in self.tools.required}
        tool_allowed = {item.tool for item in self.tools.allowed}
        if tool_required & tool_allowed or (tool_required | tool_allowed) & set(self.tools.denied):
            raise ValueError("a tool cannot be required/allowed/denied at the same time")
        protected = {
            "project_id", "root", "cwd", "agent_id", "task_id", "job_id",
            "invocation_id", "effective_scope_sha256", "invocation_token",
        }
        for item in [*self.tools.required, *self.tools.allowed]:
            conflict = protected & set(item.defaults)
            if conflict:
                raise ValueError(f"protected tool defaults are not configurable: {sorted(conflict)}")
        return self


@dataclass(frozen=True)
class ResolvedAgentConfig:
    config: AgentConfig
    config_version_id: str
    scope_version_id: str
    config_uri: str
    config_sha256: str
    scope_policy: dict[str, Any]
    scope_sha256: str
    required_skill_texts: dict[str, str]
    tool_context: dict[str, Any]


TOOL_DEFINITIONS: dict[str, tuple[str, bool, str]] = {
    "workspace_list": ("List files inside the bound workspace.", True, "read"),
    "workspace_search": ("Search UTF-8 text files inside the bound workspace.", True, "read"),
    "workspace_read_text": ("Read one UTF-8 file inside the bound workspace.", True, "read"),
    "workspace_write_text": ("Create or replace one UTF-8 file with hash fencing.", False, "write"),
    "list_project_conversations": ("List conversations in the bound project and time window.", True, "read"),
    "search_project_history": ("Search message originals in the bound project.", True, "read"),
    "get_project_messages": ("Read messages from one in-scope conversation.", True, "read"),
    "get_message_original": ("Read one in-scope immutable message original.", True, "read"),
    "search_project_capabilities": ("Find project Skills and scripts before creating new ones.", True, "read"),
    "list_projects": ("List registered projects without reading their business files.", True, "read"),
    "ask_user": ("Register immutable questions for the current stage.", False, "external"),
    "submit_stage_for_confirmation": ("Register a stage document for human confirmation.", False, "external"),
}


def _binding(tool: str, **defaults: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"tool": tool, "version": "v0001"}
    if defaults:
        result["defaults"] = defaults
    return result


def default_agent_config(agent_id: str, agent_type: str, project_id: str | None) -> AgentConfig:
    stage = agent_type.removeprefix("stage_")
    read_tools = [
        _binding("workspace_list"), _binding("workspace_search", limit=50),
        _binding("workspace_read_text", max_bytes=262144),
        _binding("list_project_conversations", lookback_days=365, limit=30),
        _binding("search_project_history", lookback_days=365, limit=30),
        _binding("get_project_messages", lookback_days=365, limit=50),
        _binding("get_message_original"), _binding("search_project_capabilities", limit=20),
    ]
    denied = ["workspace_write_text"]
    if agent_type == "butler":
        skill = "assistant-butler-intake"
        tools = [
            _binding("list_projects"),
            _binding("list_project_conversations", lookback_days=365, limit=30),
            _binding("search_project_history", lookback_days=365, limit=30),
            _binding("get_project_messages", lookback_days=365, limit=50),
            _binding("get_message_original"),
        ]
        scope_mode = "system"
    elif agent_type == "project_manager":
        skill = "assistant-project-manager"
        tools = read_tools
        scope_mode = "own_project"
    else:
        if stage not in {"requirements", "implementation", "review"}:
            raise ValueError(f"unsupported stage profile: {stage}")
        skill = f"assistant-{stage}"
        tools = [*read_tools, _binding("ask_user"), _binding("submit_stage_for_confirmation")]
        scope_mode = "task_stage"
        if stage == "implementation":
            tools.append(_binding("workspace_write_text", max_bytes=1048576))
            denied = []
    return AgentConfig.model_validate(
        {
            "agent_id": agent_id,
            "scope": {"mode": scope_mode, "project_id": project_id},
            "skills": {"required": [{"skill": skill, "version": "current"}]},
            "tools": {"required": tools, "denied": denied},
        }
    )


class AgentConfigManager:
    def __init__(self, settings):
        self.settings = settings
        self.runtime = ProjectRuntimeManager(settings.data_root)
        self.repository = AgentConfigRepository(AssistantDatabase(settings))
        self.prompt_skills = Path(__file__).parents[1] / "prompts" / "skills"

    @staticmethod
    def _canonical_sha(value: Any) -> str:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def config_relative_path(self, agent_id: str, agent_type: str) -> str:
        if agent_type in {"butler", "project_manager"}:
            return "agent-config.yaml"
        stage = agent_type.removeprefix("stage_")
        if agent_type.startswith("stage_"):
            return f"stages/{stage}/agent-config.yaml"
        return f"roles/{uuid.uuid5(uuid.NAMESPACE_URL, agent_id).hex}/agent-config.yaml"

    def default_scope_policy(self, agent_type: str, project_id: str | None) -> dict[str, Any]:
        runtime_project = str(project_id or "system")
        can_write = agent_type == "stage_implementation"
        return {
            "schema_version": "scope-policy.v1",
            "file_roots": [{"uri": f"vault://{runtime_project}/", "access": "write" if can_write else "read"}],
            "history_scopes": [{"project": project_id, "access": ["list", "read", "search"]}],
            "capability_scopes": ([] if agent_type == "butler" else [{"project": project_id, "access": ["list", "read", "use"]}]),
            "project_registry_actions": (["list", "propose", "create", "route"] if agent_type == "butler" else []),
            "denied_paths": [".env", "runtime/executors", "runtime/codex-home/auth.json", "runtime/trae-home"],
            "denied_write_paths": [
                "agent-config.yaml", "**/agent-config.yaml",
                "config-versions/**", "**/config-versions/**",
                "config-history/**", "**/config-history/**",
                ".agents/skills/**", "roles/versions/**", "**/role.md",
            ],
        }

    def _ensure_skill(self, root: Path, name: str) -> tuple[str, str]:
        target = self.runtime.resolve_below(root, f".agents/skills/{name}/SKILL.md")
        if not target.exists():
            source = self.prompt_skills / name / "SKILL.md"
            if not source.is_file():
                raise ValueError(f"required skill is not installed: {name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        content = target.read_text(encoding="utf-8")
        return content, hashlib.sha256(content.encode("utf-8")).hexdigest()

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(content, encoding="utf-8")
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()

    @staticmethod
    def _tool_release_sha() -> str:
        root = Path(__file__).parents[1] / "mcp_tools"
        digest = hashlib.sha256()
        for path in sorted(root.glob("*.py")):
            digest.update(path.name.encode("utf-8"))
            digest.update(path.read_bytes())
        return digest.hexdigest()

    async def resolve(
        self, *, agent_id: str, agent_type: str, project_id: str | None, role_version_id: str | None
    ) -> ResolvedAgentConfig:
        runtime_project = str(project_id or "system")
        root = self.runtime.ensure(runtime_project).vault_root
        relative = self.config_relative_path(agent_id, agent_type)
        projection = self.runtime.resolve_below(root, relative)
        active = await self.repository.get_active(agent_id)
        if active is not None:
            active_payload = dict(active["config_json"])
            active_payload["scope"] = {
                "mode": active_payload.get("scope", {}).get("mode"),
                "agent_scope_version_id": str(active["effective_scope_version_id"]),
            }
            config = AgentConfig.model_validate(active_payload)
            policy = dict(active["policy"])
            scope = active
            row = active
            uri = str(active["config_uri"])
            serialized = yaml.safe_dump(config.model_dump(mode="json", exclude_none=True), allow_unicode=True, sort_keys=False)
            if not projection.exists() or projection.read_text(encoding="utf-8") != serialized:
                self._atomic_write(projection, serialized)
        else:
            if projection.exists():
                loaded = yaml.safe_load(projection.read_text(encoding="utf-8"))
                config = AgentConfig.model_validate(loaded)
            else:
                config = default_agent_config(agent_id, agent_type, project_id)
            if config.agent_id != agent_id:
                raise ValueError("agent-config.yaml agent_id does not match the current agent")
            policy = self.default_scope_policy(agent_type, project_id)
            scope = await self.repository.ensure_scope_version(agent_id=agent_id, policy=policy)
            config_payload = config.model_dump(mode="json", exclude_none=True)
            config_payload["role"] = {"role_version_id": role_version_id}
            config_payload["scope"] = {
                "mode": config.scope.get("mode"),
                "agent_scope_version_id": str(scope["agent_scope_version_id"]),
            }
            config = AgentConfig.model_validate(config_payload)
            version_dir = projection.parent / "config-versions" / f"v{config.config_version:04d}"
            version_file = version_dir / "agent-config.yaml"
            version_dir.mkdir(parents=True, exist_ok=True)
            serialized = yaml.safe_dump(config.model_dump(mode="json", exclude_none=True), allow_unicode=True, sort_keys=False)
            self._atomic_write(projection, serialized)
            if not version_file.exists():
                version_file.write_text(serialized, encoding="utf-8")
            uri = f"vault://{runtime_project}/{version_file.relative_to(root).as_posix()}"
            row = await self.repository.ensure_config_version(
                agent_id=agent_id,
                scope_version_id=str(scope["agent_scope_version_id"]),
                config=config.model_dump(mode="json", exclude_none=True),
                config_uri=uri,
            )
        required_skill_texts: dict[str, str] = {}
        skill_bindings: list[dict[str, Any]] = []
        for item in config.skills.required:
            content, digest = self._ensure_skill(root, item.skill)
            required_skill_texts[item.skill] = content
            skill_bindings.append({"name": item.skill, "version": item.version, "sha256": digest})
        all_tools: list[dict[str, Any]] = []
        code_sha = self._tool_release_sha()
        for mode, items in (("required", config.tools.required), ("allowed", config.tools.allowed)):
            for item in items:
                if item.tool not in TOOL_DEFINITIONS:
                    if mode == "required":
                        raise ValueError(f"required tool is not registered: {item.tool}")
                    continue
                description, read_only, risk = TOOL_DEFINITIONS[item.tool]
                definition = await self.repository.ensure_tool_definition(
                    tool_key=item.tool,
                    description=description,
                    read_only=read_only,
                    risk_class=risk,
                    code_sha256=code_sha,
                )
                all_tools.append(
                    {
                        "name": item.tool,
                        "binding_mode": mode,
                        "defaults": item.defaults,
                        "tool_definition_id": str(definition["tool_definition_id"]),
                    }
                )
        scope_sha = str(scope["policy_sha256"])
        tool_context = {
            "schema_version": "agent-mcp-context.v1",
            "agent_id": agent_id,
            "agent_type": agent_type,
            "project_id": project_id,
            "runtime_project_id": runtime_project,
            "scope_policy": policy,
            "effective_scope_sha256": scope_sha,
            "tools": all_tools,
            "skill_bindings": skill_bindings,
        }
        return ResolvedAgentConfig(
            config=config,
            config_version_id=str(row["agent_config_version_id"]),
            scope_version_id=str(scope.get("effective_scope_version_id") or scope["agent_scope_version_id"]),
            config_uri=uri,
            config_sha256=str(row["config_sha256"]),
            scope_policy=policy,
            scope_sha256=scope_sha,
            required_skill_texts=required_skill_texts,
            tool_context=tool_context,
        )

    async def record_invocation(self, invocation_id: str, resolved: ResolvedAgentConfig) -> None:
        await self.repository.record_invocation_tools(
            invocation_id=invocation_id,
            bindings=[
                {**item, "selection_source": "agent"}
                for item in resolved.tool_context["tools"]
            ],
            effective_scope_sha256=resolved.scope_sha256,
        )

    async def publish(
        self, *, agent_id: str, content_yaml: str, change_reason: str, accepted_by: str
    ) -> dict[str, Any]:
        if not change_reason.strip() or not accepted_by.strip():
            raise ValueError("change_reason and accepted_by are required")
        loaded = yaml.safe_load(content_yaml)
        candidate = AgentConfig.model_validate(loaded)
        if candidate.agent_id != agent_id:
            raise ValueError("candidate agent_id does not match the target agent")
        async with self.repository.database.connection() as connection:
            agent = await (
                await connection.execute(
                    "SELECT * FROM assistant.agents WHERE agent_id=%s AND status='active'",
                    (agent_id,),
                )
            ).fetchone()
            if agent is None:
                raise ValueError("active agent not found")
            active = await (
                await connection.execute(
                    """
                    SELECT c.*,s.agent_scope_version_id AS effective_scope_version_id,
                           s.policy,s.policy_sha256
                    FROM assistant.agent_config_versions c
                    JOIN assistant.agent_scope_versions s
                      ON s.agent_scope_version_id=c.agent_scope_version_id
                    WHERE c.agent_id=%s AND c.status='active'
                    """,
                    (agent_id,),
                )
            ).fetchone()
            role = await (
                await connection.execute(
                    """
                    SELECT rv.role_version_id FROM assistant.roles r
                    JOIN assistant.role_versions rv ON rv.role_id=r.role_id
                    WHERE r.agent_id=%s AND r.name='primary' AND r.status='active'
                      AND rv.status='active' ORDER BY rv.version DESC LIMIT 1
                    """,
                    (agent_id,),
                )
            ).fetchone()
        if active is None or role is None:
            raise ValueError("initialize the Agent config and Role before publishing a revision")
        next_version = int(active["version"]) + 1
        payload = candidate.model_dump(mode="json", exclude_none=True)
        payload["config_version"] = next_version
        payload["role"] = {"role_version_id": str(role["role_version_id"])}
        payload["scope"] = {
            "mode": candidate.scope.get("mode") or active["config_json"]["scope"].get("mode"),
            "agent_scope_version_id": str(agent["active_scope_version_id"]),
        }
        candidate = AgentConfig.model_validate(payload)
        project_id = str(agent["project_id"]) if agent["project_id"] else None
        runtime_project = str(project_id or "system")
        root = self.runtime.ensure(runtime_project).vault_root
        projection = self.runtime.resolve_below(
            root, self.config_relative_path(agent_id, str(agent["agent_type"]))
        )
        for item in [*candidate.skills.required, *candidate.skills.allowed]:
            self._ensure_skill(root, item.skill)
        configured_tools = {
            item.tool for item in [*candidate.tools.required, *candidate.tools.allowed]
        } | set(candidate.tools.denied)
        unknown = configured_tools - set(TOOL_DEFINITIONS)
        if unknown:
            raise ValueError(f"unknown tools in Agent config: {sorted(unknown)}")
        serialized = yaml.safe_dump(
            candidate.model_dump(mode="json", exclude_none=True), allow_unicode=True, sort_keys=False
        )
        config_hash = self._canonical_sha(candidate.model_dump(mode="json", exclude_none=True))
        version_dir = projection.parent / "config-versions" / f"v{next_version:04d}"
        version_file = version_dir / "agent-config.yaml"
        previous_text = yaml.safe_dump(active["config_json"], allow_unicode=True, sort_keys=False)
        diff = "".join(
            difflib.unified_diff(
                previous_text.splitlines(keepends=True),
                serialized.splitlines(keepends=True),
                fromfile=f"agent-config-v{active['version']:04d}.yaml",
                tofile=f"agent-config-v{next_version:04d}.yaml",
            )
        )
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        history_name = (
            f"v{next_version:04d}__{stamp}__from-v{int(active['version']):04d}__{config_hash[:8]}.md"
        )
        history_file = projection.parent / "config-history" / history_name
        version_dir.mkdir(parents=True, exist_ok=True)
        history_file.parent.mkdir(parents=True, exist_ok=True)
        if version_file.exists() and version_file.read_text(encoding="utf-8") != serialized:
            raise ValueError("candidate version path already contains different content")
        version_file.write_text(serialized, encoding="utf-8")
        history = (
            "---\n"
            f"agent_id: {json.dumps(agent_id, ensure_ascii=False)}\n"
            f"version: {next_version}\n"
            f"parent_agent_config_version_id: {active['agent_config_version_id']}\n"
            f"config_sha256: {config_hash}\n"
            f"accepted_by: {json.dumps(accepted_by, ensure_ascii=False)}\n"
            f"change_reason: {json.dumps(change_reason, ensure_ascii=False)}\n"
            "---\n\n"
            "```diff\n" + diff + "```\n"
        )
        history_file.write_text(history, encoding="utf-8")
        diff_hash = hashlib.sha256(history.encode("utf-8")).hexdigest()
        config_uri = f"vault://{runtime_project}/{version_file.relative_to(root).as_posix()}"
        diff_uri = f"vault://{runtime_project}/{history_file.relative_to(root).as_posix()}"
        async with self.repository.database.connection() as connection:
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (f"agent-config:{agent_id}",),
            )
            current = await (
                await connection.execute(
                    "SELECT * FROM assistant.agent_config_versions "
                    "WHERE agent_id=%s AND status='active' FOR UPDATE",
                    (agent_id,),
                )
            ).fetchone()
            if current is None or str(current["agent_config_version_id"]) != str(active["agent_config_version_id"]):
                raise RuntimeError("active Agent config changed while publishing")
            await connection.execute(
                "UPDATE assistant.agent_config_versions SET status='retired',retired_at=now() "
                "WHERE agent_config_version_id=%s",
                (current["agent_config_version_id"],),
            )
            row = await (
                await connection.execute(
                    """
                    INSERT INTO assistant.agent_config_versions(
                        agent_config_version_id,agent_id,version,parent_agent_config_version_id,
                        agent_scope_version_id,config_json,config_uri,config_sha256,
                        diff_uri,diff_sha256,status,change_reason,accepted_by,
                        accepted_at,activated_at
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'active',%s,%s,now(),now())
                    RETURNING *
                    """,
                    (
                        str(uuid.uuid4()), agent_id, next_version,
                        current["agent_config_version_id"], agent["active_scope_version_id"],
                        Jsonb(yaml.safe_load(serialized)), config_uri, config_hash, diff_uri, diff_hash,
                        change_reason, accepted_by,
                    ),
                )
            ).fetchone()
            await connection.execute(
                "UPDATE assistant.agents SET active_agent_config_version_id=%s,updated_at=now() "
                "WHERE agent_id=%s",
                (row["agent_config_version_id"], agent_id),
            )
            await connection.commit()
        self._atomic_write(projection, serialized)
        return {
            "agent_id": agent_id,
            "agent_config_version_id": str(row["agent_config_version_id"]),
            "version": next_version,
            "config_uri": config_uri,
            "config_sha256": config_hash,
            "diff_uri": diff_uri,
            "diff_sha256": diff_hash,
            "status": "active",
        }

    async def publish_scope(
        self, *, agent_id: str, policy: dict[str, Any], change_reason: str, accepted_by: str
    ) -> dict[str, Any]:
        if policy.get("schema_version") != "scope-policy.v1":
            raise ValueError("scope policy schema_version must be scope-policy.v1")
        roots = policy.get("file_roots")
        if not isinstance(roots, list) or not roots:
            raise ValueError("scope policy requires at least one file_root")
        for root in roots:
            if not isinstance(root, dict) or root.get("access") not in {"read", "write"}:
                raise ValueError("each file_root requires read or write access")
            uri = str(root.get("uri") or "")
            if not uri.startswith("vault://") or "/" not in uri.removeprefix("vault://"):
                raise ValueError("file_root must be a vault URI")
        if not isinstance(policy.get("denied_paths", []), list) or not isinstance(policy.get("denied_write_paths", []), list):
            raise ValueError("scope deny rules must be lists")
        scope = await self.repository.publish_scope_version(
            agent_id=agent_id,
            policy=policy,
            change_reason=change_reason,
            accepted_by=accepted_by,
        )
        active = await self.repository.get_active(agent_id)
        if active is None:
            raise ValueError("active Agent config not found")
        config = AgentConfig.model_validate(active["config_json"])
        config_payload = config.model_dump(mode="json", exclude_none=True)
        config_payload["scope"] = {
            "mode": config.scope.get("mode"),
            "agent_scope_version_id": str(scope["agent_scope_version_id"]),
        }
        published = await self.publish(
            agent_id=agent_id,
            content_yaml=yaml.safe_dump(config_payload, allow_unicode=True, sort_keys=False),
            change_reason=f"scope update: {change_reason}",
            accepted_by=accepted_by,
        )
        return {
            "agent_id": agent_id,
            "agent_scope_version_id": str(scope["agent_scope_version_id"]),
            "scope_version": int(scope["version"]),
            "policy": scope["policy"],
            "policy_sha256": scope["policy_sha256"],
            "agent_config": published,
        }
