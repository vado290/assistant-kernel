from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path


PROJECT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


@dataclass(frozen=True)
class ProjectRuntime:
    project_id: str
    vault_root: Path
    runtime_root: Path
    codex_home: Path

# Historical Butler projects are never executable again.
RETIRED_PROJECTS = frozenset({"system", "butler-maintenance"})


class ProjectRuntimeManager:
    """Creates one durable, isolated Codex state directory per project."""

    def __init__(self, data_root: Path):
        self.data_root = data_root
        self.template_home = data_root / "system/codex-home-template"

    def ensure(self, project_id: str) -> ProjectRuntime:
        if project_id in RETIRED_PROJECTS:
            raise PermissionError("Butler is retired; select an existing Project PM")
        runtime = self.describe(project_id)
        vault_root, codex_home = runtime.vault_root, runtime.codex_home
        codex_home.mkdir(parents=True, exist_ok=True)
        for relative in ("sources", "reports", "runs", "knowledge", "capabilities", "artifacts", ".agents/skills"):
            self.resolve_below(vault_root, relative).mkdir(parents=True, exist_ok=True)
        self._seed_codex_home(codex_home)
        return runtime

    def describe(self, project_id: str) -> ProjectRuntime:
        """Derive the canonical locations without creating files on a read request."""
        if not PROJECT_ID_PATTERN.fullmatch(project_id):
            raise ValueError("project_id must be a safe slug containing letters, digits, dot, dash or underscore")
        # Retired runtime keys retain their historical read-only locations.
        # ``system`` runtime key and vault://system URIs keep their original
        # directory so existing jobs, questions, Sessions and references survive.
        vault_root = (
            self.data_root / "butler"
            if project_id == "system"
            else self.data_root / "vaults" / project_id
        )
        runtime_root = vault_root / "runtime"
        codex_home = runtime_root / "codex-home"
        expected_root = (
            (self.data_root / "butler").resolve()
            if project_id == "system"
            else (self.data_root / "vaults").resolve() / project_id
        )
        if vault_root.resolve() != expected_root:
            raise ValueError("project root cannot redirect to another Vault")
        if project_id != "system":
            self.resolve_below(self.data_root / "vaults", project_id)
        self.resolve_below(vault_root, "runtime/codex-home")
        return ProjectRuntime(project_id, vault_root, runtime_root, codex_home)

    @staticmethod
    def resolve_below(root: Path, relative_path: str) -> Path:
        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("relative path must stay inside the project Vault")
        target = (root / relative).resolve()
        resolved_root = root.resolve()
        if target != resolved_root and resolved_root not in target.parents:
            raise ValueError("resolved path escaped the project Vault")
        return target

    def _seed_codex_home(self, codex_home: Path) -> None:
        for filename in ("auth.json", "config.toml"):
            source = self.template_home / filename
            target = codex_home / filename
            if source.is_file() and not target.exists():
                shutil.copy2(source, target)
                if filename == "auth.json":
                    os.chmod(target, 0o600)

    def resolve_vault_file(self, project_id: str, relative_path: str) -> Path:
        runtime = self.describe(project_id)
        return self.resolve_below(runtime.vault_root, relative_path)
