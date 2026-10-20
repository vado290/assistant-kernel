"""0.45.6 one-off: make the kernel project shareable (public) and keep the trading project off GitHub.

- Every project: the charter's "背景信息存档" section moves to knowledge/background.md, where Kernel appends from now
  on; the charter keeps only the project's definition. The changed files are committed (an ignored file is skipped).
- The kernel project: the PM and Supervisor roles get the rule that the project is public, and the charter's
  "组织与行业背景" points to the shared background, which every installation keeps for itself.
- trading-risk-bot: settings git_push = false (it still commits locally; nothing is pushed).
Refuses while a delivery task is active: its candidates may still carry the old charter.

Run with the prod venv after deploying 0.45.6:
  python scripts/r0456_migrate.py [--apply]
"""
from __future__ import annotations

import argparse
from pathlib import Path

from assistant_kernel.config import KernelSettings
from assistant_kernel.core import projectgit, rules, skills
from assistant_kernel.core.vault_sync import ARCHIVE, BACKGROUND_HEADER

PLACEHOLDER = "（需求文档获批时"
OLD_NOTE = "第 9 节由 Kernel 自动追加。"   # the header note of the charters Round 2 generated
NEW_NOTE = "背景信息存档在 `knowledge/background.md`，由 Kernel 自动追加。"
PRIVATE_PROJECTS = ("trading-risk-bot",)
PUBLIC_RULE = ("- 本项目（kernel）是公开的：它的文件会推送到公开的 GitHub 仓库，任何人都能看到。章程、角色和技能只写通用内容；"
               "用户本人、组织、机器环境等具体事实写进公共背景（knowledge/shared/，经卡片由用户确认）或背景信息存档"
               "（knowledge/background.md），这两处不随本项目公开。")
ROLE_SECTIONS = {"pm": "本项目对 PM 的特别要求", "supervisor": "本项目对 Supervisor 的特别要求"}
KERNEL_ORGANIZATION = "见公共背景（knowledge/shared/organization.md）：每个安装各自维护，不随本项目公开。"


def split_charter(text: str) -> tuple[str, str]:
    """(the charter without its archive section, the archive's entries; '' when it has none)."""
    lines = text.splitlines()
    bounds = skills.section_bounds(lines, ARCHIVE)
    if bounds is None:
        return text, ""
    entries = "\n".join(line for line in lines[bounds[1]:bounds[2]] if not line.startswith(PLACEHOLDER)).strip()
    rest = "\n".join(lines[:bounds[0]] + lines[bounds[2]:]).rstrip() + "\n"
    return rest, entries


def with_entries(current: str | None, entries: str) -> str:
    """knowledge/background.md holding `entries` (entries already there, by their marker line, are not repeated)."""
    text = current if current is not None else BACKGROUND_HEADER
    new = [block for block in ("\n" + entries).split("\n### ") if block.strip()]
    for block in new:
        block = "### " + block.strip()
        if block.splitlines()[0] not in text:
            text = text.rstrip() + "\n\n" + block + "\n"
    return text


def add_rule(text: str, title: str) -> str:
    body = skills.section_text(text, title)
    if PUBLIC_RULE in body:
        return text
    kept = "" if body.startswith("（") else body
    return skills.replace_section(text, title, (kept + "\n" + PUBLIC_RULE).strip())


def migrate_vault(vault: Path, project_id: str, apply: bool) -> list[str]:
    changed, knowledge = [], vault / "knowledge"
    charter = knowledge / "charter.md"
    if charter.is_file():
        old = charter.read_text("utf-8")
        new, entries = split_charter(old)
        new = new.replace(OLD_NOTE, NEW_NOTE)
        if project_id == skills.SHARED_CONTEXT_HOME and skills.section_text(new, "组织与行业背景").startswith("（"):
            new = skills.replace_section(new, "组织与行业背景", KERNEL_ORGANIZATION)
        if entries:
            background = knowledge / "background.md"
            text = with_entries(background.read_text("utf-8") if background.is_file() else None, entries)
            print(f"  背景信息存档 → knowledge/background.md：{entries.count('### ') or 1} 条")
            if apply:
                background.write_text(text, "utf-8", newline="\n")
            changed.append("knowledge/background.md")
        if new != old:
            print("  章程：去掉背景信息存档一节" + ("，第 4 节指向公共背景" if project_id == skills.SHARED_CONTEXT_HOME else ""))
            if apply:
                charter.write_text(new, "utf-8", newline="\n")
            changed.append("knowledge/charter.md")
    if project_id == skills.SHARED_CONTEXT_HOME:
        for role, title in ROLE_SECTIONS.items():
            path = vault / "agents" / role / "role.md"
            if path.is_file() and (text := add_rule(path.read_text("utf-8"), title)) != path.read_text("utf-8"):
                print(f"  {role}/role.md：加上“本项目公开”的规则")
                if apply:
                    path.write_text(text, "utf-8", newline="\n")
                changed.append(f"agents/{role}/role.md")
    return changed


def main() -> None:
    import psycopg
    from assistant_kernel.core import migrate as core_migrate
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="write the changes (default: only show them)")
    options = parser.parse_args()
    settings = KernelSettings.from_environment()
    with psycopg.connect(**core_migrate.connection_args(settings)) as connection:
        active = connection.execute("SELECT project_id, task_id FROM core.tasks WHERE kind='delivery' AND state = ANY(%s)",
                                    (list(rules.ACTIVE_STATES),)).fetchall()
        if active:
            raise SystemExit(f"有进行中的交付任务，先等它结束再迁移：{active}")
        for vault in sorted(p for p in (settings.data_root / "vaults").iterdir() if (p / "project.yaml").is_file()):
            print(f"\n项目 {vault.name}")
            changed = migrate_vault(vault, vault.name, options.apply)
            if changed and options.apply:
                present = [p for p in dict.fromkeys(changed) if (vault / p).exists()]
                commit = projectgit.commit_paths(vault, present, "背景信息存档移到 knowledge/background.md（Kernel 0.45.6）")
                print(f"  提交：{commit or '无（文件被忽略或没有变化）'}")
            elif not changed:
                print("  无需改动")
        print(f"\n关闭推送：{', '.join(PRIVATE_PROJECTS)}（本地照常提交）")
        if options.apply:
            connection.execute("""UPDATE core.projects SET settings = coalesce(settings, '{}'::jsonb) || '{"git_push": false}'
                                  WHERE project_id = ANY(%s)""", (list(PRIVATE_PROJECTS),))
    print("\n已执行。" if options.apply else "\n只是预演；确认无误后加 --apply 执行。")


if __name__ == "__main__":
    main()
