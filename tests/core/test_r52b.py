"""ROUND5 R5.2 fixes (0.45.2): the map's project: entries and validation with candidates, the Kernel-side skill
rollout, release commits linked to the change record, the tools over real MCP, and the kernel map guard."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from assistant_kernel.core import credentials, projectgit, projectmap, skills, vault_sync
from assistant_kernel.core.executors.bootstrap import first_commit
from assistant_kernel.core.executors.common import workspace_snapshot
from assistant_kernel.core.executors.delivery import check_map, check_repo_map

from .sim import next_step
from .test_scoped_tools_core import payload, private_config

REPO = Path(__file__).parents[2]
MAP = "schema_version: project-map.v1\nentries:\n"


def log(path):
    return subprocess.run(["git", "log", "--format=%s"], cwd=path, capture_output=True, text=True).stdout.splitlines()


def candidates(vault: Path, task_id: str, files: dict[str, str | None]) -> None:
    """A task's candidate area as the workspace tools leave it (None: delete)."""
    root = vault / "runs" / task_id / "candidate"
    listing = []
    for path, text in files.items():
        listing.append({"target_uri": f"workspace://project/{path}", "operation": "delete" if text is None else "create"})
        if text is not None:
            (root / "project" / path).parent.mkdir(parents=True, exist_ok=True)
            (root / "project" / path).write_text(text, "utf-8")
    root.mkdir(parents=True, exist_ok=True)
    (root / "manifest.json").write_text(json.dumps({"files": listing}), "utf-8")


def test_the_kernel_map_lists_every_kernel_python_file():
    """Guard for direct kernel development: deploy runs the tests, so a forgotten map entry stops the deploy."""
    entries = projectmap.load(REPO / "docs" / "map.yaml")
    exists = lambda p: p.startswith("project:") or (REPO / p).exists()  # noqa: E731
    assert projectmap.problems(entries, exists, projectmap.covered_files(REPO, REPO)) == []


def test_map_validation_with_candidate_files(kernel_settings, project_files):
    vault = project_files
    assert check_map(kernel_settings, "p1", "t1") == []                         # no product, no map: nothing to check
    candidates(vault, "t2", {"product/entries/daily.py": "x"})
    assert check_map(kernel_settings, "p1", "t2") == ["product/entries/daily.py 没有登记在地图里"]
    candidates(vault, "t3", {"product/entries/daily.py": "x",
                             "knowledge/map.yaml": MAP + "  - {path: product/entries/daily.py, purpose: 日报}\n"
                                                         "  - {path: project:knowledge/charter.md, purpose: 章程}\n"})
    (vault / "knowledge").mkdir(exist_ok=True)
    (vault / "knowledge" / "charter.md").write_text("x", "utf-8")
    assert check_map(kernel_settings, "p1", "t3") == []
    (vault / "product" / "entries").mkdir(parents=True)
    (vault / "product" / "entries" / "daily.py").write_text("x", "utf-8")
    (vault / "knowledge" / "map.yaml").write_text(MAP + "  - {path: product/entries/daily.py, purpose: 日报}\n", "utf-8")
    candidates(vault, "t4", {"product/entries/daily.py": None})                # deleted, still in the map
    assert check_map(kernel_settings, "p1", "t4") == ["地图 product/entries/daily.py 指向的文件不存在"]
    candidates(vault, "t5", {"knowledge/map.yaml": "entries: ["})
    assert len(check_map(kernel_settings, "p1", "t5")) == 1


def test_a_repository_map_can_name_project_folder_files(tmp_path):
    vault, repo = tmp_path / "vault", tmp_path / "repo"
    (vault / "knowledge").mkdir(parents=True)
    (vault / "knowledge" / "charter.md").write_text("x", "utf-8")
    worktree = repo / "workspaces" / "t1"
    (worktree / "docs").mkdir(parents=True)
    (worktree / "app.py").write_text("x", "utf-8")
    code = {"workspaces": repo / "workspaces", "map_path": "docs/map.yaml"}
    (worktree / "docs" / "map.yaml").write_text(MAP + "  - {path: app.py, purpose: 入口}\n"
                                                      "  - {path: project:knowledge/charter.md, purpose: 章程}\n", "utf-8")
    assert check_repo_map(code, "t1", vault) == []
    (vault / "knowledge" / "charter.md").unlink()
    assert check_repo_map(code, "t1", vault) == ["地图 project:knowledge/charter.md 指向的文件不存在"]


def test_rollout_gives_new_skills_refreshes_unedited_and_respects_removals(tmp_path):
    vault = tmp_path
    skills.install_templates(vault, frozenset())
    credentials_skill = skills.agent_skill_dir(vault, "pm") / "assistant-credentials"
    import shutil
    shutil.rmtree(credentials_skill)                                            # missing, never offered (old project)
    old = skills.agent_skill_dir(vault, "pm") / "assistant-implementation" / "SKILL.md"
    old.write_text(old.read_text("utf-8").replace('\n  version: "', '\n  version: "0.', 1), "utf-8")
    edited = skills.agent_skill_dir(vault, "pm") / "assistant-requirements" / "SKILL.md"
    edited.write_text(edited.read_text("utf-8").replace('\n  version: "', '\n  version: "0.', 1) + "\n本项目补充。\n", "utf-8")
    result = skills.sync_templates(vault, frozenset(), "9.9.9")
    assert "pm/assistant-credentials" in result["installed"] and "assistant-implementation" in result["refreshed"]
    assert result["kept"] == ["assistant-requirements"] and "本项目补充" in edited.read_text("utf-8")
    shutil.rmtree(credentials_skill)                                            # the project removes it on purpose
    again = skills.sync_templates(vault, frozenset(), "9.9.10")
    assert again["installed"] == [] and not credentials_skill.exists()
    assert skills.offered(vault)["kernel_version"] == "9.9.10"


async def test_rollout_runs_once_per_version_and_commits_only_skills(kernel, kernel_settings, project_files):
    await kernel.ensure_project("p1")
    shutil_target = skills.agent_skill_dir(project_files, "pm") / "assistant-credentials"
    import shutil
    shutil.rmtree(shutil_target)                                                # an older project without it
    projectgit.ensure_repo(project_files)
    projectgit.commit_all(project_files, "start")
    (project_files / "notes.md").write_text("an unrelated edit", "utf-8")
    assert await vault_sync._sync_templates(kernel, kernel_settings) >= 1
    assert shutil_target.exists() and log(project_files)[0].startswith("同步技能模板")
    assert "notes.md" in projectgit.git("status", "--porcelain", cwd=project_files)   # not swept into that commit
    assert await vault_sync._sync_templates(kernel, kernel_settings) == 0          # same Kernel version: once


def test_release_commits_name_the_task_and_carry_external_changes(tmp_path):
    vault, external = tmp_path / "vault", tmp_path / "crypto"
    for folder in (vault, external):
        folder.mkdir()
        (folder / "a.txt").write_text("x", "utf-8")
    projectgit.ensure_repo(external, None)
    grants = [{"resource_key": "crypto", "policy_json": {"path": str(external), "access": "write"}}]
    (external / "by-hand.txt").write_text("before the task", "utf-8")
    assert projectgit.snapshot_external(vault, "p1", grants, "任务开始实施前的快照") == []
    (external / "strategy.py").write_text("the task's change", "utf-8")     # the PM edits it directly in build
    problems, _ = projectgit.commit_release(vault, "p1", grants, "发布前快照", external=False)
    assert problems == [] and log(external) == ["任务开始实施前的快照"]           # not swept into a snapshot
    (vault / "b.txt").write_text("published by the release", "utf-8")
    problems, commits = projectgit.commit_release(vault, "p1", grants, "发布：日报")
    by_repo = {c["repo"]: c for c in commits}
    assert by_repo["crypto"]["files"] == ["strategy.py"] and by_repo["project"]["files"] == ["b.txt"]


async def test_the_change_record_keeps_the_release_commits(kernel, sim):
    from assistant_kernel.core import machine
    task_id = (await kernel.receive("p1", role="pm", text="改策略"))["task_id"]
    async with kernel.db.tx() as c:
        task = await machine.load_task(c, task_id)
        await machine.close(c, task, "completed", "published", published_files=["workspace://project/a.txt"],
                            commits=[{"repo": "project", "commit": "abc", "files": ["a.txt"]},
                                     {"repo": "crypto", "commit": "def", "files": ["strategy.py"]}])
    [row] = await kernel.iterations("p1")
    assert row["facts"]["commits"] == [{"repo": "project", "commit": "abc"}, {"repo": "crypto", "commit": "def"}]
    assert "grant://crypto/strategy.py" in row["facts"]["files"]


def test_a_new_project_starts_with_a_commit(kernel_settings):
    vault = kernel_settings.data_root / "vaults" / "fresh"
    (vault / "knowledge").mkdir(parents=True)
    (vault / "knowledge" / "charter.md").write_text("x", "utf-8")
    first_commit(kernel_settings, {"project_id": "fresh", "display_name": "新项目"})
    assert log(vault) == ["创建项目 新项目"]


async def test_map_and_credential_tools_over_mcp(kernel, sim, kernel_settings, project_files, tmp_path):
    (project_files / "knowledge").mkdir(exist_ok=True)
    (project_files / "knowledge" / "map.yaml").write_text(
        MAP + "  - {path: knowledge/, kind: directory, purpose: 项目知识：章程与用户画像}\n", "utf-8")
    credentials.put(project_files, "binance_api", "k-1", service="币安")
    task_id = (await kernel.receive("p1", role="pm", text="查地图"))["task_id"]
    sim.script("supervisor:evaluate", next_step("requirements"))
    await sim.drain()
    await kernel.act("p1", (await sim.item("p1"))["item_id"], "execute")
    run = await kernel.claim("w1")
    snapshot = workspace_snapshot(kernel_settings, "p1", task_id, "probe", candidates=False)
    tools = ("get_authorized_workspace", "map_lookup", "list_credentials", "get_credential")
    env = private_config(kernel_settings, run, task_id, snapshot, tmp_path, tools=tools, stage="requirements")
    server = StdioServerParameters(command=sys.executable, args=["-m", "assistant_kernel.core.tools"], env=env)
    async with stdio_client(server) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        await session.call_tool("get_authorized_workspace", {})
        found = payload(await session.call_tool("map_lookup", {"query": "章程"}))
        assert found["count"] == 1 and found["entries"][0]["path"] == "knowledge/"
        listed = payload(await session.call_tool("list_credentials", {}))
        assert listed["items"][0]["key"] == "binance_api" and "value" not in listed["items"][0]
        assert payload(await session.call_tool("get_credential", {"key": "binance_api"}))["value"] == "k-1"
