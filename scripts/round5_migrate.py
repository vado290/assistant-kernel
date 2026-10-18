"""ROUND5 R5.1 one-off migration of existing project folders (docs/ROUND5_PLAN.md §10.1).

For each project:
  1. back up what is touched (backups/round5-r51-<date>/<project>.tar.gz);
  2. move each skill from the project root `.agents/skills/` into the folders of the agents its load.roles name
     (`agents/<agent>/.agents/skills/`), one copy per agent; then archive the root `.agents/`;
  3. move the earlier generations' folders and files to `archive/pre-r5/`;
  4. keep only the project.yaml fields Kernel reads (the original goes to the archive; removed content is listed);
  5. bring never-edited skill copies up to the current templates and add missing template copies;
  6. report anything that still breaks the folder rule.
Global (with --revoke-butler): revoke the retired butler-maintenance directory grant.

Run with the prod venv after deploying 0.44.0:  python scripts/round5_migrate.py [--dry-run] <project> ...
"""
from __future__ import annotations

import argparse
import shutil
import tarfile
from datetime import date
from pathlib import Path

import yaml

from assistant_kernel.config import KernelSettings
from assistant_kernel.core import skills

LEGACY = ("artifacts", "background", "capabilities", "changes", "handoffs", "operations", "reports", "roles",
          "sources", "templates", "source-map.yaml", "project-bootstrap.json")
KEEP_FIELDS = ("schema_version", "project_id", "display_name", "purpose", "layout_version", "created_by", "created_at",
               "status", "capabilities", "delivery", "share_with_kernel", "validation_policy", "multi_agent",
               "repository", "main_branch", "validation", "release_note", "map_path")
CONTENT_FIELDS = ("boundaries", "non_goals", "initial_goal", "routing")   # worth a look before they leave


def back_up(vault: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(target, "w:gz") as tar:
        for name in (".agents", "agents", "project.yaml", *LEGACY):
            if (vault / name).exists():
                tar.add(vault / name, arcname=name)


def move_skills(vault: Path, dry: bool) -> list[str]:
    done = []
    root = vault / ".agents" / "skills"
    for skill in skills._docs(root, "*/SKILL.md"):
        roles = skill.load.get("roles") or list(skills.MANAGING_AGENTS)
        agents = [role for role in roles if (vault / "agents" / role).is_dir()]
        if not agents:
            done.append(f"{skill.name}：没有对应的 agent 目录（{roles}），随 .agents 一起存档")
            continue
        for agent in agents:
            target = skills.agent_skill_dir(vault, agent) / skill.name
            if target.exists():
                continue
            done.append(f"{skill.name} → agents/{agent}")
            if not dry:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(skill.path.parent, target)
    return done


def archive(vault: Path, names: tuple[str, ...], dry: bool) -> list[str]:
    target = vault / "archive" / "pre-r5"
    moved = []
    for name in names:
        source = vault / name
        if not source.exists():
            continue
        moved.append(name)
        if not dry:
            target.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(target / name))
    return moved


def slim_project_yaml(vault: Path, dry: bool) -> tuple[list[str], dict]:
    path = vault / "project.yaml"
    data = yaml.safe_load(path.read_text("utf-8")) or {}
    removed = [key for key in data if key not in KEEP_FIELDS]
    content = {key: data[key] for key in CONTENT_FIELDS if data.get(key)}
    if removed and not dry:
        target = vault / "archive" / "pre-r5" / "project.yaml"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        kept = {key: data[key] for key in KEEP_FIELDS if key in data}
        path.write_text(yaml.safe_dump(kept, allow_unicode=True, sort_keys=False), "utf-8", newline="\n")
    return removed, content


def migrate(settings: KernelSettings, project_id: str, dry: bool) -> None:
    vault = settings.data_root / "vaults" / project_id
    print(f"\n=== {project_id} {'(dry run)' if dry else ''}")
    if not dry:
        backup = settings.data_root / "backups" / f"round5-r51-{date.today():%Y%m%d}" / f"{project_id}.tar.gz"
        back_up(vault, backup)
        print(f"备份：{backup}")
    for line in move_skills(vault, dry):
        print(f"  技能 {line}")
    print(f"  存档：{', '.join(archive(vault, ('.agents', *LEGACY), dry)) or '无'}")
    removed, content = slim_project_yaml(vault, dry)
    print(f"  project.yaml 移出字段：{', '.join(removed) or '无'}")
    for key, value in content.items():
        print(f"    {key}: {yaml.safe_dump(value, allow_unicode=True).strip()[:600]}")
    if not dry:
        capabilities = skills.project_config(vault)["capabilities"]
        refreshed, kept = skills.refresh_unmodified(vault)
        added = skills.install_templates(vault, capabilities)
        print(f"  模板更新：{len(refreshed)} 份未改动的副本；保留项目改动过的 {sorted(set(kept)) or '无'}；补装 {added or '无'}")
        problems = skills.layout_problems(vault)
        print(f"  目录规则：{'通过' if not problems else '；'.join(problems)}")


def revoke_butler(settings: KernelSettings) -> None:
    import psycopg
    from assistant_kernel.core import migrate as core_migrate
    with psycopg.connect(**core_migrate.connection_args(settings)) as connection:
        count = connection.execute(
            """UPDATE core.project_grants SET status='revoked', revoked_at=now()
               WHERE project_id='butler-maintenance' AND status='active'""").rowcount
    print(f"\n撤销 butler-maintenance 的目录授权：{count} 条")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("projects", nargs="+")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--revoke-butler", action="store_true")
    options = parser.parse_args()
    settings = KernelSettings.from_environment()
    for project_id in options.projects:
        migrate(settings, project_id, options.dry_run)
    if options.revoke_butler and not options.dry_run:
        revoke_butler(settings)


if __name__ == "__main__":
    main()
