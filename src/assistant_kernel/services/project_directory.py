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
from assistant_kernel.storage import ProjectRuntimeManager
from assistant_kernel.storage.project_runtime import PROJECT_ID_PATTERN


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
            for item in self._entries() if item.get("status") == "active"
        ]

    def propose(
        self, run_id: str, project_id: str, purpose: str, *,
        initial_goal: str = "", boundaries: list[str] | None = None,
        non_goals: list[str] | None = None,
        workspace_roots: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if not PROJECT_ID_PATTERN.fullmatch(project_id) or project_id == "system":
            raise ValueError("project_id must be a safe non-system slug")
        if not purpose.strip():
            raise ValueError("project purpose is required")
        root = self.runtime.ensure("system").vault_root
        relative = Path("runs") / run_id / f"project-proposal-{project_id}.yaml"
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        normalized_roots = SourceScopeService(
            self.settings, self.repository
        ).normalize_bindings(list(workspace_roots or []))
        payload = {
            "schema_version": "project-proposal.v4", "source_run_id": run_id,
            "project_id": project_id, "purpose": purpose.strip(),
            "initial_goal": initial_goal.strip(),
            "boundaries": [str(item).strip() for item in (boundaries or []) if str(item).strip()],
            "non_goals": [str(item).strip() for item in (non_goals or []) if str(item).strip()],
            "workspace_roots": normalized_roots,
            "approval_question_id": f"project-create:{project_id}",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        if target.exists():
            existing = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
            comparable = {key: existing.get(key) for key in (
                "schema_version", "source_run_id", "project_id", "purpose", "initial_goal",
                "boundaries", "non_goals", "workspace_roots",
                "approval_question_id",
            )}
            expected = {key: payload.get(key) for key in comparable}
            if comparable != expected:
                raise ValueError("a different Project proposal already exists for this run")
        else:
            if any(str(item.get("project_id")) == project_id for item in self._entries()):
                raise ValueError("Project ID is already registered")
            project_root = self.runtime.describe(project_id).vault_root
            if project_root.exists():
                raise ValueError("Project Vault already exists but is not registered")
            temporary = target.with_suffix(".tmp")
            temporary.write_text(
                yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8"
            )
            temporary.replace(target)
        data = target.read_bytes()
        return {
            "uri": f"vault://system/{relative.as_posix()}",
            "sha256": hashlib.sha256(data).hexdigest(),
            "project_id": project_id, "purpose": purpose.strip(),
            "initial_goal": initial_goal.strip(),
            "workspace_roots": normalized_roots,
            "approval_question_id": payload["approval_question_id"],
        }

    async def create_from_approved_run(
        self, *, run_id: str, worker_instance_id: str, claim_token: str,
        proposal_uri: str, expected_sha256: str,
    ) -> dict[str, Any]:
        prefix = "vault://system/"
        if not proposal_uri.startswith(prefix):
            raise ValueError("proposal_uri must belong to the Butler Vault")
        proposal_path = self.runtime.resolve_vault_file("system", proposal_uri[len(prefix):])
        data = proposal_path.read_bytes()
        if hashlib.sha256(data).hexdigest() != expected_sha256:
            raise ValueError("Project proposal hash does not match")
        proposal = yaml.safe_load(data.decode("utf-8")) or {}
        if proposal.get("source_run_id") != run_id:
            raise ValueError("Project proposal belongs to another Run")
        project_id = str(proposal.get("project_id") or "")
        purpose = str(proposal.get("purpose") or "").strip()
        approval_question_id = str(
            proposal.get("approval_question_id") or f"project-create:{project_id}"
        )
        async with self.repository.database.connection() as connection:
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (f"project-create:{project_id}",),
            )
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                ("workspace-scope-config",),
            )
            run = await (
                await connection.execute(
                    """
                    SELECT * FROM assistant.runs
                    WHERE run_id=%s AND worker_instance_id=%s AND claim_token=%s
                      AND status='running' FOR UPDATE
                    """,
                    (run_id, worker_instance_id, claim_token),
                )
            ).fetchone()
            if run is None:
                raise PermissionError("the scoped Butler Run is no longer leased")
            answer = dict((run["request_json"] or {}).get("answer") or {})
            decision = approval_decision(answer)
            if decision is None:
                answers = answer.get("answers") or {}
                selected = answers.get(approval_question_id) if isinstance(answers, dict) else None
                selected_payload = selected if isinstance(selected, dict) else {"text": selected}
                decision = approval_decision(selected_payload)
            if decision != "approve":
                raise PermissionError("the current user answer does not explicitly approve Project creation")
            existing = next(
                (item for item in self._entries() if str(item.get("project_id")) == project_id), None
            )
            if existing is not None:
                if existing.get("proposal_sha256") != expected_sha256:
                    raise ValueError("Project ID is already registered by a different proposal")
                await connection.commit()
                return {
                    "project_id": project_id, "status": "active", "purpose": purpose,
                    "initial_goal": str(proposal.get("initial_goal") or ""),
                    "workspace_roots": list(proposal.get("workspace_roots") or []),
                    "idempotent": True,
                }
            self._bootstrap(
                project_id, purpose, proposal=proposal,
                proposal_uri=proposal_uri, proposal_sha256=expected_sha256,
            )
            applied = await SourceScopeService(
                self.settings, self.repository
            ).apply_full_directory_grants(
                connection=connection,
                target_project_id=project_id,
                bindings=list(proposal.get("workspace_roots") or []),
                source_binding={
                    "proposal_uri": proposal_uri,
                    "proposal_sha256": expected_sha256,
                    "approval_question_id": approval_question_id,
                    "answer_message_id": (run.get("request_json") or {}).get(
                        "answer_message_id"
                    ),
                    "authorization_mode": "project_creation",
                },
                source_message_id=str(run.get("trigger_message_id") or "") or None,
                approval_message_id=str(
                    (run.get("request_json") or {}).get("answer_message_id") or ""
                ) or None,
            )
            await connection.commit()
        return {
            "project_id": project_id, "status": "active", "purpose": purpose,
            "initial_goal": str(proposal.get("initial_goal") or ""),
            "workspace_roots": list(applied["bindings"]),
            "file_grant_ids": list(applied["grant_ids"]),
            "idempotent": False,
        }

    async def create_and_handoff_from_approved_run(
        self, *, run_id: str, worker_instance_id: str, claim_token: str,
        proposal_uri: str, expected_sha256: str, handoff_summary: str = "",
    ) -> dict[str, Any]:
        """Create or resume one approved Project and ensure its PM Run exists."""
        created = await self.create_from_approved_run(
            run_id=run_id, worker_instance_id=worker_instance_id,
            claim_token=claim_token, proposal_uri=proposal_uri,
            expected_sha256=expected_sha256,
        )
        summary = handoff_summary.strip() or str(created.get("initial_goal") or "").strip()
        if not summary:
            summary = f"接手新项目：{created['purpose']}"
        source = await self.repository.claimed_run_source(
            run_id=run_id,
            worker_instance_id=worker_instance_id,
            claim_token=claim_token,
        )
        target_channel: str | None = None
        target_chat_id: str | None = None
        provisioning: dict[str, Any] | None = None
        if str(source["channel"]) == "discord":
            # Imported lazily to keep the file-owned Project registry channel-neutral.
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
                        "Discord Project provisioning requires a Guild conversation"
                    )
                card = next(
                    item for item in self.list()
                    if str(item.get("project_id")) == str(created["project_id"])
                )
                discord_provisioner = DiscordProjectProvisioner(
                    self.repository, discord_config
                )
                provisioning = await discord_provisioner.provision(
                    project_id=str(created["project_id"]),
                    display_name=str(card.get("display_name") or created["project_id"]),
                    purpose=str(created["purpose"]),
                    guild_id=guild_id,
                    bound_by_user_id=str(source.get("user_id") or "system"),
                    source_message_id=str(source["message_id"]),
                    project_routing_hash=str(card["routing_hash"]),
                )
                provisioning["interfaces"] = (
                    await discord_provisioner.provision_supervisor_interface(
                        project_id=str(created["project_id"]), guild_id=guild_id,
                    )
                )
                target_channel = "discord"
                target_chat_id = str(provisioning["channel_id"])
        handoff = await self.repository.handoff_from_claim(
            source_run_id=run_id, worker_instance_id=worker_instance_id,
            claim_token=claim_token, target_project_id=str(created["project_id"]),
            handoff_summary=summary,
            target_channel=target_channel,
            target_chat_id=target_chat_id,
        )
        if provisioning is not None and target_chat_id is not None:
            mention = f"<#{target_chat_id}>"
            await self.repository.create_outbound(
                project_id="system",
                session_id=None,
                run_id=run_id,
                channel="discord",
                chat_id=str(source["chat_id"]),
                user_id=str(source.get("user_id") or "") or None,
                message_type="reply",
                content={
                    "text": (
                        f"Project `{created['project_id']}` 已创建并绑定到 {mention}。"
                        "PM Run 已建立，后续项目对话请在新频道继续。"
                    ),
                    "origin": "kernel", "context_visibility": "ui_only",
                },
                reply_to_message_id=str(source["message_id"]),
                idempotency_key=(
                    f"discord-project-provision-receipt:{created['project_id']}:"
                    f"{target_chat_id}"
                ),
            )
            await self.repository.create_outbound(
                project_id=str(created["project_id"]),
                session_id=None,
                run_id=run_id,
                channel="discord",
                chat_id=target_chat_id,
                user_id=str(source.get("user_id") or "") or None,
                message_type="reply",
                content={
                    "text": (
                        f"Project `{created['project_id']}` 已就绪。"
                        "此频道固定连接本 Project 的 PM；首个任务正在接管。"
                    ),
                    "origin": "kernel", "context_visibility": "ui_only",
                },
                reply_to_message_id=None,
                idempotency_key=(
                    f"discord-project-channel-welcome:{created['project_id']}:"
                    f"{target_chat_id}"
                ),
            )
        return {
            **created,
            "handoff_message_id": str(handoff["message"]["message_id"]),
            "pm_run_id": str(handoff["run"]["run_id"]),
            "pm_run_status": str(handoff["run"]["status"]),
            "discord_provisioning": provisioning,
        }

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
                    "timeout_seconds": 90, "max_retries": 2,
                    "control_mode": "advisory",
                    "daily_input_tokens": 200_000,
                    "daily_output_tokens": 40_000,
                    "max_task_turns": 24,
                    "max_fetch_requests": 3,
                },
                "multi_agent": {
                    "enabled": True,
                    "max_concurrent_subagents": 3,
                },
                "work_modes": {
                    mode: {
                        "mode_id": mode,
                        "profile": mode,
                        "context_profile": mode,
                        "permission_profile": (
                            "candidate-write" if mode in {"build", "repair"}
                            else "read-only"
                        ),
                        "validation_policy": {
                            "required_scopes": ["structure", "component"]
                        },
                    }
                    for mode in ("requirements", "build", "audit", "operate", "repair")
                },
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
        SemanticMapService(self.settings).bootstrap(
            project_id, purpose=purpose,
            base_pack_version="assistant-base-pack.v1",
        )
        skill_root = Path(__file__).parents[1] / "prompts" / "skills"
        role_root = Path(__file__).parents[1] / "prompts" / "roles"
        manager_role_source = (
            Path(__file__).parents[1] / "prompts" / "roles" / "project-agent.md"
        )
        manager_config = {
            "schema_version": "agent-config.v2",
            "agent_id": "pm",
            "agent_key": f"project:{project_id}:manager",
            "kind": "manager",
            "status": "active",
            "display_name": "Project Agent",
            "aliases": ["manager"],
            "role": "role.md",
            "scope_profile": "current-project",
            "harness": {
                "provider": "openai", "context_policy": "max",
                "reasoning_effort": "medium",
                "timeout_seconds": 600, "sandbox": "project-scope",
            },
            "skills": [
                {"name": "assistant-task-continuity", "source": "kernel"},
                {"name": "assistant-project-agent", "source": "kernel"},
                {"name": "assistant-subagent-orchestration", "source": "kernel"},
                {"name": "assistant-requirements", "source": "project"},
                {"name": "assistant-release-audit", "source": "kernel"},
                {"name": "assistant-history-retrieval", "source": "kernel"},
            ],
            "mcp_servers": {},
            "entrypoints": {"pm_handoff": True, "feishu_slash": True},
            "triggers": ["user_message", "handoff"],
            "tools": [
                "get_authorized_workspace", "list_authorized_tree",
                "read_authorized_file",
                "current_conversation_route", "list_recent_history",
                "get_project_map", "query_project_map", "read_map_component",
                "list_source_discovery_roots", "inspect_source_path",
                "list_project_credentials",
                "get_project_credential", "request_project_credential",
                "delete_project_credential",
                "run_status",
                "write_candidate_file", "delete_candidate_file",
            ],
        }
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
                "Owns the single business Task conversation across Requirements, Build, "
                "Audit, Operate and Repair. Kernel owns accepted document refs, approval, "
                "Validation and publication. Read the Project Semantic Map before broad "
                "source discovery.\n"
            ),
        )
        for profile_name, profile_payload in {
            "requirements": {
                "schema_version": "project-agent-profile.v1", "mode_id": "requirements",
                "role_append": (
                    "# Active mode · Requirements\nExpand, research, challenge and clarify "
                    "the user's requirements using internet, Project sources and configured "
                    "external systems. Keep current Requirements accurate. Do not write "
                    "candidate files. Submit needs_approval before Build."
                ),
                "disabled_tools": ["write_candidate_file", "delete_candidate_file"],
            },
            "build": {
                "schema_version": "project-agent-profile.v1", "mode_id": "build",
                "role_append": (
                    "# Active mode · Build\nImplement approved Requirements in Task "
                    "candidate, test results and record actual external effects. If approved "
                    "Requirements must change, return return_to_requirements with the complete "
                    "revised document so Kernel can reset approval and rotate the Session."
                ), "disabled_tools": [],
            },
                "audit": {
                "schema_version": "project-agent-profile.v1", "mode_id": "audit",
                "role_append": (
                    "# Active mode · Audit\nRe-evaluate Requirements, candidate, Validation "
                    "and actual effects in a fresh physical Session."
                ), "disabled_tools": ["write_candidate_file", "delete_candidate_file"],
            },
            "operate": {
                "schema_version": "project-agent-profile.v1", "mode_id": "operate",
                "role_append": (
                    "# Active mode · Operate\nInterpret published runtime state and receipts. "
                    "Do not change formal code; open an Incident or Repair request when needed."
                ), "disabled_tools": ["write_candidate_file", "delete_candidate_file"],
            },
            "repair": {
                "schema_version": "project-agent-profile.v1", "mode_id": "repair",
                "role_append": (
                    "# Active mode · Repair\nUse the Incident and approved repair scope to "
                    "modify candidate files, test the repair and preserve rollback evidence."
                ), "disabled_tools": [],
            },
        }.items():
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
        child_config = {
            "schema_version": "agent-config.v2", "agent_id": "child",
            "agent_key": f"project:{project_id}:child", "kind": "child",
            "status": "active", "display_name": "Project Child Worker",
            "aliases": [], "role": "role.md", "scope_profile": "current-project",
            "harness": {
                "provider": "openai", "context_policy": "max",
                "reasoning_effort": "medium", "timeout_seconds": 600,
                "sandbox": "project-scope",
            },
            "skills": [{"name": "assistant-task-continuity", "source": "kernel"}],
            "mcp_servers": {},
            "entrypoints": {"pm_handoff": False, "feishu_slash": False},
            "triggers": ["child_task"],
            "tools": [
                "get_authorized_workspace", "list_authorized_tree",
                "read_authorized_file", "write_candidate_file",
                "delete_candidate_file", "get_project_map", "query_project_map",
                "read_map_component", "list_project_credentials",
                "get_project_credential", "run_status",
            ],
        }
        self._write_once(
            root / "agents" / "child" / "config.yaml",
            yaml.safe_dump(child_config, sort_keys=False, allow_unicode=True),
        )
        self._write_once(
            root / "agents" / "child" / "role.md",
            "# Project Child Worker\n\nExecute one bounded Delegation Request. "
            "Do not create a new Task, contact the user, publish or spawn descendants. "
            "Return exactly one child-result.v1 JSON object.\n",
        )
        self._write_once(
            root / "agents" / "child" / "MAP.md",
            "---\nschema_version: agent-map.v1\nagent_id: child\n"
            "owner: kernel-base-pack\nstate_authority: parent-task\n"
            "context_profile: delegated-bounded-work\n---\n\n"
            "# Project Child Worker Map\n\nExecutes one parent-owned Delegation "
            "Request and returns child-result.v1 only to the parent Task.\n",
        )
        operator_config = {
            "schema_version": "agent-config.v2", "agent_id": "operator",
            "agent_key": f"project:{project_id}:operator", "kind": "operator",
            "status": "active", "display_name": "Project Operator", "aliases": [],
            "role": "role.md", "scope_profile": "current-project",
            "harness": {"provider": "openai", "model": "gpt-5.6-luna",
                        "reasoning_effort": "low", "context_policy": "max",
                        "timeout_seconds": 180, "sandbox": "project-scope"},
            "skills": [{"name": "assistant-project-operator", "source": "kernel"}],
            "mcp_servers": {},
            "entrypoints": {"pm_handoff": False, "feishu_slash": False},
            "triggers": ["incident", "scheduled_health"],
            "tools": [
                "run_status", "list_recent_history", "read_authorized_file",
                "get_project_map", "query_project_map", "read_map_component",
            ],
        }
        self._write_once(
            root / "agents" / "operator" / "config.yaml",
            yaml.safe_dump(operator_config, sort_keys=False, allow_unicode=True),
        )
        self._write_once(
            root / "agents" / "operator" / "role.md",
            (role_root / "project-operator.md").read_text(encoding="utf-8"),
        )
        self._write_once(
            root / "agents" / "operator" / "MAP.md",
            (
                "---\nschema_version: agent-map.v1\nagent_id: operator\n"
                "owner: kernel-base-pack\nstate_authority: observations-only\n"
                "context_profile: operations-and-incidents\n---\n\n"
                "# Project Operator Map\n\n"
                "Reads Operation Map, runbooks, health, Runs and receipts. Produces typed "
                "observations and incidents; code repairs are separate PM Tasks.\n"
            ),
        )
        skill_name = "assistant-requirements"
        source = skill_root / skill_name / "SKILL.md"
        target_root = root / ".agents" / "skills" / skill_name
        target = target_root / "SKILL.md"
        self._write_once(target, source.read_text(encoding="utf-8"))
        data = target.read_bytes()
        files = [{
            "path": "SKILL.md", "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
        }]
        bundle_hash = hashlib.sha256(
            json.dumps(files, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        self._write_once(
            target_root / "skill-manifest.json",
            json.dumps({
                "schema_version": "skill-bundle.v1", "name": skill_name,
                "active_revision": bundle_hash, "files": files,
            }, ensure_ascii=False, indent=2),
        )
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
        RuntimeCatalogService(self.settings).refresh()

    @staticmethod
    def _write_once(path: Path, content: str) -> None:
        if path.exists():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
