from __future__ import annotations

import asyncio

from assistant_kernel.config import KernelSettings
from assistant_kernel.repositories import ControlPlaneRepository
from assistant_kernel.services.agent_directory import AgentDirectory


async def main() -> None:
    settings = KernelSettings.from_environment()
    repository = ControlPlaneRepository(settings)
    directory = AgentDirectory(settings, repository)
    agents = await directory.list()
    initialized = []
    for agent in agents:
        if agent["status"] != "active":
            continue
        result = await directory.initialize(str(agent["agent_id"]))
        initialized.append(
            {
                "agent_id": result["agent_id"],
                "project_id": result["project_id"],
                "agent_type": result["agent_type"],
                "agent_config_path": result["agent_config_path"],
            }
        )
    print(f"initialized_agent_configs={len(initialized)}")
    for item in initialized:
        print(f"{item['agent_id']} -> {item['agent_config_path']}")


if __name__ == "__main__":
    asyncio.run(main())

