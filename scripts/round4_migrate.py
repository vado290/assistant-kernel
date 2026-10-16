"""Round 4 one-off migration (docs/ROUND4_PLAN.md); every step can be run again safely.

1. changes/iterations.md of each project → core.iterations (kind imported); the file moves to archive/round4/
2. the retired semantic map knowledge/map/ → archive/round4/map-legacy/
3. kernel project: map_path docs/ARCHITECTURE.md
4. kernel project: its own repository grant (kernel-source) becomes read-only (it delivers through worktrees)

    python scripts/round4_migrate.py
"""
from __future__ import annotations

import asyncio
import re
import shutil
from datetime import datetime, timezone

import yaml

from assistant_kernel.config import KernelSettings
from assistant_kernel.core import grants, machine
from assistant_kernel.core.db import Database, lock_project, one
from assistant_kernel.storage import ProjectRuntimeManager

LINE = re.compile(r"^- (\d{4}-\d{2}-\d{2}) · (.+?) · (\S+?)/(\S+?) · (.+?) · 原因：(.*?)(?: <!-- task:([0-9a-f-]+) -->)?$")


async def main() -> None:
    settings = KernelSettings.from_environment()
    db = Database(settings)
    runtime = ProjectRuntimeManager(settings.data_root)
    async with db.tx() as c:
        projects = [r["project_id"] for r in await (await c.execute("SELECT project_id FROM core.projects")).fetchall()]
    for project_id in projects:
        vault = runtime.ensure(project_id).vault_root
        archive = vault / "archive" / "round4"
        log = vault / "changes" / "iterations.md"
        if log.is_file():
            imported = 0
            async with db.tx() as c:
                for line in log.read_text("utf-8").splitlines():
                    m = LINE.match(line.strip())
                    if not m:
                        continue
                    date, outcome, origin, kind, goal, reason, task = m.groups()
                    exists = await one(c, "SELECT 1 FROM core.iterations WHERE project_id=%s AND kind='imported' "
                                          "AND facts->>'imported_line' = %s", (project_id, line.strip()))
                    if exists:
                        continue
                    row = await machine.insert_iteration(
                        c, project_id, None, kind="imported", source=origin, title=goal[:200],
                        reason=reason if reason != "—" else goal, outcome=outcome,
                        facts={"imported_line": line.strip(), "original_task": task, "kind": kind})
                    await c.execute("UPDATE core.iterations SET created_at=%s WHERE iteration_id=%s",
                                    (datetime.fromisoformat(date).replace(tzinfo=timezone.utc), row["iteration_id"]))
                    imported += 1
            archive.mkdir(parents=True, exist_ok=True)
            shutil.move(str(log), str(archive / "iterations.md"))
            print(f"{project_id}: imported {imported} lines from changes/iterations.md")
        legacy_map = vault / "knowledge" / "map"
        if legacy_map.is_dir():
            archive.mkdir(parents=True, exist_ok=True)
            shutil.move(str(legacy_map), str(archive / "map-legacy"))
            print(f"{project_id}: archived knowledge/map/")

    vault = runtime.ensure("kernel").vault_root
    config_path = vault / "project.yaml"
    config = yaml.safe_load(config_path.read_text("utf-8")) or {}
    if config.get("delivery") == "git-repo" and config.get("map_path") != "docs/ARCHITECTURE.md":
        config["map_path"] = "docs/ARCHITECTURE.md"
        config_path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), "utf-8")
        print("kernel: map_path docs/ARCHITECTURE.md")

    async with db.tx() as c:
        current = await one(c, """SELECT policy_json FROM core.project_grants WHERE project_id='kernel'
                                  AND resource_key='kernel-source' AND status='active'""")
        if current and current["policy_json"].get("access") != "read":
            await lock_project(c, "kernel")
            directory = grants.check_directory({"path": current["policy_json"]["path"], "access": "read",
                                                "purpose": current["policy_json"].get("description") or "Kernel 源码"},
                                               "kernel", settings.data_root)
            directory["key"] = "kernel-source"
            await machine.grant_directory(c, "kernel", directory, source="user",
                                          reason="kernel 改为代码交付后，对自身仓库只需只读（用户确认，ROUND4）")
            print("kernel: kernel-source is now read-only")


if __name__ == "__main__":
    asyncio.run(main())
