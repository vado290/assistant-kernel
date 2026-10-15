"""Load a project's live PM / Supervisor agent files (layout v2) for one model call.

Each turn reads the current files (plan D2: no pinned package versions). A phase profile can only
narrow what the agent's config.yaml declares: it selects skills from the configured ones and a subset
of the configured tools.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from assistant_kernel.storage import ProjectRuntimeManager

KERNEL_SKILLS = Path(__file__).parents[1] / "prompts" / "skills"
PHASES = {"requirements", "design", "build", "audit", "operate"}
PHASE_ALIASES = {"implementation": "build", "review": "audit", "operation": "operate", "repair": "build"}
CANDIDATE_TOOLS = {"write_candidate_file", "delete_candidate_file"}
DEFAULT_ROLE = ("You are the assistant for this Project. Follow the user's request, preserve facts, "
                "and report only work that was actually completed.")


@dataclass(frozen=True)
class AgentConfig:
    agent_id: str
    agent_key: str
    kind: str
    role_text: str
    skill_texts: tuple[str, ...]
    provider: str | None
    model: str | None
    reasoning_effort: str
    context_policy: str
    model_context_window_tokens: int | None
    model_auto_compact_token_limit: int | None
    timeout_seconds: int
    allowed_tools: tuple[str, ...]
    config_sha256: str
    # What config.yaml declares; a phase profile selects within these.
    skills: dict[str, str]
    base_files: tuple[str, ...]  # config.yaml and role file; skill files are added per phase


def _yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"YAML document must be an object: {path}")
    return data


def _slug(value: str, label: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value):
        raise ValueError(f"{label} must be a safe slug")
    return value


def _skill_spec(item: Any) -> tuple[str, str]:
    name = str((item.get("name") or item.get("skill")) if isinstance(item, dict) else item).strip()
    source = str(item.get("source") or "auto") if isinstance(item, dict) else "auto"
    if source not in {"project", "kernel", "auto"}:
        raise ValueError(f"unsupported Skill source: {source}")
    return _slug(name, "Skill name"), source


def _optional_int(*values: Any) -> int | None:
    value = next((v for v in values if v), None)
    return int(value) if value else None


class AgentLoader:
    def __init__(self, settings):
        self.settings = settings
        self.runtime = ProjectRuntimeManager(settings.data_root)

    def _skill_path(self, vault: Path, name: str, source: str) -> Path:
        project = self.runtime.resolve_below(vault, f".agents/skills/{name}/SKILL.md")
        legacy = self.runtime.resolve_below(vault, f"capabilities/skills/{name}/SKILL.md")
        kernel = KERNEL_SKILLS / name / "SKILL.md"
        candidates = {"project": (project,), "kernel": (kernel,)}.get(source, (project, legacy, kernel))
        path = next((c for c in candidates if c.is_file()), None)
        if path is None:
            raise FileNotFoundError(f"required Skill is missing: {name}")
        return path

    def _load_skills(self, vault: Path, specs: list[tuple[str, str]]) -> tuple[list[str], list[Path]]:
        texts, files, seen = [], [], set()
        for name, source in specs:
            if name in seen:
                continue
            seen.add(name)
            path = self._skill_path(vault, name, source)
            texts.append(path.read_text(encoding="utf-8"))
            files.extend(p for p in sorted(path.parent.rglob("*")) if p.is_file() and not any(
                part in {"__pycache__", ".pytest_cache", ".git"} for part in p.relative_to(path.parent).parts))
        return texts, files

    def _digest(self, vault: Path, files: list[Path], extra: bytes = b"") -> str:
        digest = hashlib.sha256(b"agent-config-bundle.v2\0")
        for path in files:
            digest.update(str(path.relative_to(vault) if vault in path.parents else path).encode() + b"\0")
            digest.update(path.read_bytes() + b"\0")
        digest.update(extra)
        return digest.hexdigest()

    def _agent_dir(self, project_id: str, vault: Path, agent_key: str) -> Path:
        project = vault / "project.yaml"
        if not project.is_file() or int(_yaml(project).get("layout_version") or 0) != 2:
            raise ValueError(f"project {project_id} must use agent layout_version 2")
        matches = [path.parent for path in sorted((vault / "agents").glob("*/config.yaml"))
                   if str(_yaml(path).get("agent_key") or f"project:{project_id}:{path.parent.name}") == agent_key]
        if len(matches) != 1:
            raise FileNotFoundError(f"published Agent configuration is missing: {agent_key}")
        return matches[0]

    def load(self, project_id: str, agent_key: str) -> AgentConfig:
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
        policy = data.get("skills") or []
        required = list(policy.get("required") or []) if isinstance(policy, dict) else list(policy)
        available = list(policy.get("available") or []) if isinstance(policy, dict) else []
        skills = dict(_skill_spec(item) for item in [*available, *required])
        texts, skill_files = self._load_skills(vault, [_skill_spec(item) for item in required])
        harness = data.get("harness") if data.get("harness") is not None else data.get("harness_defaults") or {}
        if not isinstance(harness, dict):
            raise ValueError("harness configuration must be an object")
        setting = lambda key: data.get(key) or harness.get(key)  # noqa: E731
        context_policy = str(setting("context_policy") or "max")
        if context_policy not in {"model_default", "max", "custom"}:
            raise ValueError("context_policy must be model_default, max, or custom")
        agent_id = str(data.get("agent_id") or agent_dir.name)
        return AgentConfig(
            agent_id=agent_id, agent_key=str(data.get("agent_key") or agent_key),
            kind=str(data.get("kind") or "manager"), role_text=role_text, skill_texts=tuple(texts),
            provider=setting("provider"), model=setting("model"),
            reasoning_effort=str(setting("reasoning_effort") or "medium"), context_policy=context_policy,
            model_context_window_tokens=_optional_int(setting("model_context_window_tokens")),
            model_auto_compact_token_limit=_optional_int(setting("model_auto_compact_token_limit")),
            timeout_seconds=int(setting("timeout_seconds") or self.settings.agent_timeout_seconds),
            allowed_tools=tuple(dict.fromkeys(str(t) for t in data.get("tools") or [])),
            config_sha256=self._digest(vault, files + skill_files), skills=skills,
            base_files=tuple(str(p) for p in files))

    def apply_phase_profile(self, project_id: str, config: AgentConfig, profile: dict[str, Any]) -> AgentConfig:
        """Narrow the agent to one phase: role appendix, required skills and tools (never more than configured)."""
        mode = PHASE_ALIASES.get(str(profile.get("mode_id") or "").strip(), str(profile.get("mode_id") or "").strip())
        if mode not in PHASES:
            raise ValueError("unknown Project phase profile")
        vault = self.runtime.ensure(project_id).vault_root
        append = str(profile.get("role_append") or "").strip()
        role = config.role_text.rstrip() + ("\n\n" + append if append else "")
        texts, skill_files = list(config.skill_texts), None
        phase_skills = profile.get("skills")
        if phase_skills is not None:
            if not isinstance(phase_skills, dict):
                raise ValueError("phase Skills must declare required and available lists")
            specs = []
            for item in phase_skills.get("required") or []:
                name, source = _skill_spec(item)
                if name not in config.skills:
                    raise PermissionError(f"phase Skill is not configured for this Agent: {name}")
                configured = config.skills[name]
                specs.append((name, configured if configured != "auto" else source))
            texts, skill_files = self._load_skills(vault, specs)
        tools = profile.get("tools", list(config.allowed_tools))
        if not isinstance(tools, list) or any(not isinstance(item, str) for item in tools):
            raise ValueError("phase tools must be a list of tool names")
        disabled = set(profile.get("disabled_tools") or [])
        allowed = tuple(t for t in dict.fromkeys(tools) if t in config.allowed_tools and t not in disabled)
        if mode != "build":
            allowed = tuple(t for t in allowed if t not in CANDIDATE_TOOLS)
        profile_bytes = yaml.safe_dump(profile, sort_keys=True).encode()
        digest = (self._digest(vault, [Path(p) for p in config.base_files] + skill_files, profile_bytes)
                  if skill_files is not None else hashlib.sha256(config.config_sha256.encode() + profile_bytes).hexdigest())
        return AgentConfig(**{**config.__dict__, "role_text": role, "skill_texts": tuple(texts),
                              "allowed_tools": allowed, "config_sha256": digest})
