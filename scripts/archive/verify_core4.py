"""Read-only production verification for the active Core5 runtime."""
from __future__ import annotations

import asyncio
import yaml

from assistant_kernel import __schema_version__
from assistant_kernel.config import KernelSettings
from assistant_kernel.repositories import CoreRepository
from assistant_kernel.services.agent_files import AgentFileLoader
from assistant_kernel.services.project_directory import ProjectDirectoryService
from assistant_kernel.storage import ProjectRuntimeManager


async def main() -> None:
    settings = KernelSettings.from_environment()
    repository = CoreRepository(settings)
    tables = await repository.table_names()
    version = await repository.schema_version()
    expected = [
        "kernel_meta", "messages", "model_invocations", "project_grants", "runs", "sessions",
        "project_file_changes", "project_runtime_units", "service_instances", "project_schedules", "schedule_ticks"
    ]
    if set(tables) != set(expected):
        raise RuntimeError(f"unexpected assistant tables: {tables}")
    if version != __schema_version__:
        raise RuntimeError(f"unexpected schema version: {version}")
    loader = AgentFileLoader(settings)
    project_directory = ProjectDirectoryService(settings, repository)
    runtime = ProjectRuntimeManager(settings.data_root)
    verified_projects = []
    for entry in project_directory.list():
        if entry.get("status") != "active":
            continue
        project_id = str(entry["project_id"])
        project_path = runtime.describe(project_id).vault_root / "project.yaml"
        project = yaml.safe_load(project_path.read_text(encoding="utf-8")) or {}
        if project.get("task_protocol") != "single-agent.v2":
            raise RuntimeError(f"{project_id} does not use single-agent.v2")
        if not {"requirements", "build", "audit", "operate"}.issubset(project.get("work_modes") or {}):
            raise RuntimeError(f"{project_id} Work Mode bundle is incomplete")
        active = {
            item.agent_id: item for item in loader.list_agents(project_id)
            if item.status == "active"
        }
        if set(active) != {"pm", "supervisor"}:
            raise RuntimeError(
                f"{project_id} active Agent set is not the v2 base pack"
            )
        for agent_id in ("pm", "supervisor"):
            loader.load(project_id, active[agent_id].agent_key)
        verified_projects.append(project_id)
    active = {}
    async with repository.database.connection() as connection:
        for table in ("messages", "sessions", "runs"):
            active[table] = int(
                (await (await connection.execute(f"SELECT count(*) AS n FROM assistant.{table}")).fetchone())["n"]
            )
    print(
        "CORE5_VERIFY_OK; "
        f"version={version}; tables={','.join(tables)}; "
        f"projects={','.join(verified_projects)}; rows={active}"
    )


if __name__ == "__main__":
    asyncio.run(main())
