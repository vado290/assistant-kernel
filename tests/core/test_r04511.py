"""0.45.11 (analysis 2026-10-22, M1): commits made outside tasks join the project's change stream, once, dated by
the commit; a task's release commit is left to the task's own record."""
from __future__ import annotations

from datetime import datetime

from assistant_kernel.core import projectgit, vault_sync


def commit(path, name, message, file, text):
    (path / file).write_text(text, "utf-8")
    projectgit.git("add", "-A", cwd=path)
    projectgit.git("-c", f"user.name={name}", "-c", f"user.email={name}@x", "commit", "-q", "-m", message, cwd=path)
    return projectgit.git("rev-parse", "HEAD", cwd=path)


async def test_commits_outside_tasks_are_imported_once(kernel, kernel_settings, project_files, monkeypatch):
    await kernel.ensure_project("p1")
    projectgit.ensure_repo(project_files)
    start = projectgit.commit_all(project_files, "start")                      # Kernel's own commit
    manual = commit(project_files, "someone", "手工补充章程\n\n用户在主机上直接改的", "notes.md", "a")
    system = commit(project_files, "assistant-kernel", "同步技能模板（Kernel 9.9.9）", "notes.md", "b")
    commit(project_files, "assistant-kernel", "发布：日报\n\n任务 x", "notes.md", "c")      # the task records this one
    monkeypatch.setattr(vault_sync, "_last_import", 0.0)
    assert await vault_sync._import_commits(kernel, kernel_settings) == 3
    rows = {r["source_ref"]: r for r in await kernel.iterations("p1", days=30)}
    assert set(rows) == {f"git:project:{start}", f"git:project:{manual}", f"git:project:{system}"}
    hand = rows[f"git:project:{manual}"]
    assert hand["kind"] == "imported" and hand["source"] == "manual" and "用户在主机上直接改的" in hand["reason"]
    assert hand["facts"]["files"] == ["workspace://project/notes.md"] and rows[f"git:project:{system}"]["source"] == "system"
    date = projectgit.git("log", "-1", "--format=%cI", manual, cwd=project_files)
    assert hand["created_at"] == datetime.fromisoformat(date)
    monkeypatch.setattr(vault_sync, "_last_import", 0.0)
    assert await vault_sync._import_commits(kernel, kernel_settings) == 0                   # each commit once
