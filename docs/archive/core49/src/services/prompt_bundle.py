"""Project-local stage roles and skills, with immutable per-node binding snapshots."""
from __future__ import annotations

import hashlib
from pathlib import Path

from assistant_kernel.storage import ProjectRuntimeManager

PROMPTS = Path(__file__).parents[1] / "prompts"
STAGE_SKILLS = {
    "requirements": "assistant-requirements",
    "implementation": "assistant-implementation",
    "review": "assistant-review",
}


def role_relative(stage):
    if stage not in STAGE_SKILLS:
        raise ValueError("unknown stage role")
    return f"stages/{stage}/role.md"


def default_role(stage):
    if stage not in STAGE_SKILLS:
        raise ValueError("unknown stage role")
    return (PROMPTS / "roles" / "stage-worker.md").read_text(encoding="utf-8")


class PromptBundle:
    def __init__(self, settings):
        self.runtime = ProjectRuntimeManager(settings.data_root)

    def bind(self, project_id, stage, role):
        root = self.runtime.ensure(project_id).vault_root
        role_file = self.runtime.resolve_below(root, role_relative(stage))
        role_file.parent.mkdir(parents=True, exist_ok=True)
        if not role_file.exists():
            role_file.write_text(role["content"], encoding="utf-8")
        name = STAGE_SKILLS[stage]
        skill_file = self.runtime.resolve_below(root, f".agents/skills/{name}/SKILL.md")
        skill_file.parent.mkdir(parents=True, exist_ok=True)
        if not skill_file.exists():
            skill_file.write_text((PROMPTS / "skills" / name / "SKILL.md").read_text(encoding="utf-8"), encoding="utf-8")
        binding = {"stage": stage, "role_version_id": str(role["role_version_id"]), "skill_name": name}
        for kind, content in (("role", role["content"]), ("skill", skill_file.read_text(encoding="utf-8"))):
            if len(content.encode("utf-8")) > 100000:
                raise ValueError("role/skill exceeds stage prompt budget")
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            rel = f"runtime/prompt-snapshots/{digest}.md"
            target = self.runtime.resolve_below(root, rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                target.write_text(content, encoding="utf-8")
            if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                raise ValueError("prompt snapshot hash mismatch")
            binding[kind] = {"path": rel, "sha256": digest}
        return binding

    def load(self, project_id, binding):
        root = self.runtime.describe(project_id).vault_root
        result = {}
        for kind in ("role", "skill"):
            path = self.runtime.resolve_below(root, binding[kind]["path"])
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != binding[kind]["sha256"]:
                raise ValueError("bound prompt was modified")
            result[kind] = data.decode("utf-8")
        return result
