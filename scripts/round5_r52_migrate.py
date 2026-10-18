"""ROUND5 R5.2 one-off: give each project folder and each folder a project may write to a repository of its own
(docs/ROUND5_PLAN.md §10.2).

Found on 2026-10-18: the `.git` entries in trading-risk-bot's folder, H:\\AI\\crypto and H:\\AI\\IB are empty folders,
so git fell through to the outer H:\\AI repository; none of them had history of its own.

For each folder: `git init` (branch main); a project folder gets Kernel's .gitignore; an external folder keeps its
own .gitignore files and gets the outer repository's root rules in `.git/info/exclude` (not committed), so it
ignores exactly what it ignored before. Before the first commit the files about to be committed are checked:
anything that looks like a secret or is larger than 50 MB stops the run. Remotes are created on the first push
(dispatcher maintenance) once `gh auth login` has been done.
Also: the kernel project's map moves to docs/map.yaml; the outer repository stops tracking crypto and IB.

Run with the prod venv after deploying 0.45.0:
  python scripts/round5_r52_migrate.py [--dry-run] [--untrack-outer]
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

import yaml

from assistant_kernel.config import KernelSettings
from assistant_kernel.core import projectgit

OUTER = Path("/home/v/AI")
SECRET = re.compile(r"(api_key|apikey|secret|credential|password|token|\.env$|\.pem$|\.key$|id_rsa)", re.I)
HARMLESS = re.compile(r"(example|sample|template)", re.I)
MAX_BYTES = 50 * 1024 * 1024


def to_commit(path: Path) -> list[str]:
    """Files `git add -A` would stage (tracked changes and untracked files not ignored)."""
    out = projectgit.git("status", "--porcelain", "--untracked-files=all", cwd=path)
    return [line[3:].strip('"') for line in out.splitlines() if line]


def check(path: Path, files: list[str]) -> list[str]:
    problems = []
    for name in files:
        if SECRET.search(name) and not HARMLESS.search(name):
            problems.append(f"像是凭据的文件：{name}")
        target = path / name
        if target.is_file() and target.stat().st_size > MAX_BYTES:
            problems.append(f"超过 50 MB：{name}")
    return problems


def initialise(path: Path, label: str, ignore: str | None, exclude: str | None, dry: bool) -> None:
    print(f"\n=== {label}：{path}")
    if projectgit.is_repo(path):
        print("  已经是独立仓库，跳过")
        return
    if dry:
        print("  将 git init 并做第一次提交")
        return
    projectgit.ensure_repo(path, ignore)
    if exclude:
        (path / ".git" / "info").mkdir(parents=True, exist_ok=True)
        (path / ".git" / "info" / "exclude").write_text(exclude, "utf-8")
    files = to_commit(path)
    problems = check(path, files)
    if problems:
        print("  ✗ 停止，未提交：" + "；".join(problems[:20]))
        return
    commit = projectgit.commit_all(path, f"开始独立的版本记录（{label}，ROUND5 R5.2）")
    print(f"  提交 {len(files)} 个文件：{(commit or '无改动')[:12]}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--untrack-outer", action="store_true")
    options = parser.parse_args()
    settings = KernelSettings.from_environment()
    vaults = settings.data_root / "vaults"
    outer_rules = (OUTER / ".gitignore").read_text("utf-8")
    for vault in sorted(p for p in vaults.iterdir() if (p / "project.yaml").is_file()):
        if vault.name == "kernel":
            config = yaml.safe_load((vault / "project.yaml").read_text("utf-8")) or {}
            if config.get("map_path") != "docs/map.yaml" and not options.dry_run:
                config["map_path"] = "docs/map.yaml"
                (vault / "project.yaml").write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), "utf-8")
            print("kernel 项目的地图改为源码仓库的 docs/map.yaml")
        initialise(vault, f"项目 {vault.name}", projectgit.PROJECT_IGNORE, None, options.dry_run)
    for name in ("crypto", "IB"):
        initialise(OUTER / name, f"trading-risk-bot 的外部目录 {name}", None,
                   "# The outer H:\\AI repository's root rules (ROUND5 R5.2)\n" + outer_rules, options.dry_run)
    if options.untrack_outer and not options.dry_run:
        projectgit.git("rm", "-r", "--cached", "-f", "-q", "crypto", "IB", cwd=OUTER)
        rules = (OUTER / ".gitignore").read_text("utf-8")
        if "\ncrypto/\n" not in f"\n{rules}":
            (OUTER / ".gitignore").write_text(rules.rstrip("\n") + "\n# Own repositories (ROUND5 R5.2)\ncrypto/\nIB/\n",
                                              "utf-8")
        projectgit.git("add", ".gitignore", cwd=OUTER)
        print("\n外层仓库不再跟踪 crypto、IB（文件不动，未提交）")


if __name__ == "__main__":
    main()
