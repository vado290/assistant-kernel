"""Round 2 / R1: bring existing project vaults to the new layout (docs/ROUND2_PLAN.md, R1 step 3).

For each project: archive the old skill copies, profiles and role files under archive/round2-r1/;
add capabilities / delivery / share_with_kernel to project.yaml; install the template skills;
give project-own skills a load rule; write a charter (from project.yaml and the old business profile)
and short role files; for kernel, write the first drafts of the shared context.

Idempotent and model-free. Nothing is deleted: everything replaced is moved or copied into the archive.

    python scripts/round2_r1_vaults.py --data-root /home/v/AI/assistant-data kernel trading-risk-bot ... [--dry-run]
"""
from __future__ import annotations

import argparse
import re
import shutil
from pathlib import Path

import yaml

from assistant_kernel.core import skills
from assistant_kernel.core.executors.bootstrap import TEMPLATE, fill

CAPABILITIES = {"kernel": ["routing", "project_management", "shared_context"]}
ARCHIVE = Path("archive") / "round2-r1"
SHARED_TEMPLATE = Path(skills.__file__).parent / "shared_template"


class Migration:
    def __init__(self, vault: Path, project_id: str, dry_run: bool):
        self.vault, self.project_id, self.dry_run = vault, project_id, dry_run
        self.archive = vault / ARCHIVE
        self.actions: list[str] = []

    def act(self, text: str) -> bool:
        self.actions.append(text)
        return not self.dry_run

    def keep(self, relative: str, move: bool) -> None:
        source = self.vault / relative
        if not source.exists():
            return
        target = self.archive / relative
        if self.act(f"{'move' if move else 'copy'} {relative} -> {ARCHIVE / relative}"):
            target.parent.mkdir(parents=True, exist_ok=True)
            if move:
                shutil.move(str(source), str(target))
            elif source.is_dir():
                shutil.copytree(source, target, dirs_exist_ok=True)
            else:
                shutil.copy2(source, target)

    def run(self) -> list[str]:
        project = yaml.safe_load((self.vault / "project.yaml").read_text("utf-8")) or {}
        done = (self.archive / "done").is_file()
        if not done:
            for relative in ("project.yaml", "agents/pm/role.md", "agents/supervisor/role.md",
                             "knowledge/business-profile.md"):
                self.keep(relative, move=False)
            for relative in ("agents/pm/profiles", "agents/intake"):
                self.keep(relative, move=True)
            for folder in sorted((self.vault / ".agents" / "skills").glob("assistant-*")):
                self.keep(folder.relative_to(self.vault).as_posix(), move=True)
        self.project_yaml(project)
        caps = frozenset(CAPABILITIES.get(self.project_id, []))
        if self.act(f"install template skills for capabilities {sorted(caps)}"):
            installed = skills.install_templates(self.vault, caps)
            self.actions.append(f"  installed {len(installed)}: {', '.join(installed)}")
        self.own_skill_load_rules()
        self.charter(project)
        self.roles(project)
        if self.project_id == skills.SHARED_CONTEXT_HOME:
            self.shared_context()
        if not done and self.act("mark migration done"):
            self.archive.mkdir(parents=True, exist_ok=True)
            (self.archive / "done").write_text("round2 r1 vault migration\n", "utf-8")
        return self.actions

    def project_yaml(self, project: dict) -> None:
        path = self.vault / "project.yaml"
        additions = {"capabilities": CAPABILITIES.get(self.project_id, []), "delivery": "vault-files",
                     "share_with_kernel": "improvements"}
        missing = {key: value for key, value in additions.items() if key not in project}
        if missing and self.act(f"project.yaml add {missing}"):
            # the original (with its comments) is in the archive
            path.write_text(yaml.safe_dump({**project, **missing}, sort_keys=False, allow_unicode=True), "utf-8",
                            newline="\n")

    def own_skill_load_rules(self) -> None:
        for path in sorted((self.vault / ".agents" / "skills").glob("*/SKILL.md")):
            skill = skills.parse(path)
            if skill.load:
                continue
            if self.act(f"give {path.parent.name} a load rule (all PM and Supervisor turns)"):
                text = path.read_text("utf-8")
                text = re.sub(r"(\n  version: [^\n]*\n)", r"\1  load: {roles: [pm, supervisor]}\n", text, count=1)
                path.write_text(text, "utf-8", newline="\n")

    def charter(self, project: dict) -> None:
        path = self.vault / "knowledge" / "charter.md"
        if path.exists():
            return
        routing = project.get("routing") or {}
        profile_path = self.archive / "knowledge" / "business-profile.md"
        if not profile_path.is_file():
            profile_path = self.vault / "knowledge" / "business-profile.md"
        profile = profile_path.read_text("utf-8").strip() if profile_path.is_file() else ""
        bullet = lambda items: "\n".join(f"- {item}" for item in items or []) or "（待补充）"  # noqa: E731
        text = f"""# {project.get('display_name') or self.project_id} · 项目章程

> 项目 ID：`{self.project_id}`。由 Round 2 迁移根据原 project.yaml 和业务背景生成（2026-10-16），请在运营中补充完善。
> PM 和 Supervisor 每一轮都会读本章程；第 1、5、7 节同时用于 kernel 的项目路由。第 9 节由 Kernel 自动追加。

## 1 项目用途
{project.get('purpose') or '（待补充）'}

## 2 业务目标与成功标准
（待补充）

## 3 使用者与使用场景
（待补充）

## 4 组织与行业背景
{profile or '（待补充）'}

## 5 范围
- 负责：
{bullet(project.get('boundaries'))}
- 不负责：
{bullet(project.get('non_goals'))}

## 6 约束
（待补充）

## 7 路由信息
- 别名：{'、'.join(routing.get('aliases') or []) or '（待补充）'}
- 主题：{'、'.join(routing.get('topics') or []) or '（待补充）'}
- 典型请求：
{bullet(routing.get('typical_requests'))}
- 不负责的请求：
{bullet(routing.get('exclusions'))}

## 8 变更记录
- 2026-10-16 由 Round 2 迁移生成

## 9 背景信息存档
（需求文档获批时由 Kernel 追加：日期、任务、该次收集到的组织与行业事实）
"""
        if self.act("write knowledge/charter.md"):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, "utf-8", newline="\n")

    def roles(self, project: dict) -> None:
        values = {"project_id": self.project_id, "display_name": str(project.get("display_name") or self.project_id),
                  "purpose": str(project.get("purpose") or "")[:300]}
        for role in ("pm", "supervisor"):
            target = self.vault / "agents" / role / "role.md"
            template = fill((TEMPLATE / "agents" / role / "role.md").read_text("utf-8"), values)
            if target.is_file() and target.read_text("utf-8") == template:
                continue
            if self.act(f"write short agents/{role}/role.md (old one archived)"):
                target.write_text(template, "utf-8", newline="\n")

    def shared_context(self) -> None:
        folder = self.vault / "knowledge" / "shared"
        for name in skills.SHARED_FILES:
            if (folder / name).exists():
                continue
            if self.act(f"write knowledge/shared/{name} (first draft)"):
                folder.mkdir(parents=True, exist_ok=True)
                shutil.copy2(SHARED_TEMPLATE / name, folder / name)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("projects", nargs="+")
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    for project_id in args.projects:
        vault = args.data_root / "vaults" / project_id
        if not (vault / "project.yaml").is_file():
            raise SystemExit(f"no vault for {project_id}")
        print(f"== {project_id}{' (dry run)' if args.dry_run else ''}")
        for line in Migration(vault, project_id, args.dry_run).run():
            print("  " + line)


if __name__ == "__main__":
    main()
