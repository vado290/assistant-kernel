from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from assistant_kernel.config import KernelSettings
from assistant_kernel.storage import ProjectRuntimeManager


DEFAULT_SHARED_SKILLS = (
    {"name": "assistant-volcengine-models", "source": "kernel"},
)
DEFAULT_SHARED_TOOLS = (
    "volcengine_model_status", "volcengine_generate", "volcengine_ocr",
)

if TYPE_CHECKING:
    from assistant_kernel.repositories import CoreRepository


@dataclass(frozen=True)
class FileAgentConfig:
    agent_id: str
    agent_key: str
    kind: str
    status: str
    display_name: str
    aliases: tuple[str, ...]
    role_text: str
    skill_texts: tuple[str, ...]
    skill_bindings: tuple[dict[str, Any], ...]
    provider: str | None
    model: str | None
    reasoning_effort: str
    context_policy: str
    model_context_window_tokens: int | None
    model_auto_compact_token_limit: int | None
    timeout_seconds: int
    sandbox: str
    config_sha256: str
    source_files: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    scope_profile: str
    mcp_servers: tuple[dict[str, Any], ...]
    entrypoints: dict[str, bool]
    triggers: tuple[str, ...]
    input_schema: str | None
    output_schema: str | None


@dataclass(frozen=True)
class AgentDescriptor:
    agent_id: str
    agent_key: str
    kind: str
    status: str
    display_name: str
    aliases: tuple[str, ...]
    entrypoints: dict[str, bool]
    config_path: str


class AgentFileLoader:
    """Loads the live Agent, Role, and Skills directly from a Project Vault."""

    def __init__(self, settings: KernelSettings):
        self.settings = settings
        self.runtime = ProjectRuntimeManager(settings.data_root)

    @staticmethod
    def _safe_slug(value: str, label: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value):
            raise ValueError(f"{label} must be a safe slug")
        return value

    @staticmethod
    def _read_yaml(path: Path) -> dict[str, Any]:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ValueError(f"YAML document must be an object: {path}")
        return data

    def project_layout_version(self, project_id: str) -> int:
        root = self.runtime.ensure(project_id).vault_root
        project_path = root / "project.yaml"
        if not project_path.is_file():
            return 1
        data = self._read_yaml(project_path)
        version = int(data.get("layout_version") or 0)
        if version not in {1, 2}:
            raise ValueError("project.yaml layout_version must be 1 or 2")
        if data.get("project_id") and str(data["project_id"]) != project_id:
            raise ValueError("project.yaml project_id does not match its Vault")
        return version

    def _v2_agent_entries(self, project_id: str) -> list[tuple[Path, dict[str, Any]]]:
        root = self.runtime.ensure(project_id).vault_root
        agents_root = root / "agents"
        if not agents_root.is_dir():
            return []
        entries: list[tuple[Path, dict[str, Any]]] = []
        seen_ids: set[str] = set()
        seen_keys: set[str] = set()
        seen_aliases: dict[str, str] = {}
        for config_path in sorted(agents_root.glob("*/config.yaml")):
            data = self._read_yaml(config_path)
            if str(data.get("schema_version") or "") != "agent-config.v2":
                raise ValueError(f"v2 Agent must use agent-config.v2: {config_path}")
            agent_id = self._safe_slug(
                str(data.get("agent_id") or config_path.parent.name), "agent_id"
            )
            if agent_id != config_path.parent.name:
                raise ValueError(f"agent_id must match its directory: {config_path}")
            agent_key = str(data.get("agent_key") or f"project:{project_id}:{agent_id}")
            expected_prefix = "system-" if project_id == "system" else f"project:{project_id}:"
            if not agent_key.startswith(expected_prefix):
                raise ValueError(f"Agent key belongs to another Project: {agent_key}")
            status = str(data.get("status") or "active")
            if status not in {"active", "archived", "disabled"}:
                raise ValueError(f"unsupported Agent status: {status}")
            aliases = tuple(
                self._safe_slug(str(item), "Agent alias")
                for item in (data.get("aliases") or [])
            )
            if agent_id in seen_ids or agent_key in seen_keys:
                raise ValueError("duplicate Agent identity in Project registry")
            seen_ids.add(agent_id)
            seen_keys.add(agent_key)
            for alias in (agent_id, *aliases):
                owner = seen_aliases.setdefault(alias.lower(), agent_id)
                if owner != agent_id:
                    raise ValueError(f"duplicate Agent alias: {alias}")
            entries.append((config_path, data))
        return entries

    def list_agents(self, project_id: str) -> list[AgentDescriptor]:
        root = self.runtime.ensure(project_id).vault_root
        if self.project_layout_version(project_id) == 2:
            result = []
            for config_path, data in self._v2_agent_entries(project_id):
                agent_id = str(data.get("agent_id") or config_path.parent.name)
                agent_key = str(data.get("agent_key") or f"project:{project_id}:{agent_id}")
                entrypoints = data.get("entrypoints") or {}
                if not isinstance(entrypoints, dict):
                    raise ValueError(f"entrypoints must be an object: {config_path}")
                result.append(AgentDescriptor(
                    agent_id=agent_id,
                    agent_key=agent_key,
                    kind=str(data.get("kind") or "business"),
                    status=str(data.get("status") or "active"),
                    display_name=str(data.get("display_name") or agent_id),
                    aliases=tuple(str(item) for item in (data.get("aliases") or [])),
                    entrypoints={str(key): bool(value) for key, value in entrypoints.items()},
                    config_path=str(config_path),
                ))
            return result

        config_paths = []
        if (root / "agent-config.yaml").is_file():
            config_paths.append(root / "agent-config.yaml")
        config_paths.extend(sorted((root / "stages").glob("*/agent-config.yaml")))
        result = []
        for config_path in config_paths:
            data = self._read_yaml(config_path)
            agent_key = str(data.get("agent_key") or "")
            agent_id = (
                "pm" if agent_key.endswith(":manager") else
                agent_key.rsplit(":", 1)[-1] if agent_key else config_path.parent.name
            )
            result.append(AgentDescriptor(
                agent_id=agent_id,
                agent_key=agent_key,
                kind="manager" if agent_id == "pm" else "stage",
                status=str(data.get("status") or "active"),
                display_name=agent_id,
                aliases=(),
                entrypoints={"pm_handoff": True, "feishu_slash": agent_id == "pm"},
                config_path=str(config_path),
            ))
        return result

    def resolve_agent(
        self, project_id: str, selector: str, *, require_feishu_slash: bool = False,
    ) -> AgentDescriptor:
        normalized = selector.strip().lower()
        matches = []
        for item in self.list_agents(project_id):
            names = {item.agent_id.lower(), item.agent_key.lower(), *(a.lower() for a in item.aliases)}
            if normalized in names:
                matches.append(item)
        if not matches:
            raise KeyError(f"Agent was not found in Project registry: {selector}")
        if len(matches) != 1:
            raise ValueError(f"Agent selector is ambiguous: {selector}")
        selected = matches[0]
        if selected.status != "active":
            raise PermissionError(f"Agent is not active: {selected.agent_id}")
        if require_feishu_slash and not selected.entrypoints.get("feishu_slash", False):
            raise PermissionError(f"Agent does not allow Feishu slash entry: {selected.agent_id}")
        return selected

    def _agent_root(self, project_id: str, agent_key: str) -> Path:
        root = self.runtime.ensure(project_id).vault_root
        if self.project_layout_version(project_id) == 2:
            matches = [
                path.parent for path, data in self._v2_agent_entries(project_id)
                if str(data.get("agent_key") or f"project:{project_id}:{path.parent.name}") == agent_key
            ]
            if len(matches) != 1:
                raise FileNotFoundError(f"published Agent configuration is missing: {agent_key}")
            return matches[0]
        stage = agent_key.rsplit(":", 1)[-1]
        stage_root = root / "stages" / stage
        if stage not in {"manager", "system-butler"} and (stage_root / "agent-config.yaml").is_file():
            return stage_root
        return root

    def load(
        self, project_id: str, agent_key: str, *, task_kind: str | None = None,
        requested_skills: list[str] | None = None,
    ) -> FileAgentConfig:
        root = self.runtime.ensure(project_id).vault_root
        agent_root = self._agent_root(project_id, agent_key)
        layout_version = self.project_layout_version(project_id)
        config_path = agent_root / ("config.yaml" if layout_version == 2 else "agent-config.yaml")
        data: dict[str, Any] = {}
        source_files: list[Path] = []
        if config_path.is_file():
            data = self._read_yaml(config_path)
            source_files.append(config_path)
            if data.get("status") in {"archived", "disabled"}:
                raise ValueError(f"Agent configuration is archived: {agent_key}")

        role_relative = str(data.get("role") or "role.md")
        role_path = self.runtime.resolve_below(agent_root, role_relative)
        role_text = role_path.read_text(encoding="utf-8") if role_path.is_file() else (
            "You are the assistant for this Project. Follow the user's request, preserve facts, "
            "and report only work that was actually completed."
        )
        if role_path.is_file():
            source_files.append(role_path)

        configured_skills = data.get("skills") or []
        skill_policy = configured_skills if isinstance(configured_skills, dict) else {}
        required = (
            list(skill_policy.get("required") or [])
            if skill_policy else list(configured_skills)
        )
        existing_required = {
            str((item.get("name") or item.get("skill")) if isinstance(item, dict) else item)
            for item in required
        }
        required = [
            *(
                item for item in DEFAULT_SHARED_SKILLS
                if str(item["name"]) not in existing_required
            ),
            *required,
        ]
        if task_kind and skill_policy:
            task_skills = skill_policy.get("task_kinds") or {}
            required.extend(list(task_skills.get(task_kind) or []))
        requested = [str(item) for item in (requested_skills or [])]
        if requested:
            available_items = list(skill_policy.get("available") or [])
            available_names = {
                str(item.get("name") or item.get("skill")) if isinstance(item, dict) else str(item)
                for item in available_items
            }
            unexpected = set(requested) - available_names
            if unexpected:
                raise PermissionError(
                    "requested Skill is not available to this Stage: " + ", ".join(sorted(unexpected))
                )
            by_name = {
                str(item.get("name") or item.get("skill")) if isinstance(item, dict) else str(item): item
                for item in available_items
            }
            required.extend(by_name[name] for name in requested)
        skill_texts: list[str] = []
        skill_bindings: list[dict[str, Any]] = []
        seen_skills: set[str] = set()
        for item in required:
            name = str(
                (item.get("name") or item.get("skill")) if isinstance(item, dict) else item
            ).strip()
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", name):
                raise ValueError("Skill name must be a safe slug")
            if name in seen_skills:
                continue
            seen_skills.add(name)
            source = str(item.get("source") or "auto") if isinstance(item, dict) else "auto"
            project_path = root / ".agents" / "skills" / name / "SKILL.md"
            legacy_path = root / "capabilities" / "skills" / name / "SKILL.md"
            kernel_path = Path(__file__).parents[1] / "prompts" / "skills" / name / "SKILL.md"
            if source == "project":
                candidates = (project_path,)
            elif source == "kernel":
                candidates = (kernel_path,)
            elif source == "auto":
                candidates = (project_path, legacy_path, kernel_path)
            else:
                raise ValueError(f"unsupported Skill source: {source}")
            path = next((candidate for candidate in candidates if candidate.is_file()), None)
            if path is None:
                raise FileNotFoundError(f"required Skill is missing: {name}")
            skill_texts.append(path.read_text(encoding="utf-8"))
            skill_root = path.parent
            files = [
                candidate for candidate in sorted(skill_root.rglob("*"))
                if candidate.is_file()
                and not any(part in {"__pycache__", ".pytest_cache", ".git"}
                            for part in candidate.relative_to(skill_root).parts)
            ]
            source_files.extend(files)
            file_entries = []
            for candidate in files:
                payload = candidate.read_bytes()
                file_entries.append({
                    "path": candidate.relative_to(skill_root).as_posix(),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "size": len(payload),
                })
            binding_source = (
                "project" if root in path.parents
                else "kernel"
            )
            binding_hash = hashlib.sha256(
                json.dumps(file_entries, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            skill_bindings.append({
                "name": name,
                "source": binding_source,
                "bundle_hash": binding_hash,
                "files": file_entries,
                "source_root": str(skill_root),
            })

        harness = data.get("harness_defaults") or {}
        if data.get("harness") is not None:
            harness = data.get("harness")
        if not isinstance(harness, dict):
            raise ValueError("harness configuration must be an object")
        mcp_servers = data.get("mcp_servers") or {}
        if not isinstance(mcp_servers, dict):
            raise ValueError("mcp_servers must be an object")
        normalized_mcp = []
        for server_id, binding in sorted(mcp_servers.items()):
            self._safe_slug(str(server_id), "MCP server_id")
            if not isinstance(binding, dict):
                raise ValueError("each MCP server binding must be an object")
            enabled_tools = binding.get("enabled_tools") or []
            if not isinstance(enabled_tools, list):
                raise ValueError("MCP enabled_tools must be a list")
            normalized_mcp.append({
                "server_id": str(server_id),
                **{str(key): value for key, value in binding.items()},
                "enabled_tools": [str(item) for item in enabled_tools],
            })
        entrypoints = data.get("entrypoints") or {}
        if not isinstance(entrypoints, dict):
            raise ValueError("entrypoints must be an object")
        agent_id = str(data.get("agent_id") or (
            "pm" if agent_key.endswith(":manager") else agent_key.rsplit(":", 1)[-1]
        ))
        aliases = tuple(str(item) for item in (data.get("aliases") or []))
        triggers = tuple(str(item) for item in (data.get("triggers") or []))
        stage = agent_key.rsplit(":", 1)[-1]
        sandbox = str(data.get("sandbox") or harness.get("sandbox") or (
            "workspace-write" if stage == "implementation" else "read-only"
        ))
        context_policy = str(
            data.get("context_policy") or harness.get("context_policy") or "max"
        )
        if context_policy not in {"model_default", "max", "custom"}:
            raise ValueError("context_policy must be model_default, max, or custom")
        digest = hashlib.sha256()
        digest.update(b"agent-config-bundle.v2\0")
        for path in source_files:
            digest.update(str(path.relative_to(root) if root in path.parents else path).encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
        return FileAgentConfig(
            agent_id=agent_id,
            agent_key=str(data.get("agent_key") or data.get("agent_id") or agent_key),
            kind=str(data.get("kind") or ("manager" if stage in {"manager", "system-butler"} else "stage")),
            status=str(data.get("status") or "active"),
            display_name=str(data.get("display_name") or agent_id),
            aliases=aliases,
            role_text=role_text,
            skill_texts=tuple(skill_texts),
            skill_bindings=tuple(skill_bindings),
            provider=data.get("provider") or harness.get("provider"),
            model=data.get("model") or harness.get("model"),
            reasoning_effort=str(data.get("reasoning_effort") or harness.get("reasoning_effort") or "medium"),
            context_policy=context_policy,
            model_context_window_tokens=(
                int(data.get("model_context_window_tokens") or harness.get("model_context_window_tokens"))
                if data.get("model_context_window_tokens") or harness.get("model_context_window_tokens")
                else None
            ),
            model_auto_compact_token_limit=(
                int(data.get("model_auto_compact_token_limit") or harness.get("model_auto_compact_token_limit"))
                if data.get("model_auto_compact_token_limit") or harness.get("model_auto_compact_token_limit")
                else None
            ),
            timeout_seconds=int(data.get("timeout_seconds") or harness.get("timeout_seconds") or self.settings.agent_timeout_seconds),
            sandbox=sandbox,
            config_sha256=digest.hexdigest(),
            source_files=tuple(str(path) for path in source_files),
            allowed_tools=tuple(dict.fromkeys([
                *(str(item) for item in (data.get("tools") or [])),
                *DEFAULT_SHARED_TOOLS,
            ])),
            scope_profile=str(data.get("scope_profile") or "current-project"),
            mcp_servers=tuple(normalized_mcp),
            entrypoints={str(key): bool(value) for key, value in entrypoints.items()},
            triggers=triggers,
            input_schema=str(data["input_schema"]) if data.get("input_schema") else None,
            output_schema=str(data["output_schema"]) if data.get("output_schema") else None,
        )

    def snapshot(self, project_id: str, config: FileAgentConfig) -> tuple[str, str]:
        runtime = self.runtime.ensure(project_id)
        relative = Path("runtime") / "config-snapshots" / f"{config.config_sha256}.json"
        target = runtime.vault_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        projected_bindings = []
        for binding in config.skill_bindings:
            projected_bindings.append({
                **binding,
                "snapshot_uri": (
                    f"vault://{project_id}/runtime/skill-snapshots/"
                    f"{config.config_sha256}/{binding['name']}/"
                ),
            })
        payload = {
            "schema_version": "agent-config-snapshot.v2",
            "agent_id": config.agent_id,
            "agent_key": config.agent_key,
            "kind": config.kind,
            "status": config.status,
            "display_name": config.display_name,
            "aliases": list(config.aliases),
            "role_text": config.role_text,
            "skill_texts": list(config.skill_texts),
            "skill_bindings": projected_bindings,
            "provider": config.provider,
            "model": config.model,
            "reasoning_effort": config.reasoning_effort,
            "context_policy": config.context_policy,
            "model_context_window_tokens": config.model_context_window_tokens,
            "model_auto_compact_token_limit": config.model_auto_compact_token_limit,
            "timeout_seconds": config.timeout_seconds,
            "sandbox": config.sandbox,
            "config_sha256": config.config_sha256,
            "source_files": list(config.source_files),
            "allowed_tools": list(config.allowed_tools),
            "scope_profile": config.scope_profile,
            "mcp_servers": list(config.mcp_servers),
            "entrypoints": config.entrypoints,
            "triggers": list(config.triggers),
            "input_schema": config.input_schema,
            "output_schema": config.output_schema,
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
        if target.exists():
            existing = json.loads(target.read_text(encoding="utf-8"))
            if existing != payload:
                raise RuntimeError("agent snapshot hash collision")
        else:
            temporary = target.with_suffix(".tmp")
            temporary.write_text(encoded, encoding="utf-8")
            temporary.replace(target)
        skill_snapshot_root = (
            runtime.vault_root / "runtime" / "skill-snapshots" / config.config_sha256
        )
        if not skill_snapshot_root.exists():
            temporary_root = skill_snapshot_root.with_name(skill_snapshot_root.name + ".tmp")
            if temporary_root.exists():
                shutil.rmtree(temporary_root)
            temporary_root.mkdir(parents=True, exist_ok=True)
            for binding in config.skill_bindings:
                destination = temporary_root / str(binding["name"])
                source_root = Path(str(binding["source_root"]))
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(source_root, destination)
            temporary_root.replace(skill_snapshot_root)
        file_sha256 = hashlib.sha256(target.read_bytes()).hexdigest()
        return f"vault://{project_id}/{relative.as_posix()}", file_sha256

    def load_snapshot(
        self, project_id: str, uri: str, expected_file_sha256: str,
    ) -> FileAgentConfig:
        prefix = f"vault://{project_id}/"
        if not uri.startswith(prefix):
            raise ValueError("agent snapshot URI belongs to another project")
        path = self.runtime.resolve_vault_file(project_id, uri[len(prefix):])
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected_file_sha256:
            raise ValueError("agent snapshot file hash does not match the pinned run")
        payload = json.loads(path.read_text(encoding="utf-8"))
        source_config_sha256 = str(payload.get("config_sha256") or "")
        if not source_config_sha256:
            raise ValueError("agent snapshot does not contain its source configuration hash")
        return FileAgentConfig(
            agent_id=str(payload.get("agent_id") or str(payload["agent_key"]).rsplit(":", 1)[-1]),
            agent_key=str(payload["agent_key"]),
            kind=str(payload.get("kind") or "business"),
            status=str(payload.get("status") or "active"),
            display_name=str(payload.get("display_name") or payload.get("agent_id") or payload["agent_key"]),
            aliases=tuple(str(item) for item in payload.get("aliases") or []),
            role_text=str(payload["role_text"]),
            skill_texts=tuple(str(item) for item in payload.get("skill_texts") or []),
            skill_bindings=tuple(dict(item) for item in payload.get("skill_bindings") or []),
            provider=payload.get("provider"), model=payload.get("model"),
            reasoning_effort=str(payload["reasoning_effort"]),
            context_policy=str(payload.get("context_policy") or "max"),
            model_context_window_tokens=(
                int(payload["model_context_window_tokens"])
                if payload.get("model_context_window_tokens") is not None else None
            ),
            model_auto_compact_token_limit=(
                int(payload["model_auto_compact_token_limit"])
                if payload.get("model_auto_compact_token_limit") is not None else None
            ),
            timeout_seconds=int(payload["timeout_seconds"]), sandbox=str(payload["sandbox"]),
            config_sha256=source_config_sha256,
            source_files=tuple(str(item) for item in payload.get("source_files") or []),
            allowed_tools=tuple(str(item) for item in payload.get("allowed_tools") or []),
            scope_profile=str(payload.get("scope_profile") or "current-project"),
            mcp_servers=tuple(dict(item) for item in payload.get("mcp_servers") or []),
            entrypoints={str(key): bool(value) for key, value in (payload.get("entrypoints") or {}).items()},
            triggers=tuple(str(item) for item in payload.get("triggers") or []),
            input_schema=str(payload["input_schema"]) if payload.get("input_schema") else None,
            output_schema=str(payload["output_schema"]) if payload.get("output_schema") else None,
        )


class ChannelRouter:
    """Small file-owned replacement for channel binding tables."""

    def __init__(self, settings: KernelSettings):
        self.settings = settings

    def resolve(
        self, channel: str, chat_id: str, user_id: str | None,
        project_id: str | None = None, agent_key: str | None = None,
    ) -> tuple[str, str]:
        if project_id:
            return project_id, agent_key or (
                "system-butler" if project_id == "system" else f"project:{project_id}:manager"
            )
        path = self.settings.data_root / "config" / "channel-bindings.yaml"
        if path.is_file():
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            for item in data.get("bindings") or []:
                if str(item.get("channel")) != channel:
                    continue
                if str(item.get("chat_id")) != chat_id:
                    continue
                expected_user = item.get("user_id")
                if expected_user and str(expected_user) != str(user_id):
                    continue
                selected = str(item["project_id"])
                return selected, str(item.get("agent_key") or (
                    "system-butler" if selected == "system" else f"project:{selected}:manager"
                ))
        return "system", "system-butler"


class RouteResolver:
    """Resolve one conversation route consistently across all intake adapters."""

    def __init__(self, settings: KernelSettings, repository: "CoreRepository"):
        self.fallback = ChannelRouter(settings)
        self.repository = repository

    async def resolve(
        self, channel: str, chat_id: str, user_id: str | None,
        project_id: str | None = None, agent_key: str | None = None,
        reply_to_message_id: str | None = None,
    ) -> tuple[str, str]:
        if reply_to_message_id:
            original = await self.repository.get_message(reply_to_message_id)
            if original is None or str(original.get("direction")) != "outbound":
                raise ValueError("the replied message is not a known outbound message")
            if str(original.get("channel")) != channel or str(original.get("chat_id")) != chat_id:
                raise PermissionError("the replied message belongs to another conversation")
            original_user = original.get("user_id")
            if original_user is not None and str(original_user) != str(user_id):
                raise PermissionError("the replied message belongs to another user")
            selected_project = str(original["project_id"])
            related_run = (
                await self.repository.get_run(str(original["run_id"]))
                if original.get("run_id") else None
            )
            selected_agent = str((related_run or {}).get("agent_key") or (
                "system-butler" if selected_project == "system"
                else f"project:{selected_project}:manager"
            ))
            if project_id is not None and project_id != selected_project:
                raise ValueError("explicit Project conflicts with the replied message")
            if agent_key is not None and agent_key != selected_agent:
                raise ValueError("explicit Agent conflicts with the replied message")
            return selected_project, selected_agent
        if project_id:
            return self.fallback.resolve(
                channel, chat_id, user_id, project_id, agent_key,
            )
        durable = await self.repository.resolve_route_binding(channel, chat_id, user_id)
        if durable is not None:
            return durable
        return self.fallback.resolve(channel, chat_id, user_id)
