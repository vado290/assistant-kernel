"""The project map (docs/ROUND5_PLAN.md D12): one index per project, `map.yaml`.

Each entry: path (relative to the map's root), purpose (required), name, kind, summary, related. Directory entries
(path ending in "/") give the structural overview. The map holds only the current state; when a file last changed
comes from git, why it changed from the change stream.

Where it lives: `knowledge/map.yaml` in the project folder; a code-delivery project keeps it in its repository
(`project.yaml` map_path, default `docs/map.yaml`), because there the PM only edits the repository.
Every Python file of the deliverable (the project's `product/`, or the repository) must have an entry.
The map indexes the whole project: in a repository's map, `project:<path>` names a file of the project folder
(e.g. `project:knowledge/charter.md`); `grant:<key>/<path>` names a file in a granted external folder.
"""
from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path
from typing import Any, Callable

import yaml

SCHEMA = "project-map.v1"
PROJECT_PREFIX = "project:"
KINDS = ("directory", "entry", "module", "script", "config", "doc", "skill", "agent", "data", "test", "other")
SKIP_DIRS = {"data", "tests", "docs", "__pycache__", ".git", "archive", "runs", "runtime", ".agents",
             "build", "dist", ".venv", "venv", "node_modules"}
RENDER_LIMIT = 8000


def location(settings, project_id: str) -> tuple[Path, Path, str]:
    """(root the paths are relative to, the map file, how to name it in a prompt)."""
    from assistant_kernel.core import gitdelivery
    from assistant_kernel.storage import ProjectRuntimeManager
    code = gitdelivery.config(settings, project_id)
    if code:
        return code["repo"], code["repo"] / code["map_path"], f"代码仓库中的 `{code['map_path']}`"
    vault = ProjectRuntimeManager(settings.data_root).ensure(project_id).vault_root
    return vault, vault / "knowledge" / "map.yaml", "`knowledge/map.yaml`（通过候选文件修改）"


def parse(text: str) -> list[dict[str, Any]]:
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:   # reported as a map problem, never a crash of the validation run
        raise ValueError(f"地图不是合法的 YAML：{str(exc)[:200]}") from exc
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA or not isinstance(data.get("entries"), list):
        raise ValueError(f"地图必须是 schema_version: {SCHEMA} 且带 entries 列表")
    return [dict(entry) for entry in data["entries"] if isinstance(entry, dict)]


def load(path: Path) -> list[dict[str, Any]]:
    return parse(path.read_text("utf-8")) if path.is_file() else []


def covered_files(root: Path, product_root: Path) -> list[str]:
    """The deliverable's Python files that need an entry (relative to `root`)."""
    if not product_root.is_dir():
        return []
    found = []
    for path in sorted(product_root.rglob("*.py")):
        relative = path.relative_to(product_root)
        if path.name == "__init__.py" or any(part in SKIP_DIRS for part in relative.parts[:-1]):
            continue
        found.append(path.relative_to(root).as_posix())
    return found


def problems(entries: list[dict[str, Any]], exists: Callable[[str], bool], required: list[str]) -> list[str]:
    """Format, duplicates, paths that do not exist, and Python files of the deliverable without an entry."""
    found, seen = [], set()
    for number, entry in enumerate(entries, 1):
        path = str(entry.get("path") or "").strip()
        if not path or not str(entry.get("purpose") or "").strip():
            found.append(f"地图第 {number} 项缺少 path 或 purpose")
            continue
        if entry.get("kind") and entry["kind"] not in KINDS:
            found.append(f"地图 {path}：kind 只能是 {', '.join(KINDS)}")
        if path in seen:
            found.append(f"地图 {path} 重复")
        seen.add(path)
        if not path.startswith("grant:") and not exists(path.rstrip("/")):
            found.append(f"地图 {path} 指向的文件不存在")
    found += [f"{path} 没有登记在地图里" for path in required if path not in seen]
    return found


def render(entries: list[dict[str, Any]], limit: int = RENDER_LIMIT) -> str:
    lines = []
    for entry in entries:
        name = f" {entry['name']}" if entry.get("name") else ""
        summary = f"（{entry['summary']}）" if entry.get("summary") else ""
        lines.append(f"- `{entry.get('path')}`{name}：{entry.get('purpose')}{summary}")
    text = "\n".join(lines)
    return text if len(text) <= limit else text[:limit] + "\n…（地图较长，其余条目用 map_lookup 查询）"


def resolve(root: Path, project_root: Path, path: str) -> tuple[Path, str] | None:
    """(the folder a map path is relative to, the path inside it); None for an external `grant:` path."""
    if path.startswith("grant:"):
        return None
    if path.startswith(PROJECT_PREFIX):
        return project_root, path[len(PROJECT_PREFIX):]
    return root, path


def exists_in(root: Path, project_root: Path) -> Callable[[str], bool]:
    def exists(path: str) -> bool:
        place = resolve(root, project_root, path)
        return place is not None and (place[0] / place[1]).exists()
    return exists


def last_changed(root: Path, path: str) -> str:
    """Date of the last commit touching `path` ('' outside a repository)."""
    if not (root / ".git").exists():
        return ""
    done = subprocess.run(["git", "log", "-1", "--format=%cs", "--", path], cwd=str(root), capture_output=True,
                          text=True, timeout=30)
    return done.stdout.strip() if done.returncode == 0 else ""


def lookup(root: Path, entries: list[dict[str, Any]], query: str, limit: int = 20,
           project_root: Path | None = None) -> list[dict[str, Any]]:
    """Entries whose path, name, purpose or summary contain every word of `query`, with their last change date
    (from the repository the file belongs to)."""
    words = [w.lower() for w in re.split(r"\s+", query.strip()) if w]
    matches = [e for e in entries if all(w in " ".join(str(e.get(k) or "") for k in
                                                          ("path", "name", "purpose", "summary")).lower() for w in words)]
    found = []
    for entry in matches[:limit]:
        place = resolve(root, project_root or root, str(entry.get("path") or "").rstrip("/"))
        found.append({**entry, "last_changed": last_changed(*place) if place else ""})
    return found


def skeleton(root: Path, files: list[str]) -> list[dict[str, Any]]:
    """Starting entries for existing Python files: purpose from the module docstring's first line."""
    entries = []
    for path in files:
        try:
            doc = ast.get_docstring(ast.parse((root / path).read_text("utf-8"))) or ""
        except (SyntaxError, UnicodeDecodeError, OSError):
            doc = ""
        purpose = doc.strip().splitlines()[0].strip() if doc.strip() else "（待补充）"
        entries.append({"path": path, "kind": "module", "purpose": purpose[:200]})
    return entries


def dump(entries: list[dict[str, Any]]) -> str:
    return yaml.safe_dump({"schema_version": SCHEMA, "entries": entries}, allow_unicode=True, sort_keys=False)
