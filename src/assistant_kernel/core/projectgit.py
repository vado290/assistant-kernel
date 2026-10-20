"""Version history of each project (docs/ROUND5_PLAN.md D13, §10.2).

- Every project folder is a git repository; Kernel takes a snapshot before a release (so out-of-band edits are
  kept) and commits after it. External folders the project may write to are repositories of their own and get
  the same two commits.
- Remotes are private GitHub repositories created with `gh` (`<owner>/project-<id>`, `<owner>/<id>-<key>`); an
  existing other `origin` is kept as `legacy-origin`. Pushing happens on the dispatcher's maintenance tick.
- `gh` keeps its login in a mounted folder (GH_CONFIG_DIR), so it survives a container restart; git borrows it
  through gh's credential helper only for the push command itself — nothing is written into repository config.
- Everything here is best effort around a release: a git problem is reported, never blocks the release.
"""
from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)
IDENTITY = ("-c", "user.name=assistant-kernel", "-c", "user.email=assistant-kernel@kernel.local")
PROJECT_IGNORE = """# Kernel internals and runtime data (ROUND5 §3)
runtime/
runs/
product/data/
.codex/
__pycache__/
*.pyc
"""


class GitError(RuntimeError):
    pass


def gh_config_dir() -> str:
    return os.environ.get("CORE_GH_CONFIG_DIR") or "/home/v/AI/.container-home/gh"


def owner() -> str:
    """The GitHub account the repositories belong to: CORE_GITHUB_OWNER, else the account `gh` is logged in as
    (anyone may install the public kernel project; its repositories go to their own account, never a fixed one)."""
    configured = os.environ.get("CORE_GITHUB_OWNER")
    if configured:
        return configured
    try:
        login = run("gh", "api", "user", "--jq", ".login", cwd=Path.cwd(), timeout=60)
    except (GitError, OSError, subprocess.TimeoutExpired) as exc:
        raise GitError(f"不知道推送到哪个 GitHub 账号（gh 未登录？）：{exc}") from exc
    if not login:
        raise GitError("gh 没有返回登录的 GitHub 账号")
    return login


def _env() -> dict[str, str]:
    return {**os.environ, "GH_CONFIG_DIR": gh_config_dir(), "GIT_TERMINAL_PROMPT": "0"}


def run(*args: str, cwd: Path, timeout: int = 120) -> str:
    done = subprocess.run(list(args), cwd=str(cwd), capture_output=True, text=True, timeout=timeout, env=_env())
    if done.returncode != 0:
        raise GitError(f"{' '.join(args[:3])} 失败：{(done.stderr or done.stdout).strip()[-500:]}")
    return done.stdout.strip()


def git(*args: str, cwd: Path, timeout: int = 120) -> str:
    return run("git", *args, cwd=cwd, timeout=timeout)


def is_repo(path: Path) -> bool:
    """A repository of its own (an empty `.git` folder is not: git would fall through to an enclosing repository)."""
    return (path / ".git" / "HEAD").is_file() or (path / ".git").is_file()


def ensure_repo(path: Path, ignore: str | None = PROJECT_IGNORE) -> bool:
    """Make `path` a repository on branch main (with the ignore rules when it has none); True when created."""
    if is_repo(path):
        return False
    path.mkdir(parents=True, exist_ok=True)
    git("init", "-q", "-b", "main", cwd=path)
    # The folders live on the Windows drive H: mounted into the container: the container sees every file as
    # executable and Windows git converts line endings; neither is a real change.
    git("config", "core.filemode", "false", cwd=path)
    git("config", "core.autocrlf", "false", cwd=path)
    if ignore and not (path / ".gitignore").exists():
        (path / ".gitignore").write_text(ignore, "utf-8", newline="\n")
    return True


def commit_all(path: Path, message: str) -> str | None:
    """Commit everything that changed; the new commit, or None when there was nothing to commit."""
    if not is_repo(path) or not git("status", "--porcelain", cwd=path):
        return None
    git("add", "-A", cwd=path)
    git(*IDENTITY, "commit", "-q", "-m", message[:4000], cwd=path)
    return git("rev-parse", "HEAD", cwd=path)


def ignored(path: Path, paths: list[str]) -> set[str]:
    """Which of `paths` the repository ignores (e.g. the personal files of the public kernel project)."""
    if not paths:
        return set()
    done = subprocess.run(["git", "check-ignore", "--", *paths], cwd=str(path), capture_output=True, text=True,
                          timeout=60, env=_env())
    if done.returncode not in (0, 1):   # 1: none of them is ignored
        raise GitError(f"git check-ignore 失败：{(done.stderr or done.stdout).strip()[-500:]}")
    return {line.strip() for line in done.stdout.splitlines() if line.strip()}


def commit_paths(path: Path, paths: list[str], message: str) -> str | None:
    """Commit only what changed under `paths` (other pending edits stay uncommitted); None when nothing did.
    Ignored paths are left out: naming one to `git add` would fail the whole commit."""
    if not is_repo(path):
        return None
    skip = ignored(path, paths)
    paths = [p for p in paths if p not in skip]
    if not paths:
        return None
    git("add", "-A", "--", *paths, cwd=path)
    if not git("diff", "--cached", "--name-only", "--", *paths, cwd=path):
        return None
    git(*IDENTITY, "commit", "-q", "-m", message[:4000], "--", *paths, cwd=path)
    return git("rev-parse", "HEAD", cwd=path)


def head(path: Path) -> str | None:
    try:
        return git("rev-parse", "HEAD", cwd=path) if is_repo(path) else None
    except GitError:   # no commit yet
        return None


def changed_files(path: Path, before: str | None, after: str) -> list[str]:
    """Files changed between two commits (all files of `after` when there is no `before`)."""
    if before:
        return [f for f in git("diff", "--name-only", before, after, cwd=path).splitlines() if f]
    return [f for f in git("show", "--name-only", "--format=", after, cwd=path).splitlines() if f]


def remote_name(project_id: str, key: str | None = None) -> str:
    return f"{project_id}-{key}" if key else f"project-{project_id}"


def ensure_remote(path: Path, name: str) -> str:
    """`origin` → https://github.com/<owner>/<name>.git, creating the private repository when it does not exist.
    A different existing origin is kept as `legacy-origin`. Needs a `gh auth login` in GH_CONFIG_DIR."""
    account = owner()
    url = f"https://github.com/{account}/{name}.git"
    remotes = git("remote", cwd=path).split()
    if "origin" in remotes:
        if git("remote", "get-url", "origin", cwd=path) == url:
            return url
        if "legacy-origin" not in remotes:
            git("remote", "rename", "origin", "legacy-origin", cwd=path)
        else:
            git("remote", "remove", "origin", cwd=path)
    try:
        run("gh", "repo", "view", f"{account}/{name}", "--json", "name", cwd=path, timeout=60)
    except GitError:
        run("gh", "repo", "create", f"{account}/{name}", "--private", cwd=path, timeout=60)
    git("remote", "add", "origin", url, cwd=path)
    return url


def pending_push(path: Path) -> bool:
    """Commits that the remote does not have yet (or no upstream at all)."""
    if not is_repo(path):
        return False
    try:
        git("rev-parse", "--verify", "-q", "HEAD", cwd=path)
    except GitError:   # no commit yet
        return False
    try:
        return int(git("rev-list", "--count", "@{u}..HEAD", cwd=path)) > 0
    except GitError:
        return True


def repositories(vault: Path, project_id: str, grants: list[dict]) -> list[tuple[Path, str]]:
    """(folder, remote name) of the project folder and of every external folder the project may write to."""
    found = [(vault, remote_name(project_id))]
    for grant in grants:
        policy = grant.get("policy_json") or {}
        if policy.get("access") == "write" and policy.get("path"):
            found.append((Path(policy["path"]), remote_name(project_id, grant["resource_key"])))
    return found


def commit_release(vault: Path, project_id: str, grants: list[dict], message: str, *,
                   external: bool = True) -> tuple[list[str], list[dict]]:
    """Commit the project folder (and, with `external`, its writable external folders).
    Returns (problems, commits): each commit as {repo: "project" or the folder key, commit, files changed}."""
    problems, commits = [], []
    for path, name in repositories(vault, project_id, grants):
        if path != vault and not external:
            continue
        try:
            if path == vault:
                ensure_repo(path)
            if not is_repo(path):
                continue
            before = head(path)
            commit = commit_all(path, message)
            if commit:
                key = "project" if path == vault else name[len(project_id) + 1:]
                commits.append({"repo": key, "commit": commit, "files": changed_files(path, before, commit)})
        except (GitError, OSError, subprocess.TimeoutExpired) as exc:
            log.warning("git commit in %s failed: %s", path, exc)
            problems.append(f"{path}：{exc}")
    return problems, commits


def snapshot_external(vault: Path, project_id: str, grants: list[dict], message: str) -> list[str]:
    """Commit the writable external folders as they are before a task starts changing them (its build stage), so
    the release commit later holds exactly the task's changes there. Returns problems."""
    problems = []
    for path, _ in repositories(vault, project_id, grants)[1:]:
        try:
            if is_repo(path):
                commit_all(path, message)
        except (GitError, OSError, subprocess.TimeoutExpired) as exc:
            log.warning("snapshot of %s failed: %s", path, exc)
            problems.append(f"{path}：{exc}")
    return problems


def push(path: Path, name: str) -> None:
    ensure_remote(path, name)
    git("-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential",
        "push", "-q", "-u", "origin", "HEAD", cwd=path, timeout=300)
