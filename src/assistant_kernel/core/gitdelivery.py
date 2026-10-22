"""Code delivery (docs/ROUND2_PLAN.md R4a): a project whose deliverable is a git repository.

project.yaml:  delivery: git-repo
               repository: repo            # absolute container path, or relative to the vault (default: repo)
               main_branch: main           # default main
               validation: {command: "python3 -m pytest -q", timeout_seconds: 1800}   # optional

- build: Kernel gives each task its own worktree (workspaces/<project>/<task>, branch task/<task8>) cut from main;
  the PM edits files there and Kernel commits them at the end of each turn (the PM never runs git);
- the implementation report is bound to the commit ("git:<sha>"); validation runs the project's test command on it;
- publish fast-forwards main to that commit by ref, never touching a working copy that has local changes;
- when the task closes, the worktree is removed (the branch too, once merged).
"""
from __future__ import annotations

import fnmatch
import os
import subprocess
from pathlib import Path
from typing import Any

import yaml

from assistant_kernel.storage import ProjectRuntimeManager

PREFIX = "git:"
IDENTITY = ("-c", "user.name=assistant-pm", "-c", "user.email=assistant-pm@kernel.local")
PASSED_ENV = ("PATH", "HOME", "LANG", "LC_ALL")


class GitError(RuntimeError):
    pass


def git(*args: str, cwd: Path, timeout: int = 120) -> str:
    done = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=timeout)
    if done.returncode != 0:
        raise GitError(f"git {' '.join(args[:3])} 失败：{(done.stderr or done.stdout).strip()[-600:]}")
    return done.stdout.strip()


def config(settings, project_id: str) -> dict[str, Any] | None:
    """The project's code-delivery settings; None for file delivery."""
    vault = ProjectRuntimeManager(settings.data_root).ensure(project_id).vault_root
    path = vault / "project.yaml"
    data = (yaml.safe_load(path.read_text("utf-8")) or {}) if path.is_file() else {}
    if data.get("delivery") != "git-repo":
        return None
    repository = Path(str(data.get("repository") or "repo"))
    validation = data.get("validation") if isinstance(data.get("validation"), dict) else {}
    return {"project_id": project_id, "repo": repository if repository.is_absolute() else vault / repository,
            "main": str(data.get("main_branch") or "main"),
            "workspaces": settings.data_root / "workspaces" / project_id,
            "test_command": str(validation.get("command") or "").strip(),
            "test_timeout": int(validation.get("timeout_seconds") or 1800),
            "pass_env": [str(p) for p in validation.get("pass_env") or []],   # e.g. POSTGRES_* for kernel's tests
            "release_note": str(data.get("release_note") or "").strip(),
            "map_path": str(data.get("map_path") or "docs/map.yaml")}


def branch(task_id: str) -> str:
    return f"task/{str(task_id)[:8]}"


def worktree_path(cfg: dict[str, Any], task_id: str) -> Path:
    return cfg["workspaces"] / str(task_id)


def ensure_worktree(cfg: dict[str, Any], task_id: str) -> Path:
    """The task's worktree, created from main on first use (repeatable)."""
    path = worktree_path(cfg, task_id)
    if (path / ".git").exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = git("branch", "--list", branch(task_id), cwd=cfg["repo"])
    if existing:
        git("worktree", "add", str(path), branch(task_id), cwd=cfg["repo"])
    else:
        git("worktree", "add", "-b", branch(task_id), str(path), cfg["main"], cwd=cfg["repo"])
    return path


def commit_all(path: Path, message: str) -> str:
    """Commit whatever the PM changed in the worktree; returns HEAD (unchanged when there was nothing to commit)."""
    if git("status", "--porcelain", cwd=path):
        git("add", "-A", cwd=path)
        git(*IDENTITY, "commit", "-q", "-m", message[:2000], cwd=path)
    return git("rev-parse", "HEAD", cwd=path)


def changed_files(cfg: dict[str, Any], sha: str) -> list[str]:
    base = git("merge-base", cfg["main"], sha, cwd=cfg["repo"])
    out = git("diff", "--name-only", base, sha, cwd=cfg["repo"])
    return [line for line in out.splitlines() if line]


def seal(cfg: dict[str, Any], task_id: str, message: str) -> str:
    """The delivery the implementation report is bound to: git:<commit>, or a no-files marker when nothing changed."""
    path = ensure_worktree(cfg, task_id)
    sha = commit_all(path, message)
    return f"{PREFIX}{sha}" if changed_files(cfg, sha) else ""


def validate(cfg: dict[str, Any], task_id: str, sha: str) -> tuple[bool, str]:
    """Run the project's test command on exactly the bound commit."""
    path = worktree_path(cfg, task_id)
    if not (path / ".git").exists():
        return False, "任务工作区不存在"
    head = git("rev-parse", "HEAD", cwd=path)
    if head != sha or git("status", "--porcelain", cwd=path):
        return False, "工作区在提交后又被改动过（或不是被验证的提交），请重新提交实施报告"
    files = changed_files(cfg, sha)
    summary = f"提交 {sha[:12]}，相对 {cfg['main']} 改动 {len(files)} 个文件：" + "、".join(files[:20])
    if not cfg["test_command"]:
        return True, summary + "\n本项目没有配置测试命令（project.yaml 的 validation.command），只确认了提交；Review 时要人工核对测试。"
    env = {key: value for key, value in os.environ.items()
           if key in PASSED_ENV or any(fnmatch.fnmatchcase(key, pattern) for pattern in cfg["pass_env"])}
    try:
        done = subprocess.run(["bash", "-lc", cfg["test_command"]], cwd=str(path), env=env, capture_output=True,
                              text=True, timeout=cfg["test_timeout"])
    except subprocess.TimeoutExpired:
        return False, summary + f"\n测试命令超时（{cfg['test_timeout']} 秒）：{cfg['test_command']}"
    output = (done.stdout + done.stderr).strip()[-3000:]
    verdict = "通过" if done.returncode == 0 else f"失败（退出码 {done.returncode}）"
    return done.returncode == 0, f"{summary}\n测试命令 `{cfg['test_command']}` {verdict}：\n{output}"


def fast_forward(cfg: dict[str, Any], sha: str) -> list[str]:
    """Move main to the approved commit (fast-forward only). A checked-out main is moved only when its working copy
    is clean; anything else is refused so nobody's local work is touched."""
    files = changed_files(cfg, sha)
    repo, main = cfg["repo"], cfg["main"]
    old = git("rev-parse", main, cwd=repo)
    try:
        git("merge-base", "--is-ancestor", old, sha, cwd=repo)
    except GitError as exc:
        raise GitError(f"{main} 在本任务开始后又有了新提交，不能快进；需要先把 {main} 合并进任务分支") from exc
    current = git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo)
    if current != main:
        git("update-ref", f"refs/heads/{main}", sha, old, cwd=repo)   # compare-and-swap: only from the checked value
    elif git("status", "--porcelain", "--untracked-files=no", cwd=repo):
        raise GitError(f"{main} 在主工作区被检出且有未提交的改动，Kernel 不会动它；请先提交或切换分支")
    else:
        git("merge", "--ff-only", "-q", sha, cwd=repo)
    return files


def runtime_home(cfg: dict[str, Any]) -> Path:
    """Runtime executes a clean main working copy; never switch or reset the user's checkout."""
    if git("rev-parse", "HEAD", cwd=cfg["repo"]) != git("rev-parse", cfg["main"], cwd=cfg["repo"]) \
            or git("status", "--porcelain", "--untracked-files=no", cwd=cfg["repo"]):
        raise GitError("运行工作区不对应干净的已发布主分支；请保留改动并核对运行目录，Kernel 不自动切换或重置")
    return cfg["repo"]


def remove_worktree(cfg: dict[str, Any], task_id: str) -> bool:
    """Remove a closed task's worktree; delete its branch only when it is merged into main."""
    path = worktree_path(cfg, task_id)
    if not path.exists():
        return False
    git("worktree", "remove", "--force", str(path), cwd=cfg["repo"])
    merged = git("branch", "--merged", cfg["main"], "--list", branch(task_id), cwd=cfg["repo"])
    if merged:
        git("branch", "-d", branch(task_id), cwd=cfg["repo"])
    return True


def init_repository(path: Path, main: str = "main", readme: str = "") -> bool:
    """A new git-repo project's repository (created at bootstrap when none is given)."""
    if (path / ".git").exists():
        return False
    path.mkdir(parents=True, exist_ok=True)
    git("init", "-q", "-b", main, cwd=path)
    (path / "README.md").write_text(readme or "# 项目代码仓库\n", "utf-8")
    git("add", "-A", cwd=path)
    git(*IDENTITY, "commit", "-q", "-m", "初始化仓库", cwd=path)
    return True
