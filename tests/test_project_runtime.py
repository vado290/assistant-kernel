from pathlib import Path

import pytest

from assistant_kernel.storage import ProjectRuntimeManager


def test_project_codex_home_is_isolated(tmp_path: Path) -> None:
    manager = ProjectRuntimeManager(tmp_path)
    first = manager.ensure("project-a")
    second = manager.ensure("project-b")
    assert first.codex_home != second.codex_home
    assert first.codex_home.is_dir()
    assert second.codex_home.is_dir()


def test_global_butler_home_is_not_a_project_vault(tmp_path: Path) -> None:
    runtime = ProjectRuntimeManager(tmp_path).ensure("system")
    assert runtime.vault_root == tmp_path / "butler"
    assert not (tmp_path / "vaults" / "system").exists()


def test_vault_path_cannot_escape(tmp_path: Path) -> None:
    manager = ProjectRuntimeManager(tmp_path)
    with pytest.raises(ValueError, match="stay inside"):
        manager.resolve_vault_file("project-a", "../outside.txt")


def test_project_id_rejects_traversal(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        ProjectRuntimeManager(tmp_path).ensure("../outside")
