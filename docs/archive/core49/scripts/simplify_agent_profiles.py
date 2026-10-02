"""Apply the small Agent-profile layout after a recoverable external backup.

An Agent row is an auditable configuration identity for a short-lived Codex
turn.  It is not a process.  The only durable profiles are the Butler and, for
each active Project, PM/requirements/implementation/review.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import shutil
from datetime import datetime, timezone
from pathlib import Path

import yaml

from assistant_kernel.config import KernelSettings
from assistant_kernel.repositories import ControlPlaneRepository
from assistant_kernel.services.agent_config import AgentConfigManager
from assistant_kernel.services.agent_directory import AgentDirectory
from assistant_kernel.services.prompt_bundle import STAGE_SKILLS, default_role


SPECIAL_PROJECTS = ("super-assistant", "kernel")
RETIRED_BOOTSTRAP_PROJECTS = ("kernel-v2-smoke", "mvp-smoke", "second-project")
ACTIVE_STAGE_TYPES = {f"stage_{stage}" for stage in STAGE_SKILLS}


def _archive_and_write(path: Path, content: str, history_root: Path) -> None:
    if path.exists():
        current = path.read_text(encoding="utf-8")
        if current == content:
            return
        digest = hashlib.sha256(current.encode("utf-8")).hexdigest()[:12]
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        archive = history_root / f"{stamp}__{digest}.md"
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_text(current, encoding="utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


async def _assert_quiescent(repository: ControlPlaneRepository) -> None:
    async with repository.database.connection() as connection:
        active_jobs = await (await connection.execute(
            "SELECT count(*) AS n FROM assistant.control_jobs "
            "WHERE status IN ('queued','running')"
        )).fetchone()
        active_runs = await (await connection.execute(
            "SELECT count(*) AS n FROM assistant.workflow_runs "
            "WHERE status IN ('queued','running','waiting_user')"
        )).fetchone()
        pending = await (await connection.execute(
            "SELECT count(*) AS n FROM assistant.approval_requests WHERE status='pending'"
        )).fetchone()
    if active_jobs["n"] or active_runs["n"] or pending["n"]:
        raise RuntimeError(
            "refusing profile simplification while jobs, runs, or approvals are active"
        )


async def _retire_unwanted(repository: ControlPlaneRepository) -> list[str]:
    keep_types = {"butler", "project_manager", *ACTIVE_STAGE_TYPES}
    async with repository.database.connection() as connection:
        rows = await (await connection.execute(
            """
            SELECT agent_id FROM assistant.agents
            WHERE status='active' AND (
                project_id = ANY(%s)
                OR agent_type <> ALL(%s)
            ) ORDER BY agent_id
            """,
            (list(RETIRED_BOOTSTRAP_PROJECTS), list(keep_types)),
        )).fetchall()
        agent_ids = [str(row["agent_id"]) for row in rows]
        if agent_ids:
            await connection.execute(
                "UPDATE assistant.agent_config_versions SET status='retired',retired_at=COALESCE(retired_at,now()) "
                "WHERE agent_id=ANY(%s) AND status='active'", (agent_ids,)
            )
            await connection.execute(
                "UPDATE assistant.agent_scope_versions SET status='retired',retired_at=COALESCE(retired_at,now()) "
                "WHERE agent_id=ANY(%s) AND status='active'", (agent_ids,)
            )
            await connection.execute(
                "UPDATE assistant.role_versions SET status='retired' WHERE role_id IN "
                "(SELECT role_id FROM assistant.roles WHERE agent_id=ANY(%s)) AND status='active'",
                (agent_ids,),
            )
            await connection.execute(
                "UPDATE assistant.roles SET status='retired' WHERE agent_id=ANY(%s)",
                (agent_ids,),
            )
            await connection.execute(
                "UPDATE assistant.agents SET status='retired',active_scope_version_id=NULL," 
                "active_agent_config_version_id=NULL,updated_at=now() WHERE agent_id=ANY(%s)",
                (agent_ids,),
            )
        await connection.execute(
            "UPDATE assistant.projects SET status='retired',updated_at=now() "
            "WHERE project_id=ANY(%s) AND status='active'",
            (list(RETIRED_BOOTSTRAP_PROJECTS),),
        )
        await connection.commit()
    return agent_ids


async def _ensure_profile_set(
    settings: KernelSettings, repository: ControlPlaneRepository
) -> list[str]:
    directory = AgentDirectory(settings, repository)
    configs = AgentConfigManager(settings)
    prompt_skills = Path(__file__).parents[1] / "src" / "assistant_kernel" / "prompts" / "skills"

    # Relocate the global Butler home outside Project Vaults. Historical files
    # remain intact because the whole directory moves together.
    old_butler = settings.data_root / "vaults" / "system"
    new_butler = settings.data_root / "butler"
    if old_butler.exists() and not new_butler.exists():
        new_butler.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(old_butler), str(new_butler))
    elif old_butler.exists() and new_butler.exists():
        raise RuntimeError("both old and new Butler homes exist; merge requires manual review")

    await repository.ensure_default_agent(None)
    desired = ["system-butler"]
    for project_id in SPECIAL_PROJECTS:
        await repository.ensure_default_agent(project_id)
        desired.append(f"project:{project_id}:manager")
        for stage in STAGE_SKILLS:
            desired.append(await repository.ensure_stage_agent(project_id, stage))

    for agent_id in desired:
        view = await directory.get(agent_id)
        if view is None:
            raise RuntimeError(f"missing desired Agent profile: {agent_id}")
        if str(view["agent_type"]).startswith("stage_"):
            stage = str(view["agent_type"]).removeprefix("stage_")
            role = await repository.ensure_role_version(
                agent_id, "primary", default_role(stage), "simplify to one generic stage-worker role"
            )
        else:
            initialized = await directory.initialize(agent_id)
            role = await repository.get_active_role(agent_id)
            if initialized is None or role is None:
                raise RuntimeError(f"failed to initialize {agent_id}")

        # Existing configs must explicitly pin a newly published generic role.
        current = await configs.repository.get_active(agent_id)
        if current is None:
            await configs.resolve(
                agent_id=agent_id,
                agent_type=str(view["agent_type"]),
                project_id=(str(view["project_id"]) if view.get("project_id") else None),
                role_version_id=str(role["role_version_id"]),
            )
        elif str((current["config_json"].get("role") or {}).get("role_version_id")) != str(role["role_version_id"]):
            candidate = dict(current["config_json"])
            await configs.publish(
                agent_id=agent_id,
                content_yaml=yaml.safe_dump(candidate, allow_unicode=True, sort_keys=False),
                change_reason="pin simplified generic worker role",
                accepted_by="system-profile-simplification",
            )

        refreshed = await directory.get(agent_id)
        root = Path(refreshed["home_path"])
        skill_name = (
            "assistant-butler-intake" if refreshed["agent_type"] == "butler"
            else "assistant-project-manager" if refreshed["agent_type"] == "project_manager"
            else STAGE_SKILLS[str(refreshed["agent_type"]).removeprefix("stage_")]
        )
        source = prompt_skills / skill_name / "SKILL.md"
        target = root / ".agents" / "skills" / skill_name / "SKILL.md"
        _archive_and_write(
            target,
            source.read_text(encoding="utf-8"),
            root / ".agents" / "skill-history" / skill_name,
        )
        if str(refreshed["agent_type"]).startswith("stage_"):
            role_path = Path(refreshed["role_path"])
            _archive_and_write(
                role_path,
                str(role["content"]),
                root / "roles" / "history" / str(refreshed["agent_type"]),
            )
    return desired


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="perform the reversible simplification")
    args = parser.parse_args()
    settings = KernelSettings.from_environment()
    repository = ControlPlaneRepository(settings)
    await _assert_quiescent(repository)
    if not args.apply:
        print("DRY_RUN_OK; add --apply after creating a recoverable backup")
        return
    desired = await _ensure_profile_set(settings, repository)
    retired = await _retire_unwanted(repository)
    print(f"SIMPLIFIED_AGENT_PROFILES_OK; active={len(desired)}; retired={len(retired)}")
    for agent_id in desired:
        print(agent_id)


if __name__ == "__main__":
    asyncio.run(main())
