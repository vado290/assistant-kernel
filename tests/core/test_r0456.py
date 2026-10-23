"""0.45.6: the kernel project can be public. The background archive lives in knowledge/background.md, ignored paths
never break a commit, a new installation gets its shared files from the template, a private project is never pushed,
and the GitHub account is the installation's own."""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

from assistant_kernel.core import projectgit, skills, vault_sync
from assistant_kernel.core.executors.common import charter
from assistant_kernel.core.executors.digest import shared_candidates

SCRIPT = Path(__file__).parents[2] / "scripts" / "r0456_migrate.py"


def load_script():
    spec = importlib.util.spec_from_file_location("r0456_migrate", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def log(path):
    return subprocess.run(["git", "log", "--format=%s"], cwd=path, capture_output=True, text=True).stdout.splitlines()


def test_agents_read_the_archive_as_the_charters_last_section(kernel_settings, project_files):
    knowledge = project_files / "knowledge"
    knowledge.mkdir(exist_ok=True)
    (knowledge / "charter.md").write_text("# 章程\n\n## 1 项目用途\n日报\n", "utf-8")
    assert charter(kernel_settings, "p1") == "# 章程\n\n## 1 项目用途\n日报"            # no archive yet: as it was
    assert vault_sync.append_archive(knowledge / "background.md", "archive:abc", "2026-10-20 · 日报", "- 公共：在上海")
    assert not vault_sync.append_archive(knowledge / "background.md", "archive:abc", "2026-10-20 · 日报", "- 公共：在上海")
    text = charter(kernel_settings, "p1")
    assert text.startswith("# 章程") and skills.section_text(text, vault_sync.ARCHIVE).count("公共：在上海") == 1
    assert "# 背景信息存档\n" not in text                                                # the file's own title is dropped
    found = shared_candidates(kernel_settings, ["p1", "p2"], exclude="p2")             # other projects' public facts
    assert "p1: 在上海" in found


def test_commits_leave_ignored_paths_out_instead_of_failing(tmp_path):
    projectgit.ensure_repo(tmp_path)
    (tmp_path / ".gitignore").write_text("knowledge/user-profile.md\nknowledge/archive/\n", "utf-8")
    (tmp_path / "knowledge" / "archive").mkdir(parents=True)
    for name in ("charter.md", "user-profile.md", "archive/old.md"):
        (tmp_path / "knowledge" / name).write_text("x", "utf-8")
    paths = ["knowledge/charter.md", "knowledge/user-profile.md", "knowledge/archive"]
    assert projectgit.ignored(tmp_path, paths) == {"knowledge/user-profile.md", "knowledge/archive"}
    assert projectgit.commit_paths(tmp_path, paths, "存档")
    tracked = projectgit.git("ls-files", cwd=tmp_path).splitlines()
    assert tracked == ["knowledge/charter.md"]
    assert projectgit.commit_paths(tmp_path, ["knowledge/user-profile.md"], "只有被忽略的") is None


def test_a_new_installation_gets_the_shared_files_from_the_template(kernel_settings):
    assert vault_sync.seed_shared(kernel_settings.data_root) == []                    # no kernel project: nothing
    shared = kernel_settings.data_root / "vaults" / "kernel" / "knowledge" / "shared"
    shared.mkdir(parents=True)
    (shared / "environment.md").write_text("# 我自己的环境\n", "utf-8")
    added = vault_sync.seed_shared(kernel_settings.data_root)
    assert sorted(added) == ["conventions.md", "organization.md"]
    assert (shared / "environment.md").read_text("utf-8") == "# 我自己的环境\n"       # never overwritten
    assert "Shared background" in (shared / "organization.md").read_text("utf-8")
    assert vault_sync.seed_shared(kernel_settings.data_root) == []


async def test_a_private_project_commits_but_is_never_pushed(kernel, kernel_settings, project_files, monkeypatch):
    projectgit.ensure_repo(project_files)
    projectgit.commit_all(project_files, "x")
    pushed = []
    monkeypatch.setattr(projectgit, "push", lambda path, name: pushed.append(name))
    async with kernel.db.tx() as c:
        await c.execute("""UPDATE core.projects SET settings = settings || '{"git_push": false}' WHERE project_id='p1'""")
    assert await vault_sync._push_repositories(kernel, kernel_settings) == 0
    assert pushed == []
    async with kernel.db.tx() as c:
        await c.execute("""UPDATE core.projects SET settings = settings - 'git_push' WHERE project_id='p1'""")
    await vault_sync._push_repositories(kernel, kernel_settings)
    assert pushed == ["project-p1"]


def test_the_github_account_is_the_installations_own(monkeypatch):
    monkeypatch.setenv("CORE_GITHUB_OWNER", "someone")
    assert projectgit.owner() == "someone"
    monkeypatch.delenv("CORE_GITHUB_OWNER")
    monkeypatch.setattr(projectgit, "run", lambda *args, **kwargs: "my-login")
    assert projectgit.owner() == "my-login"

    def no_login(*args, **kwargs):
        raise projectgit.GitError("gh auth login required")
    monkeypatch.setattr(projectgit, "run", no_login)
    with pytest.raises(projectgit.GitError):                                          # no fixed fallback account
        projectgit.owner()


CHARTER = ("# 便利店 · 项目章程\n\n> 第 1、5、7 节用于路由。第 9 节由 Kernel 自动追加。\n\n## 4 组织与行业背景\n（待补充）\n\n## 7 路由信息\n- 别名：日报\n\n## 9 背景信息存档\n\n"
           "### 2026-10-16 · 日报（任务 aaaa） <!-- archive:111 -->\n\n- 公共：在上海\n\n"
           "### 2026-10-17 · 周报（任务 bbbb） <!-- archive:222 -->\n\n- 20 家门店\n")


def test_migration_moves_the_archive_and_makes_the_kernel_project_public(tmp_path):
    migrate = load_script()
    rest, entries = migrate.split_charter(CHARTER)
    assert "背景信息存档" not in rest and rest.rstrip().endswith("- 别名：日报")
    assert entries.count("### ") == 2
    once = migrate.with_entries(None, entries)
    assert once.startswith("# Background archive") and migrate.with_entries(once, entries) == once   # repeatable
    vault = tmp_path / "kernel"
    (vault / "knowledge").mkdir(parents=True)
    (vault / "knowledge" / "charter.md").write_text(CHARTER, "utf-8")
    for role, title in migrate.ROLE_SECTIONS.items():
        (vault / "agents" / role).mkdir(parents=True)
        (vault / "agents" / role / "role.md").write_text(f"# 角色\n\n## {title}\n（没有特别要求时保持为空。）\n", "utf-8")
    projectgit.ensure_repo(vault)
    projectgit.commit_all(vault, "start")
    assert migrate.migrate_vault(vault, "kernel", apply=False) and log(vault) == ["start"]   # a dry run writes nothing
    changed = migrate.migrate_vault(vault, "kernel", apply=True)
    assert set(changed) == {"knowledge/background.md", "knowledge/charter.md", "agents/pm/role.md",
                            "agents/supervisor/role.md"}
    text = (vault / "knowledge" / "charter.md").read_text("utf-8")
    assert "见公共背景" in text and "第 9 节" not in text and "knowledge/background.md" in text
    assert "## 9 背景信息存档" not in text
    assert "20 家门店" in (vault / "knowledge" / "background.md").read_text("utf-8")
    assert migrate.PUBLIC_RULE in (vault / "agents" / "pm" / "role.md").read_text("utf-8")
    assert "保持为空" not in (vault / "agents" / "pm" / "role.md").read_text("utf-8")
    assert migrate.migrate_vault(vault, "kernel", apply=True) == []                       # nothing left to do
    other = tmp_path / "shop"
    (other / "knowledge").mkdir(parents=True)
    (other / "knowledge" / "charter.md").write_text(CHARTER, "utf-8")
    assert set(migrate.migrate_vault(other, "shop", apply=True)) == {"knowledge/background.md", "knowledge/charter.md"}
    assert "（待补充）" in (other / "knowledge" / "charter.md").read_text("utf-8")      # only kernel points elsewhere
