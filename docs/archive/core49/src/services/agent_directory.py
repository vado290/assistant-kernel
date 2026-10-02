"""Readable agent locations and role exports; this is not a filesystem sandbox."""
from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

from assistant_kernel.repositories import ControlPlaneRepository
from assistant_kernel.storage import ProjectRuntimeManager
from assistant_kernel.services.agent_config import AgentConfigManager


class AgentDirectory:
    def __init__(self, settings, repository: ControlPlaneRepository):
        self.repository = repository
        self.runtime = ProjectRuntimeManager(settings.data_root)
        self.configs = AgentConfigManager(settings)

    async def list(self, project_id: str | None = None) -> list[dict]:
        async with self.repository.database.connection() as c:
            cursor = await c.execute(
                "SELECT agent_id,project_id,agent_type,display_name,status FROM assistant.agents "
                "WHERE (%s::text IS NULL OR project_id=%s) ORDER BY agent_id",
                (project_id, project_id),
            )
            agents = await cursor.fetchall()
        return [await self.describe(agent) for agent in agents]

    async def get(self, agent_id: str) -> dict | None:
        async with self.repository.database.connection() as c:
            agent = await (await c.execute(
                "SELECT agent_id,project_id,agent_type,display_name,status FROM assistant.agents WHERE agent_id=%s",
                (agent_id,),
            )).fetchone()
        return await self.describe(agent) if agent else None

    async def describe(self, agent: dict) -> dict:
        runtime = self.runtime.describe(str(agent["project_id"] or "system"))
        role_relative = "role.md"
        if agent["agent_type"] not in {"butler", "project_manager"}:
            from assistant_kernel.services.prompt_bundle import STAGE_SKILLS, role_relative as stage_role_path
            stage = agent["agent_type"].removeprefix("stage_")
            if stage in STAGE_SKILLS:
                role_relative = stage_role_path(stage)
            else:
                key = uuid.uuid5(uuid.NAMESPACE_URL, agent["agent_id"]).hex
                role_relative = f"roles/{key}/role.md"
        role_path = self.runtime.resolve_below(runtime.vault_root, role_relative)
        role = await self.repository.get_active_role(agent["agent_id"])
        matches = None
        if role and role_path.is_file():
            matches = hashlib.sha256(role_path.read_bytes()).hexdigest() == role["content_sha256"]
        return {
            **agent, "home_path": str(runtime.vault_root), "cwd": str(runtime.vault_root),
            "codex_home": str(runtime.codex_home), "role_path": str(role_path),
            "skills_path": str(self.runtime.resolve_below(runtime.vault_root, ".agents/skills")),
            "role_version_id": str(role["role_version_id"]) if role else None,
            "role_file_matches_active": matches,
            "scope_enforcement": "vault_path_checks_only; shell_and_python_isolation_not_implemented",
            "agent_config_path": str(self.runtime.resolve_below(
                runtime.vault_root,
                self.configs.config_relative_path(str(agent["agent_id"]), str(agent["agent_type"])),
            )),
        }

    async def initialize(self, agent_id: str) -> dict | None:
        view = await self.get(agent_id)
        if view is None:
            return None
        role = await self.repository.get_active_role(agent_id)
        if role is None:
            from assistant_kernel.services.conversation import BUTLER_ROLE, PROJECT_MANAGER_ROLE
            from assistant_kernel.services.project_build import STAGE_ROLES
            if view["agent_type"] == "butler":
                content = BUTLER_ROLE
            elif view["agent_type"] == "project_manager":
                content = PROJECT_MANAGER_ROLE
            else:
                stage = view["agent_type"].removeprefix("stage_")
                if stage not in STAGE_ROLES:
                    raise ValueError("agent needs an explicitly published role")
                content = STAGE_ROLES[stage]
            role = await self.repository.ensure_role_version(agent_id, "primary", content)
        runtime = self.runtime.ensure(str(view["project_id"] or "system"))
        role_path = Path(view["role_path"])
        role_path.parent.mkdir(parents=True, exist_ok=True)
        # Leave a hand-edited role.md intact. The database remains the active version authority.
        if not role_path.exists():
            try:
                with role_path.open("x", encoding="utf-8", newline="") as f:
                    f.write(role["content"])
            except FileExistsError:
                pass
        snapshot = self.runtime.resolve_below(runtime.vault_root,
            f"roles/versions/{role['role_version_id']}.md")
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        if snapshot.exists():
            if hashlib.sha256(snapshot.read_bytes()).hexdigest() != role["content_sha256"]:
                raise ValueError("role version snapshot hash mismatch")
        else:
            try:
                with snapshot.open("x", encoding="utf-8", newline="") as f:
                    f.write(role["content"])
            except FileExistsError:
                if hashlib.sha256(snapshot.read_bytes()).hexdigest() != role["content_sha256"]:
                    raise ValueError("role version snapshot hash mismatch")
        await self.configs.resolve(
            agent_id=agent_id,
            agent_type=str(view["agent_type"]),
            project_id=(str(view["project_id"]) if view.get("project_id") else None),
            role_version_id=str(role["role_version_id"]),
        )
        return await self.get(agent_id)
