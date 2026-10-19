"""ROUND5 R5.2: project history in git, the single project map, project credentials."""
from __future__ import annotations

import subprocess

import pytest

from assistant_kernel.core import credentials, projectgit, projectmap, vault_sync

from .sim import pm
from .test_delivery_executors import _publish, dispatcher  # noqa: F401  (a fixture)
from .test_gateway import gateway  # noqa: F401  (a fixture)
from .test_scenarios import execute_next, start_task


def log(path):
    return subprocess.run(["git", "log", "--format=%s"], cwd=path, capture_output=True, text=True).stdout.splitlines()


def test_an_empty_git_folder_is_not_a_repository(tmp_path):
    (tmp_path / ".git").mkdir()
    assert not projectgit.is_repo(tmp_path)
    assert projectgit.ensure_repo(tmp_path)
    assert projectgit.is_repo(tmp_path) and "runtime/" in (tmp_path / ".gitignore").read_text("utf-8")
    (tmp_path / "runtime").mkdir()
    (tmp_path / "runtime" / "credentials.json").write_text("{}", "utf-8")
    (tmp_path / "notes.md").write_text("x", "utf-8")
    assert projectgit.commit_all(tmp_path, "first")
    assert projectgit.commit_all(tmp_path, "nothing new") is None
    files = subprocess.run(["git", "ls-files"], cwd=tmp_path, capture_output=True, text=True).stdout.split()
    assert "notes.md" in files and not any(f.startswith("runtime/") for f in files)
    assert projectgit.pending_push(tmp_path)                                   # no remote yet


def test_a_release_commits_the_project_and_its_writable_folders(tmp_path):
    vault, external, readonly = tmp_path / "vault", tmp_path / "crypto", tmp_path / "docs"
    for folder in (vault, external, readonly):
        folder.mkdir()
        (folder / "a.txt").write_text("x", "utf-8")
    projectgit.ensure_repo(external, None)
    projectgit.ensure_repo(readonly, None)
    grants = [{"resource_key": "crypto", "policy_json": {"path": str(external), "access": "write"}},
              {"resource_key": "docs", "policy_json": {"path": str(readonly), "access": "read"}}]
    assert projectgit.commit_release(vault, "p1", grants, "发布：日报")[0] == []
    assert log(vault) == ["发布：日报"] and log(external) == ["发布：日报"] and log(readonly) == []
    assert [name for _, name in projectgit.repositories(vault, "p1", grants)] == ["project-p1", "p1-crypto"]


async def test_publish_leaves_a_snapshot_and_a_release_commit(kernel, sim, dispatcher, kernel_settings,  # noqa: F811
                                                             project_files):
    (project_files / "knowledge" / "edited-by-hand.md").parent.mkdir(exist_ok=True)
    (project_files / "knowledge" / "edited-by-hand.md").write_text("x", "utf-8")
    await _publish(kernel, sim, dispatcher, kernel_settings, None)
    subjects = log(project_files)
    assert subjects[0].startswith("发布：") and subjects[1].startswith("发布前快照")


async def test_push_failures_are_said_once_and_retried_later(kernel, kernel_settings, project_files, monkeypatch):
    projectgit.ensure_repo(project_files)
    projectgit.commit_all(project_files, "x")
    await kernel.ensure_project("p1")
    attempts = []

    def no_login(path, name):
        attempts.append(name)
        raise projectgit.GitError("gh auth login required")
    monkeypatch.setattr(projectgit, "push", no_login)
    await vault_sync._push_repositories(kernel, kernel_settings)
    await vault_sync._push_repositories(kernel, kernel_settings)               # within the hour: not retried
    assert attempts == ["project-p1"]
    async with kernel.db.tx() as c:
        told = await (await c.execute("""SELECT count(*) AS n FROM core.messages WHERE project_id='p1'
                                         AND body->>'text' LIKE '%推送到 GitHub 失败%'""")).fetchone()
    assert told["n"] == 1


def test_map_problems_and_lookup(tmp_path):
    (tmp_path / "product" / "entries").mkdir(parents=True)
    (tmp_path / "product" / "tests").mkdir()
    for name in ("product/entries/daily.py", "product/entries/__init__.py", "product/tests/test_daily.py"):
        (tmp_path / name).write_text('"""日报入口：汇总并通知。"""\n', "utf-8")
    required = projectmap.covered_files(tmp_path, tmp_path / "product")
    assert required == ["product/entries/daily.py"]
    exists = lambda p: (tmp_path / p).exists()  # noqa: E731
    assert projectmap.problems([], exists, required) == ["product/entries/daily.py 没有登记在地图里"]
    entries = projectmap.skeleton(tmp_path, required) + [{"path": "product/gone.py", "purpose": "x"},
                                                         {"path": "product/entries/", "kind": "folder", "purpose": "y"}]
    assert entries[0]["purpose"] == "日报入口：汇总并通知。"
    found = projectmap.problems(entries, exists, required)
    assert len(found) == 2 and any("gone.py" in p for p in found) and any("kind" in p for p in found)
    assert [e["path"] for e in projectmap.lookup(tmp_path, entries, "日报 通知")] == ["product/entries/daily.py"]
    with pytest.raises(ValueError):
        projectmap.parse("entries: []")


def test_credentials_are_kept_per_project(tmp_path):
    assert credentials.listing(tmp_path) == []
    credentials.put(tmp_path, "binance_api", {"key": "k", "secret": "s"}, service="币安", purpose="下单")
    credentials.put(tmp_path, "binance_api", {"key": "k2", "secret": "s2"})
    [item] = credentials.listing(tmp_path)
    assert item["service"] == "币安" and "value" not in item
    assert credentials.get(tmp_path, "binance_api")["value"]["key"] == "k2"
    assert oct(credentials.path(tmp_path).stat().st_mode)[-3:] == "600"
    with pytest.raises(ValueError):
        credentials.put(tmp_path, "Bad Key", "x")
    assert credentials.delete(tmp_path, "binance_api") and credentials.listing(tmp_path) == []


async def test_a_credential_question_goes_straight_to_the_user_and_into_the_store(kernel, sim, kernel_settings,
                                                                               project_files):
    task_id = await start_task(kernel, sim)
    await execute_next(kernel, sim, task_id, "requirements")
    question = {"id": "q1", "text": "请提供币安 API Key", "credential": {"key": "binance_api", "service": "币安",
                                                                        "purpose": "查询行情"}}
    sim.script("pm_turn", pm("needs_input", questions=[question]))
    await sim.drain()
    item = await sim.item("p1", task_id)
    assert item["kind"] == "questions" and item["content"]["questions"][0]["credential"]["key"] == "binance_api"
    assert "supervisor:evaluate" not in sim.history[-1:]                       # no relay by Supervisor
    await kernel.act("p1", item["item_id"], "submit", {"answers": {"q1": "AKIA-123"}})
    assert credentials.get(project_files, "binance_api")["value"] == "AKIA-123"
    run = await kernel.claim("w1")
    assert run["input"]["answers"]["q1"].startswith("（已保存为项目凭据 binance_api")
    async with kernel.db.tx() as c:
        leaked = await (await c.execute("SELECT count(*) AS n FROM core.messages WHERE body::text LIKE '%AKIA-123%'"))\
            .fetchone()
    assert leaked["n"] == 0


async def test_secrets_command(gateway, kernel_settings, project_files):  # noqa: F811
    credentials.put(project_files, "ib_account", "x", service="IB")
    assert "ib_account" in await gateway.secrets_command("p1", [])
    assert "确认" in await gateway.secrets_command("p1", ["delete", "ib_account"])
    assert "已删除" in await gateway.secrets_command("p1", ["delete", "ib_account", "确认"])
