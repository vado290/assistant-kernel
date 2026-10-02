from __future__ import annotations

import asyncio

from assistant_kernel.config import KernelSettings
from assistant_kernel.repositories import ControlPlaneRepository
from assistant_kernel.services.agent_config import AgentConfigManager


async def main() -> None:
    settings = KernelSettings.from_environment()
    repository = ControlPlaneRepository(settings)
    manager = AgentConfigManager(settings)
    async with repository.database.connection() as connection:
        active_jobs = await (
            await connection.execute(
                "SELECT count(*) AS n FROM assistant.control_jobs WHERE status IN ('queued','running')"
            )
        ).fetchone()
        if active_jobs["n"]:
            raise RuntimeError("refusing Scope upgrade while control jobs are active")
        agents = await (
            await connection.execute(
                "SELECT agent_id,project_id,agent_type FROM assistant.agents "
                "WHERE status='active' ORDER BY agent_id"
            )
        ).fetchall()
    changed = 0
    for agent in agents:
        agent_id = str(agent["agent_id"])
        project_id = str(agent["project_id"]) if agent["project_id"] else None
        desired = manager.default_scope_policy(str(agent["agent_type"]), project_id)
        current = await manager.repository.get_active(agent_id)
        if current is None:
            raise RuntimeError(f"Agent config is not initialized: {agent_id}")
        if current["policy"] == desired:
            continue
        await manager.publish_scope(
            agent_id=agent_id,
            policy=desired,
            change_reason="upgrade default Scope Policy for MCP-protected active configuration paths",
            accepted_by="system-deployment-0008",
        )
        changed += 1
    print(f"upgraded_agent_scopes={changed}")


if __name__ == "__main__":
    asyncio.run(main())

