"""Load a project's live PM / Supervisor agent files (layout v2) for one model call.

Each turn reads the current files (plan D2: no pinned package versions). config.yaml gives the model
settings and the tool list, role.md the project's notes for the role. Skills are chosen separately by
their own load rules (core/skills.py); executors add them to the prompt.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from assistant_kernel.storage import ProjectRuntimeManager

DEFAULT_ROLE = ("You are the assistant for this Project. Follow the user's request, preserve facts, "
                "and report only work that was actually completed.")


@dataclass(frozen=True)
class AgentConfig:
    agent_id: str
    agent_key: str
    kind: str
    role_text: str
    provider: str | None
    model: str | None
    reasoning_effort: str
    context_policy: str
    model_context_window_tokens: int | None
    model_auto_compact_token_limit: int | None
    timeout_seconds: int
    allowed_tools: tuple[str, ...]
    config_sha256: str
    skills_text: str = ""                  # rendered skills, on-demand index and shared context
    skill_labels: tuple[str, ...] = ()     # name@version of everything loaded, recorded on the run


def _yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"YAML document must be an object: {path}")
    return data


def _optional_int(*values: Any) -> int | None:
    value = next((v for v in values if v), None)
    return int(value) if value else None


class AgentLoader:
    def __init__(self, settings):
        self.settings = settings
        self.runtime = ProjectRuntimeManager(settings.data_root)

    def _agent_dir(self, project_id: str, vault: Path, agent_key: str) -> Path:
        project = vault / "project.yaml"
        if not project.is_file() or int(_yaml(project).get("layout_version") or 0) != 2:
            raise ValueError(f"project {project_id} must use agent layout_version 2")
        matches = [path.parent for path in sorted((vault / "agents").glob("*/config.yaml"))
                   if str(_yaml(path).get("agent_key") or f"project:{project_id}:{path.parent.name}") == agent_key]
        if len(matches) != 1:
            raise FileNotFoundError(f"published Agent configuration is missing: {agent_key}")
        return matches[0]

    def load(self, project_id: str, agent_key: str) -> tuple[AgentConfig, list[Path]]:
        """The agent's settings and role, plus the files they came from (for the config digest)."""
        vault = self.runtime.ensure(project_id).vault_root
        agent_dir = self._agent_dir(project_id, vault, agent_key)
        config_path = agent_dir / "config.yaml"
        data = _yaml(config_path)
        if str(data.get("schema_version") or "") != "agent-config.v2":
            raise ValueError(f"v2 Agent must use agent-config.v2: {config_path}")
        if data.get("status") in {"archived", "disabled"}:
            raise ValueError(f"Agent configuration is archived: {agent_key}")
        files = [config_path]
        role_path = self.runtime.resolve_below(agent_dir, str(data.get("role") or "role.md"))
        role_text = role_path.read_text(encoding="utf-8") if role_path.is_file() else DEFAULT_ROLE
        if role_path.is_file():
            files.append(role_path)
        harness = data.get("harness") if data.get("harness") is not None else data.get("harness_defaults") or {}
        if not isinstance(harness, dict):
            raise ValueError("harness configuration must be an object")
        setting = lambda key: data.get(key) or harness.get(key)  # noqa: E731
        context_policy = str(setting("context_policy") or "max")
        if context_policy not in {"model_default", "max", "custom"}:
            raise ValueError("context_policy must be model_default, max, or custom")
        config = AgentConfig(
            agent_id=str(data.get("agent_id") or agent_dir.name), agent_key=str(data.get("agent_key") or agent_key),
            kind=str(data.get("kind") or "manager"), role_text=role_text,
            provider=setting("provider"), model=setting("model"),
            reasoning_effort=str(setting("reasoning_effort") or "medium"), context_policy=context_policy,
            model_context_window_tokens=_optional_int(setting("model_context_window_tokens")),
            model_auto_compact_token_limit=_optional_int(setting("model_auto_compact_token_limit")),
            timeout_seconds=int(setting("timeout_seconds") or self.settings.agent_timeout_seconds),
            allowed_tools=tuple(dict.fromkeys(str(t) for t in data.get("tools") or [])),
            config_sha256=hashlib.sha256(b"".join(p.read_bytes() for p in files)).hexdigest())
        return config, files
