"""0.45.11 one-off (analysis 2026-10-22, K1/F11): give unedited skill copies the content fingerprint D24 needs.

`skills.refresh_unmodified` refreshes a copy only when its `template:` stamp carries `content_sha` (body and metadata).
Copies installed before 0.45.8 have none, so the per-version rollout would keep them forever. For each such copy this
script takes the template text of the copy's recorded version from the Kernel repository's git history and compares
body and metadata; identical copies get `content_sha` (and `sha`) added to their stamp, others are listed and left.
Only the changed SKILL.md files are committed in each project.

Run with the prod venv:
  python scripts/r04511_stamp_fingerprints.py [--dry-run]
"""
from __future__ import annotations

import argparse
import itertools
import re
import subprocess
import tempfile
import zipfile
from pathlib import Path

from assistant_kernel.config import KernelSettings
from assistant_kernel.core import projectgit, skills

REPO = Path(__file__).resolve().parents[1]
TEMPLATES = "src/assistant_kernel/prompts/skills"
VERSION = re.compile(r'\n  version: "([^"]+)"')


def git(*args: str) -> str:
    done = subprocess.run(["git", "-c", "safe.directory=*", *args], cwd=REPO, capture_output=True, text=True,
                          encoding="utf-8")
    return done.stdout if done.returncode == 0 else ""


def wheel_texts(name: str, wheels: Path):
    """The template from every archived wheel, newest first: versions deployed but never committed are there."""
    for wheel in sorted(wheels.glob("*/*.whl"), reverse=True):
        try:
            with zipfile.ZipFile(wheel) as archive:
                yield archive.read(f"assistant_kernel/prompts/skills/{name}/SKILL.md").decode("utf-8")
        except (KeyError, OSError, zipfile.BadZipFile):
            continue


def template_text(name: str, version: str, cache: dict, wheels: Path) -> str | None:
    """SKILL.md of template `name` at `version`: the working copy, then git history, then the archived wheels."""
    key = (name, version)
    if key not in cache:
        path = f"{TEMPLATES}/{name}/SKILL.md"
        cache[key] = None
        current = REPO / path
        texts = [current.read_text("utf-8")] if current.is_file() else []
        texts = itertools.chain(texts, (git("show", f"{sha}:{path}")
                                        for sha in git("log", "--format=%H", "--", path).split()),
                                wheel_texts(name, wheels))
        for text in texts:
            found = VERSION.search(text)
            if found and found.group(1) == version:
                cache[key] = text
                break
    return cache[key]


def content_sha(text: str) -> str:
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "SKILL.md"
        path.write_text(text, "utf-8", newline="\n")
        return skills._content_sha(path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    options = parser.parse_args()
    data_root, cache = KernelSettings.from_environment().data_root, {}
    vaults, wheels = data_root / "vaults", data_root / "runtime" / "wheels"
    for vault in sorted(p for p in vaults.iterdir() if (p / "project.yaml").is_file()):
        stamped, left, changed = [], [], []
        for copy in skills.project_skills(vault):
            stamp = copy.template
            if not stamp.get("name") or stamp.get("content_sha"):
                continue   # project's own skill, or already fingerprinted
            label = f"{copy.path.parent.parent.parent.parent.name}/{copy.name} {stamp.get('version')}"
            text = template_text(str(stamp["name"]), str(stamp.get("version")), cache, wheels)
            if text is None:
                left.append(f"{label}：找不到这个模板版本")
                continue
            if content_sha(text) != skills._content_sha(copy.path):
                left.append(f"{label}：与模板不同（项目改过），保留")
                continue
            stamped.append(label)
            if not options.dry_run:
                raw = copy.path.read_text("utf-8")
                line = (f'  template: {{name: {stamp["name"]}, version: "{stamp.get("version")}", '
                        f'sha: "{skills.body_sha(copy.body)}", content_sha: "{skills._content_sha(copy.path)}"}}')
                copy.path.write_text(re.sub(r"\n  template: \{[^\n]*\}", "\n" + line, raw, count=1), "utf-8",
                                     newline="\n")
                changed.append(copy.path.relative_to(vault).as_posix())
        print(f"\n=== {vault.name}：补指纹 {len(stamped)} 份，保留 {len(left)} 份")
        for item in left:
            print(f"  - {item}")
        if changed:
            print(f"  已提交：{projectgit.commit_paths(vault, changed, '补记技能副本的内容指纹，供按版本自动更新（Kernel 0.45.11）')}")
    print("\n（dry-run：没有写入任何文件）" if options.dry_run else "\n完成")


if __name__ == "__main__":
    main()
