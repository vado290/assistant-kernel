"""R4a: code delivery — task worktree, Kernel commits, tests on the bound commit, fast-forward publish, cleanup."""
from __future__ import annotations

import subprocess

import pytest
import yaml

from assistant_kernel.core import gitdelivery, rules
from assistant_kernel.core.dispatch import Dispatcher
from assistant_kernel.core.executors.delivery import execute_publish, execute_validation
from assistant_kernel.core.executors.pm import execute_pm
from assistant_kernel.core.vault_sync import sync_vaults

from .test_dispatch import Scripts, settle

TEST = "python3 check_hello.py"
CHECK = "import sys, pathlib\nsys.exit(0 if pathlib.Path('hello.py').read_text().startswith('print') else 1)\n"


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path, project_files):
    path = tmp_path / "code"
    gitdelivery.init_repository(path)
    (path / "check_hello.py").write_text(CHECK, "utf-8")
    (path / "hello.py").write_text("placeholder\n", "utf-8")
    git(path, "add", "-A")
    git(path, "-c", "user.name=u", "-c", "user.email=u@x", "commit", "-qm", "test script")
    (project_files / "project.yaml").write_text(yaml.safe_dump({
        "schema_version": "project.v2", "project_id": "p1", "layout_version": 2, "delivery": "git-repo",
        "repository": str(path), "main_branch": "main",
        "validation": {"command": TEST, "timeout_seconds": 60}}), "utf-8")
    return path


@pytest.fixture
def dispatcher(kernel, kernel_settings, fake_harness, repo):
    scripts = Scripts()
    executors = {**scripts.executors(), "pm_turn": execute_pm, "validation": execute_validation,
                 "publish": execute_publish}
    d = Dispatcher(kernel, executors, services={"settings": kernel_settings, "harness": fake_harness})
    d.scripts = scripts
    return d


def writing(files, reply):
    """A scripted build turn that edits files in its working directory (the task worktree)."""
    def turn(request):
        for name, text in files.items():
            (request.cwd / name).write_text(text, "utf-8")
        return reply
    return turn


async def to_build(kernel, sim, d, kernel_settings):
    from .test_pm_executor import editing
    task_id = (await kernel.receive("p1", role="pm", text="加一个 hello 脚本"))["task_id"]
    d.scripts.add("supervisor:evaluate", {"decision": "propose_next", "target_stage": "requirements",
                                          "instruction": "写需求", "track": "quick"})
    await settle(d)
    await kernel.act("p1", (await sim.item("p1", task_id))["item_id"], "execute")
    return task_id, editing


async def test_code_delivery_end_to_end(kernel, sim, dispatcher, fake_harness, kernel_settings, repo):
    task_id, editing = await to_build(kernel, sim, dispatcher, kernel_settings)
    fake_harness.script("pm", editing(kernel_settings, {"outcome": "completed", "summary": "需求好了", "questions": [],
                                                        "document": {"kind": "task_contract"}},
                                      content="# 需求\n## 简要方案\n新增 hello.py\n## 背景信息存档\n无\n## 用户画像观察\n无"))
    dispatcher.scripts.add("supervisor:evaluate", {"decision": "propose_next", "target_stage": "build",
                                                    "instruction": "实施", "track": "quick"})
    await settle(dispatcher)
    await kernel.act("p1", (await sim.item("p1", task_id))["item_id"], "execute")

    report = {"outcome": "ready_for_validation", "summary": "已实施", "questions": [],
              "document": {"kind": "implementation_report", "markdown": "# 实施报告\n新增 hello.py"}}
    fake_harness.script("pm", writing({"hello.py": "print('hello')\n"}, report))
    dispatcher.scripts.add("supervisor:evaluate", {"decision": "propose_next", "target_stage": "release",
                                                    "instruction": "申请发布"})
    await settle(dispatcher)
    call = fake_harness.calls[-1]
    worktree = gitdelivery.worktree_path(gitdelivery.config(kernel_settings, "p1"), str(task_id))
    assert call.cwd == worktree and "write_candidate_file" not in call.scoped_tools
    assert "不要运行 git commit" in call.prompt and str(worktree) in call.prompt
    task = await sim.task(task_id)
    commit = task["documents"]["implementation_report"]["bundle_sha"]
    assert commit.startswith("git:") and git(repo, "rev-parse", gitdelivery.branch(str(task_id))) == commit[4:]
    assert task["documents"]["validation"]["passed"], task["documents"]["validation"]
    assert "测试命令" in task["documents"]["validation"]["report"]

    item = await sim.item("p1", task_id)
    assert item["content"]["decision"] == "release"
    await kernel.act("p1", item["item_id"], "approve")
    await settle(dispatcher)
    assert (await sim.task(task_id))["state"] == "completed"
    assert git(repo, "rev-parse", "main") == commit[4:]                       # main fast-forwarded
    assert any("hello.py" in m["body"]["text"] for m in await sim.outbound("p1"))
    await sync_vaults(kernel, kernel_settings)
    assert not worktree.exists() and not git(repo, "branch", "--list", gitdelivery.branch(str(task_id)))


def test_validation_runs_the_tests_on_the_commit(tmp_path, kernel_settings, repo):
    cfg = gitdelivery.config(kernel_settings, "p1")
    path = gitdelivery.ensure_worktree(cfg, "t-fail")
    (path / "hello.py").write_text("oops\n", "utf-8")
    commit = gitdelivery.seal(cfg, "t-fail", "坏的实现")
    passed, report = gitdelivery.validate(cfg, "t-fail", commit[4:])
    assert not passed and "退出码 1" in report
    (path / "hello.py").write_text("print('x')\n", "utf-8")                  # changed after sealing
    assert gitdelivery.validate(cfg, "t-fail", commit[4:])[0] is False
    assert gitdelivery.seal(cfg, "t-none", "没有改动") == ""                  # nothing changed → no-files marker


def test_fast_forward_refuses_when_main_moved(kernel_settings, repo):
    cfg = gitdelivery.config(kernel_settings, "p1")
    path = gitdelivery.ensure_worktree(cfg, "t-late")
    (path / "a.txt").write_text("a", "utf-8")
    commit = gitdelivery.seal(cfg, "t-late", "a")[4:]
    (repo / "b.txt").write_text("b", "utf-8")                                 # someone commits on main meanwhile
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=u", "-c", "user.email=u@x", "commit", "-qm", "b")
    with pytest.raises(gitdelivery.GitError, match="不能快进"):
        gitdelivery.fast_forward(cfg, commit)


def test_new_code_projects_get_a_repository(kernel_settings):
    from assistant_kernel.core.executors.bootstrap import write_project_files
    from .test_bootstrap import CHARTER
    spec = rules.charter_spec(CHARTER.replace("delivery: vault-files", "delivery: git-repo"))
    assert "git_delivery" in spec["capabilities"]
    write_project_files(kernel_settings, spec)
    vault = kernel_settings.data_root / "vaults" / "sales-report"
    cfg = gitdelivery.config(kernel_settings, "sales-report")
    assert cfg["repo"] == vault / "repo" and git(cfg["repo"], "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert (vault / ".agents" / "skills" / "assistant-git-delivery").is_dir()


def test_tests_get_only_the_allowed_environment(kernel_settings, repo, project_files, monkeypatch):
    data = yaml.safe_load((project_files / "project.yaml").read_text("utf-8"))
    data["validation"] = {"command": "python3 -c \"import os,sys; sys.exit(0 if os.environ.get('ASSISTANT_POSTGRES_HOST')"
                                     " and not os.environ.get('SECRET_TOKEN') else 1)\"",
                          "pass_env": ["ASSISTANT_POSTGRES_*"]}
    data["release_note"] = "kernel {commit} 已合并到 main，需要部署时请执行 deploy.ps1"
    (project_files / "project.yaml").write_text(yaml.safe_dump(data), "utf-8")
    monkeypatch.setenv("ASSISTANT_POSTGRES_HOST", "db")
    monkeypatch.setenv("SECRET_TOKEN", "x")
    cfg = gitdelivery.config(kernel_settings, "p1")
    path = gitdelivery.ensure_worktree(cfg, "t-env")
    (path / "hello.py").write_text("print('x')\n", "utf-8")
    commit = gitdelivery.seal(cfg, "t-env", "env")
    assert gitdelivery.validate(cfg, "t-env", commit[4:])[0] is True
    assert cfg["release_note"].format(commit=commit[4:16]).startswith(f"kernel {commit[4:16]}")
