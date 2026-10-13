from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from assistant_kernel.config import KernelSettings
from assistant_kernel.repositories import CoreRepository, approval_decision
from assistant_kernel.services.source_scope import SourceScopeService
from assistant_kernel.services.project_credentials import ProjectCredentialsStore
from assistant_kernel.services.semantic_map import SemanticMapService
from assistant_kernel.services.runtime_catalog import RuntimeCatalogService
from assistant_kernel.services.workspace_scope import DEFAULT_CONTEXT_SOFT_EXCLUDES
from assistant_kernel.services.agent_files import unbound_route
from assistant_kernel.services.kernel_management import MANAGEMENT_PROJECT
from assistant_kernel.storage import ProjectRuntimeManager
from assistant_kernel.storage.project_runtime import PROJECT_ID_PATTERN


BASE_TEMPLATE_VERSION = "five-stage.v1"
PHASE_NAMES = {
    "requirements": "requirements", "design": "design", "build": "implementation",
    "audit": "review", "operate": "operation",
}
COMMON_PM_SKILLS = (
    "assistant-task-continuity", "assistant-project-agent", "assistant-source-scope",
)
PM_READ_TOOLS = (
    "get_authorized_workspace", "list_authorized_tree", "read_authorized_file",
    "current_conversation_route", "list_recent_history", "run_status",
    "get_project_map", "query_project_map", "read_map_component",
    "list_project_changes", "read_change_diff", "refresh_project_map",
    "list_source_discovery_roots", "inspect_source_path",
    "project_task_status", "queue_project_task", "request_operation_report",
    "list_project_credentials",
)
PM_BUILD_TOOLS = (
    *PM_READ_TOOLS, "get_project_credential", "request_project_credential",
    "delete_project_credential", "write_candidate_file", "delete_candidate_file",
)
OPTIONAL_PM_SKILLS = (
    "assistant-history-retrieval", "assistant-subagent-orchestration",
    "assistant-project-credentials", "assistant-agent-design", "assistant-mcp-design",
    "assistant-workflow-design",
    "assistant-capability-maintenance", "assistant-volcengine-models",
)


def default_phase_profiles() -> dict[str, dict[str, Any]]:
    """Five published profiles; legacy repair resolves to the build profile."""
    phase_skills = {
        "requirements": ["assistant-requirements"],
        "design": ["assistant-solution-design"],
        "build": ["assistant-implementation", "assistant-subagent-orchestration"],
        "audit": ["assistant-release-audit", "assistant-subagent-orchestration"],
        "operate": ["assistant-operation"],
    }
    descriptions = {
        "requirements": "计划分析需求、调查和澄清；关键问题已有答案或获批处置后提交完整需求批准。批准后进入 design。不得实施或激活。",
        "design": "计划分析真实环境与落地方案；给出最可行方案和实施、测试、运营计划并申请设计批准。需求实质变化先回 requirements，保存已有设计进展。不得实施或激活。",
        "build": "在已批准需求和设计范围内实施 candidate、Skill/MCP/业务图定义，自测并汇总报告。涉及定时时同时制作固定 ScheduleDefinition、时间预览及登记证据，正式登记启用由 Operate 完成。修复也属于此阶段；超出范围先提问或回到受影响分析阶段。",
        "audit": "在新的只读物理 Session 对照有效需求、设计、candidate 和测试逐项审查；产出一致性结论及能力改进提案。不得修改实现或自行发布。",
        "operate": "评估已发布运营基线、目标效果和反馈，执行已批准且本阶段允许的运营动作，形成定期报告。普通事件排队，严重事件请求安全边界中断；代码改动转入获批实施范围。",
    }
    schemas = {
        "requirements": "task-contract.v1", "design": "solution-design.v1",
        "build": "implementation-report.v1", "audit": "review-report.v1",
        "operate": "operation-report.v1",
    }
    profiles = {}
    for mode, display in PHASE_NAMES.items():
        required = list(dict.fromkeys([*COMMON_PM_SKILLS, *phase_skills[mode]]))
        profiles[mode] = {
            "schema_version": "project-agent-profile.v2", "mode_id": mode,
            "display_phase": display,
            "role_append": f"# Active phase · {display}\n\n{descriptions[mode]}",
            "skills": {
                "required": [{"name": name, "source": "auto"} for name in required],
                "available": [{"name": name, "source": "auto"}
                              for name in OPTIONAL_PM_SKILLS if name not in required],
            },
            "mcp_servers": {},
            "tools": list(PM_BUILD_TOOLS if mode == "build" else
                          (*PM_READ_TOOLS, "run_project_action") if mode == "operate" else PM_READ_TOOLS),
            "disabled_tools": [] if mode == "build" else ["write_candidate_file", "delete_candidate_file"],
            "artifact_schema": schemas[mode],
        }
    return profiles


def default_work_modes() -> dict[str, dict[str, Any]]:
    return {
        mode: {
            "mode_id": mode, "display_phase": display, "profile": mode,
            "context_profile": mode,
            "permission_profile": "candidate-write" if mode == "build" else "read-only",
            "validation_policy": {"required_scopes": ["structure", "component"]},
        } for mode, display in PHASE_NAMES.items()
    }


def default_pm_config(project_id: str) -> dict[str, Any]:
    profiles = default_phase_profiles()
    all_skills = {
        item["name"]: dict(item)
        for profile in profiles.values()
        for group in ("required", "available")
        for item in profile["skills"][group]
    }
    return {
        "schema_version": "agent-config.v2", "agent_id": "pm",
        "agent_key": f"project:{project_id}:manager", "kind": "manager", "status": "active",
        "display_name": "Project PM", "aliases": ["manager"], "role": "role.md",
        "scope_profile": "current-project",
        "harness": {"provider": "openai", "context_policy": "max", "reasoning_effort": "medium",
                    "timeout_seconds": 600, "sandbox": "project-scope"},
        "skills": {
            "required": [{"name": name, "source": "auto"} for name in COMMON_PM_SKILLS],
            "available": list(all_skills.values()),
        },
        "mcp_servers": {}, "entrypoints": {"pm_handoff": True, "feishu_slash": True},
        "triggers": ["user_message", "handoff"],
        "tools": list(dict.fromkeys(tool for profile in profiles.values() for tool in profile["tools"])),
    }


class ProjectDirectoryService:
    """File-owned Project registry and idempotent Project bootstrap."""

    def __init__(self, settings: KernelSettings, repository: CoreRepository):
        self.settings = settings
        self.repository = repository
        self.runtime = ProjectRuntimeManager(settings.data_root)
        self.directory_path = settings.data_root / "config" / "project-directory.yaml"

    def _entries(self) -> list[dict[str, Any]]:
        if not self.directory_path.is_file():
            return []
        data = yaml.safe_load(self.directory_path.read_text(encoding="utf-8")) or {}
        return [dict(item) for item in data.get("projects") or []]

    @staticmethod
    def _text_list(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item).strip() for item in value if str(item).strip()]

    def _project_card(self, entry: dict[str, Any]) -> dict[str, Any]:
        project_id = str(entry.get("project_id") or "")
        project_path = self.runtime.describe(project_id).vault_root / "project.yaml"
        project: dict[str, Any] = {}
        if project_path.is_file():
            loaded = yaml.safe_load(project_path.read_text(encoding="utf-8")) or {}
            if not isinstance(loaded, dict):
                raise ValueError(f"project.yaml must be an object: {project_id}")
            if loaded.get("project_id") and str(loaded["project_id"]) != project_id:
                raise ValueError(f"project.yaml belongs to another Project: {project_id}")
            project = loaded
        routing = project.get("routing") or entry.get("routing") or {}
        if not isinstance(routing, dict):
            raise ValueError(f"Project routing profile must be an object: {project_id}")
        purpose = str(project.get("purpose") or entry.get("purpose") or "").strip()
        card = {
            "schema_version": "project-card.v1",
            "project_id": project_id,
            "display_name": str(
                project.get("display_name") or entry.get("display_name") or project_id
            ).strip(),
            "purpose": purpose,
            "status": str(entry.get("status") or project.get("status") or "active"),
            "default_agent": str(project.get("default_agent") or "pm"),
            "initial_goal": str(
                project.get("initial_goal") or entry.get("initial_goal") or ""
            ).strip(),
            "boundaries": self._text_list(
                project.get("boundaries") or entry.get("boundaries") or []
            ),
            "non_goals": self._text_list(
                project.get("non_goals") or entry.get("non_goals") or []
            ),
            "proposal_uri": str(entry.get("proposal_uri") or "") or None,
            "proposal_sha256": str(entry.get("proposal_sha256") or "") or None,
            "routing": {
                "aliases": self._text_list(routing.get("aliases") or []),
                "topics": self._text_list(routing.get("topics") or []),
                "typical_requests": self._text_list(routing.get("typical_requests") or []),
                "exclusions": self._text_list(routing.get("exclusions") or []),
                "related_projects": self._text_list(routing.get("related_projects") or []),
            },
        }
        card["routing_hash"] = hashlib.sha256(
            json.dumps(card, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        return card

    def list(self) -> list[dict[str, Any]]:
        return [
            self._project_card(item)
            for item in self._entries() if item.get("status") == "active" and item.get("project_id") not in {"system", "butler-maintenance"}
        ]

    def propose(
        self, run_id: str, project_id: str, purpose: str, *,
        initial_goal: str = "", boundaries: list[str] | None = None,
        non_goals: list[str] | None = None,
        workspace_roots: list[dict[str, Any]] | None = None,
        source_project_id: str = "system",
        assessment_parameters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        raise PermissionError("Butler project creation is retired; future Project Skills will own initialization")

    async def create_from_approved_run(
        self, *, run_id: str, worker_instance_id: str, claim_token: str,
        proposal_uri: str, expected_sha256: str,
    ) -> dict[str, Any]:
        raise PermissionError("Butler project creation is retired; future Project Skills will own initialization")

    async def create_and_handoff_from_approved_run(
        self, *, run_id: str, worker_instance_id: str, claim_token: str,
        proposal_uri: str, expected_sha256: str, handoff_summary: str = "",
    ) -> dict[str, Any]:
        raise PermissionError("Butler project creation is retired; future Project Skills will own initialization")

    async def handoff_to_existing_project_from_claim(
        self, *, run_id: str, worker_instance_id: str, claim_token: str,
        target_project_id: str, handoff_summary: str,
    ) -> dict[str, Any]:
        """Ensure an existing Project's Discord channel, then hand off its PM Run."""

        cards = {
            str(item["project_id"]): item for item in self.list()
            if str(item.get("status") or "") == "active"
        }
        card = cards.get(target_project_id)
        if card is None:
            raise ValueError("target Project is not active in the Project directory")
        source = await self.repository.claimed_run_source(
            run_id=run_id,
            worker_instance_id=worker_instance_id,
            claim_token=claim_token,
        )
        target_channel: str | None = None
        target_chat_id: str | None = None
        provisioning: dict[str, Any] | None = None
        if str(source["channel"]) == "discord":
            from assistant_kernel.services.discord import load_discord_config
            from assistant_kernel.services.discord_projects import (
                DiscordProjectProvisioner,
            )

            discord_config = load_discord_config(self.settings)
            if discord_config.get("auto_provision_projects"):
                discord_metadata = dict(
                    (dict(source.get("content_json") or {}).get("discord") or {})
                )
                guild_id = str(discord_metadata.get("guild_id") or "")
                if not guild_id:
                    raise ValueError(
                        "Discord Project routing requires a Guild conversation"
                    )
                discord_provisioner = DiscordProjectProvisioner(
                    self.repository, discord_config
                )
                provisioning = await discord_provisioner.provision(
                    project_id=target_project_id,
                    display_name=str(card.get("display_name") or target_project_id),
                    purpose=str(card.get("purpose") or target_project_id),
                    guild_id=guild_id,
                    bound_by_user_id=str(source.get("user_id") or "system"),
                    source_message_id=str(source["message_id"]),
                    project_routing_hash=str(card.get("routing_hash") or ""),
                )
                provisioning["interfaces"] = (
                    await discord_provisioner.provision_supervisor_interface(
                        project_id=target_project_id, guild_id=guild_id,
                    )
                )
                target_channel = "discord"
                target_chat_id = str(provisioning["channel_id"])

        handoff = await self.repository.handoff_from_claim(
            source_run_id=run_id,
            worker_instance_id=worker_instance_id,
            claim_token=claim_token,
            target_project_id=target_project_id,
            handoff_summary=handoff_summary,
            target_channel=target_channel,
            target_chat_id=target_chat_id,
        )
        receipt = None
        if provisioning is not None and target_chat_id is not None:
            receipt = await self.repository.create_outbound(
                project_id=str(source["project_id"]),
                session_id=None,
                run_id=run_id,
                channel="discord",
                chat_id=str(source["chat_id"]),
                user_id=str(source.get("user_id") or "") or None,
                message_type="reply",
                content={
                    "text": (
                        f"Project `{target_project_id}` 已存在；任务已投递到 "
                        f"<#{target_chat_id}> 的 PM。"
                    ),
                    "origin": "kernel", "context_visibility": "ui_only",
                },
                reply_to_message_id=str(source["message_id"]),
                idempotency_key=(
                    f"discord-existing-project-handoff-receipt:{run_id}:"
                    f"{target_project_id}:{target_chat_id}"
                ),
            )
        return {
            **handoff,
            "discord_provisioning": provisioning,
            "source_receipt": receipt,
        }

    def seed_base_templates(self) -> Path:
        """Seed one fixed Project template version without replacing it.

        Every new Project copies this version into its own Vault. Project-local
        upgrades therefore do not modify other Projects or the public baseline.
        Explicit Project initialization may seed this template.
        """
        root = self.runtime.describe(MANAGEMENT_PROJECT).vault_root / "templates" / "project-base" / BASE_TEMPLATE_VERSION
        manifest = root / "template-manifest.json"
        if manifest.is_file():
            self._read_template_manifest(root)
            return root
        prompts = Path(__file__).parents[1] / "prompts"
        profiles = default_phase_profiles()
        names = sorted({
            item["name"] for profile in profiles.values()
            for group in ("required", "available") for item in profile["skills"][group]
        })
        self._write_once(root / "project-agent.md", (prompts / "roles" / "project-agent.md").read_text(encoding="utf-8"))
        files = ["project-agent.md"]
        for name in names:
            source_root = prompts / "skills" / name
            if not (source_root / "SKILL.md").is_file():
                raise FileNotFoundError(f"base template Skill is missing: {name}")
            for source in sorted(source_root.rglob("*")):
                if not source.is_file() or any(part in {"__pycache__", ".git", ".pytest_cache"}
                                             for part in source.relative_to(source_root).parts):
                    continue
                relative = Path("skills") / name / source.relative_to(source_root)
                self._write_once(root / relative, source.read_text(encoding="utf-8"))
                files.append(relative.as_posix())
        self._write_once(manifest, json.dumps({
            "schema_version": "project-base-template.v1", "version": BASE_TEMPLATE_VERSION,
            "owner_project_id": MANAGEMENT_PROJECT, "profiles": profiles,
            "pm_config": default_pm_config("{project_id}"),
            "files": {relative: hashlib.sha256((root / relative).read_bytes()).hexdigest()
                      for relative in files},
        }, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
        self._read_template_manifest(root)
        return root

    def _read_template_manifest(self, root: Path) -> dict[str, Any]:
        manifest = json.loads((root / "template-manifest.json").read_text(encoding="utf-8"))
        if manifest.get("schema_version") != "project-base-template.v1" or manifest.get("version") != BASE_TEMPLATE_VERSION:
            raise ValueError("unsupported Project base template version")
        for relative, expected in manifest.get("files", {}).items():
            path = self.runtime.resolve_below(root, relative)
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError(f"Project base template hash changed: {relative}")
        return manifest

    def _bootstrap(
        self, project_id: str, purpose: str, *, proposal: dict[str, Any],
        proposal_uri: str, proposal_sha256: str,
    ) -> None:
        described = self.runtime.describe(project_id)
        marker_path = described.vault_root / "project-bootstrap.json"
        if described.vault_root.exists():
            if not marker_path.is_file():
                raise ValueError("Project Vault already exists without a matching bootstrap marker")
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            if marker.get("proposal_sha256") != proposal_sha256:
                raise ValueError("Project Vault belongs to a different proposal")
        runtime = self.runtime.ensure(project_id)
        root = runtime.vault_root
        ProjectCredentialsStore(self.settings.data_root).ensure(project_id)
        self._write_once(
            root / "project.yaml",
            yaml.safe_dump({
                "schema_version": "project.v2",
                "project_id": project_id,
                "layout_version": 2,
                "base_pack": {
                    "version": "assistant-base-pack.v1",
                    "semantic_map_schema": "1.0",
                },
                "task_protocol": "single-agent.v2",
                "single_agent": {
                    "audit_required": True,
                    "design_required": True,
                    "max_task_turns": 12,
                    "validation_policy": {"required_scopes": ["structure", "component"]},
                },
                "supervisor": {
                    "enabled": True, "provider": "openai", "model": "gpt-5.6-sol",
                    "models": {
                        "intent_panel": {
                            "model": "gpt-5.6-luna", "reasoning_effort": "low",
                            "timeout_seconds": 90,
                        },
                        "main_evaluation": {
                            "model": "gpt-5.6-sol", "reasoning_effort": "medium",
                            "timeout_seconds": 180,
                        },
                        "critical_evaluation": {
                            "model": "gpt-5.6-sol", "reasoning_effort": "high",
                            "timeout_seconds": 240,
                        },
                    },
                    "transport": "codex_cli",
                    "timeout_seconds": 90,
                    "control_mode": "advisory",
                    "daily_input_tokens": 200_000,
                    "daily_output_tokens": 40_000,
                    "max_task_turns": 24,
                },
                "multi_agent": {
                    "enabled": True,
                    "max_concurrent_subagents": 3,
                },
                "work_modes": default_work_modes(),
                "phase_protocol": "five-stage.v1",
                "template_source": f"vault://{MANAGEMENT_PROJECT}/templates/project-base/{BASE_TEMPLATE_VERSION}/",
                "network": {"enabled": True, "unrestricted": True},
                "external_actions": {"approval_required": False},
                "status": "active",
                "display_name": project_id,
                "purpose": purpose,
                "initial_goal": str(proposal.get("initial_goal") or ""),
                "boundaries": list(proposal.get("boundaries") or []),
                "non_goals": list(proposal.get("non_goals") or []),
                "routing": {
                    "aliases": [],
                    "topics": [],
                    "typical_requests": [
                        str(proposal.get("initial_goal") or "").strip()
                    ] if str(proposal.get("initial_goal") or "").strip() else [],
                    "exclusions": list(proposal.get("non_goals") or []),
                    "related_projects": [],
                },
                "default_agent": "pm",
            }, sort_keys=False, allow_unicode=True),
        )
        self._write_once(
            root / "source-map.yaml",
            yaml.safe_dump({
                "schema_version": "project-source-map.v1",
                "roots": {
                    str(item["root_key"]): {
                        "mode": "live",
                        "purpose": str(item.get("purpose") or item["root_key"]),
                        "entrypoints": [],
                        "soft_excludes": list(DEFAULT_CONTEXT_SOFT_EXCLUDES),
                    }
                    for item in (proposal.get("workspace_roots") or [])
                },
            }, sort_keys=False, allow_unicode=True),
        )
        self._write_once(
            root / "project-bootstrap.json",
            json.dumps({
                "schema_version": "project-bootstrap.v1", "project_id": project_id,
                "proposal_uri": proposal_uri, "proposal_sha256": proposal_sha256,
                "source_run_id": proposal.get("source_run_id"),
                "created_at": datetime.now(timezone.utc).isoformat(),
            }, ensure_ascii=False, indent=2),
        )
        template_root = self.seed_base_templates()
        templates = json.loads((template_root / "template-manifest.json").read_text(encoding="utf-8"))
        role_root = Path(__file__).parents[1] / "prompts" / "roles"
        manager_role_source = template_root / "project-agent.md"
        manager_config = dict(templates["pm_config"])
        manager_config["agent_key"] = f"project:{project_id}:manager"
        self._write_once(
            root / "agents" / "pm" / "config.yaml",
            yaml.safe_dump(manager_config, sort_keys=False, allow_unicode=True),
        )
        self._write_once(
            root / "agents" / "pm" / "role.md",
            manager_role_source.read_text(encoding="utf-8")
            .replace("{project_id}", project_id)
            .replace("{project_purpose}", purpose),
        )
        self._write_once(
            root / "agents" / "pm" / "MAP.md",
            (
                "---\nschema_version: agent-map.v1\nagent_id: pm\n"
                "owner: kernel-base-pack\nstate_authority: kernel\n"
                "context_profile: work-mode-bundle\n---\n\n"
                "# Project Agent Map\n\n"
                "Owns one Project foreground conversation across requirements, design, "
                "implementation, review and operation. Repair is implementation work. Kernel owns accepted document refs, approval, "
                "Validation and publication. Read the Project Semantic Map before broad "
                "source discovery.\n"
            ),
        )
        for profile_name, profile_payload in templates["profiles"].items():
            self._write_once(
                root / "agents" / "pm" / "profiles" / f"{profile_name}.yaml",
                yaml.safe_dump(profile_payload, sort_keys=False, allow_unicode=True),
            )
        supervisor_config = {
            "schema_version": "agent-config.v2", "agent_id": "supervisor",
            "agent_key": f"project:{project_id}:supervisor", "kind": "supervisor",
            "status": "active", "display_name": "Goal Supervisor", "aliases": [],
            "role": "role.md", "scope_profile": "current-project",
            "harness": {"provider": "openai", "model": "gpt-5.6-sol",
                        "reasoning_effort": "medium", "context_policy": "max",
                        "timeout_seconds": 90, "sandbox": "project-scope"},
            "skills": [{"name": "assistant-goal-supervisor", "source": "kernel"}],
            "mcp_servers": {},
            "entrypoints": {"pm_handoff": False, "feishu_slash": False},
            "triggers": ["after_agent_turn", "user_consultation"],
            "tools": [],
        }
        self._write_once(
            root / "agents" / "supervisor" / "config.yaml",
            yaml.safe_dump(supervisor_config, sort_keys=False, allow_unicode=True),
        )
        self._write_once(
            root / "agents" / "supervisor" / "role.md",
            (role_root / "goal-supervisor.md").read_text(encoding="utf-8"),
        )
        self._write_once(
            root / "agents" / "supervisor" / "MAP.md",
            (
                "---\nschema_version: agent-map.v1\nagent_id: supervisor\n"
                "owner: kernel-base-pack\nstate_authority: advisory-only\n"
                "context_profile: intent-and-action-plan\n---\n\n"
                "# Goal Supervisor Map\n\n"
                "Reads the accepted Turn Record separately from the current Task head; "
                "proposes an Action Plan only when its context epoch is still current. "
                "Each PM Turn is evaluated. A new PM Turn, a new "
                "Supervisor user message, a card action, Task reset, or Task completion "
                "supersedes the open card without a time-based TTL. Adopt, dismiss, and "
                "revise confirm or dispose the plan; none grants approval, trading, permission, "
                "or publication authority.\n"
            ),
        )
        for relative in templates["files"]:
            if not relative.startswith("skills/"):
                continue
            source = template_root / relative
            target = root / ".agents" / relative
            self._write_once(target, source.read_text(encoding="utf-8"))
        current = {"schema_version": "project-directory.v1", "projects": self._entries()}
        projects = [item for item in current["projects"] if item.get("project_id") != project_id]
        projects.append({
            "project_id": project_id, "purpose": purpose, "status": "active",
            "initial_goal": str(proposal.get("initial_goal") or ""),
            "boundaries": list(proposal.get("boundaries") or []),
            "non_goals": list(proposal.get("non_goals") or []),
            "proposal_uri": proposal_uri, "proposal_sha256": proposal_sha256,
        })
        current["projects"] = sorted(projects, key=lambda item: str(item["project_id"]))
        temporary = self.directory_path.with_suffix(".tmp")
        temporary.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(yaml.safe_dump(current, allow_unicode=True, sort_keys=False), encoding="utf-8")
        temporary.replace(self.directory_path)
        SemanticMapService(self.settings).bootstrap(
            project_id, purpose=purpose, base_pack_version="assistant-base-pack.v1",
        )
        RuntimeCatalogService(self.settings).refresh()

    @staticmethod
    def _write_once(path: Path, content: str) -> None:
        if path.exists():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
