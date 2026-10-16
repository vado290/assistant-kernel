"""Skills and shared context (docs/ROUND2_PLAN.md section 4–5).

A skill is ``<dir>/SKILL.md`` with YAML front matter; ``metadata.load`` says when Kernel loads it.
The package ``prompts/skills`` is the template set: Kernel copies it into a project when the project is
created, and from then on only the project's own copies (vault ``.agents/skills``) are loaded.
Shared context lives in the kernel vault (``knowledge/shared/*.md``) and is injected read-only into
every project by the same load rules.
"""
from __future__ import annotations

import hashlib
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

TEMPLATE_SKILLS = Path(__file__).parents[1] / "prompts" / "skills"
SHARED_CONTEXT_HOME = "kernel"        # the project whose vault holds knowledge/shared
SHARED_FILES = ("organization.md", "environment.md", "conventions.md")
PROJECT_DEFAULTS = {"capabilities": [], "delivery": "vault-files", "share_with_kernel": "improvements"}
NL = "\n"
_FRONT = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.S)


@dataclass(frozen=True)
class Skill:
    name: str
    version: str
    description: str
    load: dict[str, Any]
    body: str
    path: Path
    template: dict[str, Any] = field(default_factory=dict)

    @property
    def on_demand(self) -> bool:
        return bool(self.load.get("on_demand"))

    @property
    def label(self) -> str:
        return f"{self.name}@{self.version}"


def parse(path: Path) -> Skill:
    text = path.read_text("utf-8")
    match = _FRONT.match(text)
    front = (yaml.safe_load(match.group(1)) if match else None) or {}
    meta = front.get("metadata") or {}
    return Skill(name=str(front.get("name") or path.parent.name), version=str(meta.get("version") or "0"),
                 description=str(front.get("description") or ""), load=dict(meta.get("load") or {}),
                 body=(match.group(2) if match else text).strip(), path=path, template=dict(meta.get("template") or {}))


def matches(load: dict[str, Any], *, role: str, stage: str | None = None, purpose: str | None = None,
            capabilities: frozenset[str] = frozenset(), task_kind: str | None = None) -> bool:
    """roles, capabilities and task_kinds must hold; stages (PM stage turns) or purposes (other runs) either."""
    if load.get("disabled"):
        return False
    if load.get("roles") and role not in load["roles"]:
        return False
    if not set(load.get("capabilities") or ()) <= capabilities:
        return False
    if load.get("task_kinds") and task_kind not in load["task_kinds"]:
        return False
    stages, purposes = load.get("stages"), load.get("purposes")
    if not stages and not purposes:
        return True
    return bool((stage and stage in (stages or ())) or (purpose and purpose in (purposes or ())))


def _docs(folder: Path, pattern: str) -> list[Skill]:
    return [parse(p) for p in sorted(folder.glob(pattern)) if p.is_file()] if folder.is_dir() else []


def project_skills(vault: Path) -> list[Skill]:
    return _docs(vault / ".agents" / "skills", "*/SKILL.md")


def template_skills() -> list[Skill]:
    return _docs(TEMPLATE_SKILLS, "*/SKILL.md")


def shared_context(data_root: Path) -> list[Skill]:
    folder = data_root / "vaults" / SHARED_CONTEXT_HOME / "knowledge" / "shared"
    return [parse(folder / name) for name in SHARED_FILES if (folder / name).is_file()]


def project_config(vault: Path) -> dict[str, Any]:
    """capabilities / delivery / share_with_kernel from project.yaml, with defaults."""
    path = vault / "project.yaml"
    data = (yaml.safe_load(path.read_text("utf-8")) or {}) if path.is_file() else {}
    config = {key: data.get(key, default) for key, default in PROJECT_DEFAULTS.items()}
    config["capabilities"] = frozenset(str(c) for c in config["capabilities"] or ())
    return config


@dataclass(frozen=True)
class Selection:
    always: tuple[Skill, ...]
    on_demand: tuple[Skill, ...]
    shared: tuple[Skill, ...]

    @property
    def labels(self) -> list[str]:
        return [s.label for s in (*self.always, *self.on_demand, *self.shared)]

    @property
    def files(self) -> list[Path]:
        return [s.path for s in (*self.always, *self.on_demand, *self.shared)]

    def render(self, vault: Path) -> str:
        parts = []
        if self.always:
            parts.append("## 技能\n\n" + "\n\n".join(f"### 技能 {s.name}（{s.version}）\n\n{s.body}" for s in self.always))
        if self.on_demand:
            lines = [f"- {s.name}：{s.description}（全文：`{s.path.relative_to(vault).as_posix()}`）" for s in self.on_demand]
            parts.append("## 按需技能（需要时用 vault_read_text 读取全文）\n\n" + "\n".join(lines))
        if self.shared:
            parts.append("## 公共背景（kernel 维护，只读；与项目章程冲突时以章程为准）\n\n"
                         + "\n\n".join(s.body for s in self.shared))
        return "\n\n".join(parts)


def select(vault: Path, data_root: Path, **context: Any) -> Selection:
    chosen = [s for s in project_skills(vault) if matches(s.load, **context)]
    shared = [s for s in shared_context(data_root) if matches(s.load, **context)]
    return Selection(always=tuple(s for s in chosen if not s.on_demand),
                     on_demand=tuple(s for s in chosen if s.on_demand), shared=tuple(shared))


def digest(paths: list[Path]) -> str:
    h = hashlib.sha256(b"core-skills.v1\0")
    for path in paths:
        h.update(path.as_posix().encode() + b"\0" + path.read_bytes() + b"\0")
    return h.hexdigest()


def body_sha(body: str) -> str:
    """Short fingerprint of a skill body; a copy whose body still has it was never edited by the project."""
    return hashlib.sha256(body.encode()).hexdigest()[:16]


def _install(skill: Skill, target: Path) -> None:
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(skill.path.parent, target)
    text = (target / "SKILL.md").read_text("utf-8")
    stamp = f'  template: {{name: {skill.name}, version: "{skill.version}", sha: "{body_sha(skill.body)}"}}'
    text = re.sub(r"(\nmetadata:\n)", lambda m: m.group(1) + stamp + NL, text, count=1)
    (target / "SKILL.md").write_text(text, "utf-8", newline="\n")


def install_templates(vault: Path, capabilities: frozenset[str]) -> list[str]:
    """Copy the template skills a project can use into its vault, recording their origin. Existing copies stay."""
    installed = []
    for skill in template_skills():
        if not set(skill.load.get("capabilities") or ()) <= capabilities:
            continue
        target = vault / ".agents" / "skills" / skill.name
        if target.exists():
            continue
        _install(skill, target)
        installed.append(skill.name)
    return installed


def template_updates(vault: Path) -> list[dict[str, str]]:
    """Project skill copies whose template has a newer version (copies the project edited are never overwritten;
    the weekly review decides what to adopt)."""
    templates = {s.name: s for s in template_skills()}
    updates = []
    for copy in project_skills(vault):
        template = templates.get(copy.name)
        if template and template.version != copy.version:
            changelog = re.findall(r'^\s*-\s*"(.+?)"\s*$', template.path.read_text("utf-8"), re.M)[:2]
            updates.append({"name": copy.name, "copy": copy.version, "template": template.version,
                            "changes": "；".join(changelog) or "（无变更记录）"})
    return updates


def refresh_unmodified(vault: Path, known: dict[str, str] | None = None) -> tuple[list[str], list[str]]:
    """Bring copies the project never edited up to the current template; returns (refreshed, kept as edited).

    Only for Kernel rollouts: a copy counts as unmodified when its body still matches the fingerprint
    recorded at install (or one in ``known``, for copies installed before fingerprints were recorded).
    """
    templates = {s.name: s for s in template_skills()}
    refreshed, kept = [], []
    for copy in project_skills(vault):
        template = templates.get(copy.name)
        if template is None or copy.version == template.version and copy.load == template.load:
            continue
        unmodified = body_sha(copy.body) in {copy.template.get("sha"), (known or {}).get(copy.name)} - {None}
        if unmodified:
            _install(template, copy.path.parent)
            refreshed.append(copy.name)
        else:
            kept.append(copy.name)
    return refreshed, kept


# ------------------------------------------------------------------ shared-context sections

def _heading(line: str) -> tuple[int, str] | None:
    match = re.match(r"^(#{1,6})\s+(.*?)\s*$", line)
    return (len(match.group(1)), match.group(2)) if match else None


def _title(text: str) -> str:
    """Heading text without leading numbering ("10 ", "3.", "二、") and trailing notes in brackets."""
    text = re.sub(r"^[\d.、一二三四五六七八九十]+\s*", "", text.strip().lstrip("#").strip())
    return re.split(r"[（(]", text, maxsplit=1)[0].strip()


def section_bounds(lines: list[str], title: str) -> tuple[int, int, int] | None:
    """(heading index, body start, end) of the first heading with this title (numbering and notes ignored)."""
    title = _title(title)
    for index, line in enumerate(lines):
        head = _heading(line)
        if head and _title(head[1]) == title:
            level, end = head[0], len(lines)
            for later in range(index + 1, len(lines)):
                other = _heading(lines[later])
                if other and other[0] <= level:
                    end = later
                    break
            return index, index + 1, end
    return None


def section_text(markdown: str, title: str) -> str:
    lines = markdown.splitlines()
    bounds = section_bounds(lines, title)
    return NL.join(lines[bounds[1]:bounds[2]]).strip() if bounds else ""


def replace_section(markdown: str, title: str, content: str) -> str:
    """Replace a section's body (keeping its heading); a missing section is added at the end."""
    lines = markdown.rstrip().splitlines()
    bounds = section_bounds(lines, title)
    body = ["", content.strip(), ""]
    if bounds is None:
        return NL.join(lines + ["", f"## {title.strip()}"] + body).rstrip() + NL
    return NL.join(lines[:bounds[1]] + body + lines[bounds[2]:]).rstrip() + NL


def shared_path(data_root: Path, file: str) -> Path:
    if file not in SHARED_FILES:
        raise ValueError(f"shared context file must be one of {', '.join(SHARED_FILES)}")
    return data_root / "vaults" / SHARED_CONTEXT_HOME / "knowledge" / "shared" / file


def shared_section(data_root: Path, file: str, title: str) -> str:
    try:
        path = shared_path(data_root, file)
    except ValueError:
        return ""
    return section_text(path.read_text("utf-8"), title) if path.is_file() else ""
