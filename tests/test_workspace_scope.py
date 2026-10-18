import hashlib
import json
from pathlib import Path

import pytest
import yaml

from assistant_kernel.config import KernelSettings
from assistant_kernel.services.workspace_scope import AuthorizedRoot, WorkspaceScopeService
from assistant_kernel.storage import ProjectRuntimeManager


def scope_settings(tmp_path: Path) -> KernelSettings:
    base = KernelSettings.from_environment()
    return base.model_copy(update={"workspace_root": tmp_path, "data_root": tmp_path / "data"})


def test_scope_tree_only_contains_configured_roots(tmp_path: Path) -> None:
    settings = scope_settings(tmp_path)
    runtime = ProjectRuntimeManager(settings.data_root)
    project = runtime.ensure("project-a").vault_root
    sibling = runtime.ensure("project-b").vault_root
    (project / "notes.md").write_text("allowed", encoding="utf-8")
    (project / "runtime" / "codex-home" / "auth.json").write_text("secret", encoding="utf-8")
    (sibling / "private.md").write_text("denied", encoding="utf-8")
    config = settings.data_root / "config" / "project-scopes.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(yaml.safe_dump({
        "projects": {"default": {"roots": [{
            "key": "project", "path": "{project_root}", "description": "current",
        }]}}
    }), encoding="utf-8")
    service = WorkspaceScopeService(settings)
    roots = service.resolve("project-a", "run-1", None)
    tree, truncated = service.tree(roots, max_depth=6, limit=100)
    uris = {item["uri"] for item in tree}
    assert truncated is False
    assert "workspace://project/notes.md" in uris
    assert all("auth.json" not in uri for uri in uris)
    assert all("private.md" not in uri for uri in uris)


def test_scope_tree_hides_runtime_traces_and_build_caches_but_keeps_direct_reads(tmp_path: Path) -> None:
    settings = scope_settings(tmp_path)
    runtime = ProjectRuntimeManager(settings.data_root)
    project = runtime.ensure("project-a").vault_root
    visible = project / "notes.md"
    trace = project / "runs" / "agent-traces" / "trace.jsonl"
    cache = project / ".pytest_cache" / "state"
    visible.write_text("visible", encoding="utf-8")
    trace.parent.mkdir(parents=True, exist_ok=True)
    trace.write_text("trace", encoding="utf-8")
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text("cache", encoding="utf-8")
    config = settings.data_root / "config" / "project-scopes.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(yaml.safe_dump({
        "projects": {"default": {"roots": [{
            "key": "project", "path": "{project_root}", "description": "current",
        }]}}
    }), encoding="utf-8")
    service = WorkspaceScopeService(settings)
    roots = service.resolve("project-a", "run-1", None)
    tree, _ = service.tree(roots, max_depth=6, limit=100)
    uris = {item["uri"] for item in tree}
    assert "workspace://project/notes.md" in uris
    assert all("agent-traces" not in uri for uri in uris)
    assert all(".pytest_cache" not in uri for uri in uris)
    _, resolved_trace, _ = service.resolve_uri(
        roots, "workspace://project/runs/agent-traces/trace.jsonl"
    )
    assert resolved_trace.read_text(encoding="utf-8") == "trace"


def test_project_root_include_exclude_rules_limit_tree_and_direct_reads(tmp_path: Path) -> None:
    settings = scope_settings(tmp_path)
    runtime = ProjectRuntimeManager(settings.data_root)
    project = runtime.ensure("project-a").vault_root
    source = project / "source"
    (source / "vnpy").mkdir(parents=True)
    (source / "data").mkdir(parents=True)
    (source / "vnpy" / "gateway.py").write_text("allowed", encoding="utf-8")
    (source / "vnpy" / "notes.txt").write_text("not included", encoding="utf-8")
    (source / "data" / "api_key.json").write_text("secret", encoding="utf-8")
    config = settings.data_root / "config" / "project-scopes.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(yaml.safe_dump({
        "projects": {"project-a": {"roots": [{
            "key": "source",
            "path": str(source),
            "description": "selected source",
            "includes": ["vnpy/**/*.py", "vnpy/*.py"],
            "excludes": ["data", "data/**", "**/api_key.json"],
        }]}}
    }), encoding="utf-8")

    service = WorkspaceScopeService(settings)
    roots = service.resolve("project-a", "run-1", None)
    tree, _ = service.tree(roots, max_depth=6, limit=100)
    uris = {item["uri"] for item in tree}

    assert "workspace://source/vnpy/gateway.py" in uris
    assert "workspace://source/vnpy/notes.txt" not in uris
    assert "workspace://project/source/data/api_key.json" in uris
    with pytest.raises(PermissionError):
        service.resolve_uri(roots, "workspace://source/vnpy/notes.txt")
    with pytest.raises(PermissionError):
        service.resolve_uri(roots, "workspace://source/data/api_key.json")


def test_implementation_writes_candidate_not_formal_target(tmp_path: Path) -> None:
    settings = scope_settings(tmp_path)
    runtime = ProjectRuntimeManager(settings.data_root)
    project = runtime.ensure("project-a").vault_root
    target = project / "skill.md"
    target.write_text("old", encoding="utf-8")
    config = settings.data_root / "config" / "project-scopes.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(yaml.safe_dump({
        "projects": {"default": {"roots": [{
            "key": "project", "path": "{project_root}", "description": "current",
        }]}}
    }), encoding="utf-8")
    service = WorkspaceScopeService(settings)
    roots = service.resolve("project-a", "run-1", "implementation")
    result = service.write_candidate(
        roots, "workspace://project/skill.md", "new",
        expected_sha256="cba06b5736faf67e54b07b561eae94395e774c517a7d910a54369e1263ccfbd4",
    )
    assert target.read_text(encoding="utf-8") == "old"
    assert result["candidate_uri"] == "workspace://candidate/project/skill.md"
    assert (project / "runs" / "run-1" / "candidate" / "project" / "skill.md").read_text(
        encoding="utf-8"
    ) == "new"
    assert (project / "runs" / "run-1" / "candidate" / "manifest.json").is_file()


def test_candidate_create_and_delete_are_explicit_and_hash_guarded(tmp_path: Path) -> None:
    settings = scope_settings(tmp_path)
    runtime = ProjectRuntimeManager(settings.data_root)
    project = runtime.ensure("project-a").vault_root
    config = settings.data_root / "config" / "project-scopes.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(yaml.safe_dump({
        "projects": {"default": {"roots": [{
            "key": "project", "path": "{project_root}", "description": "current",
        }]}}
    }), encoding="utf-8")
    service = WorkspaceScopeService(settings)
    roots = service.resolve("project-a", "run-2", "implementation")
    created = service.write_candidate(
        roots, "workspace://project/new.txt", "new", None, "absent"
    )
    assert created["operation"] == "create"
    target = project / "old.txt"
    target.write_text("old", encoding="utf-8")
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    deleted = service.delete_candidate(
        roots, "workspace://project/old.txt", digest
    )
    assert deleted["operation"] == "delete"
    manifest = json.loads(
        (project / "runs" / "run-2" / "candidate" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["schema_version"] == "candidate-manifest.v2"
    assert {item["operation"] for item in manifest["files"]} == {"create", "delete"}


def test_resource_guide_explains_v2_resource_placement(tmp_path: Path) -> None:
    settings = scope_settings(tmp_path)
    project = ProjectRuntimeManager(settings.data_root).ensure("project-a").vault_root
    (project / "project.yaml").write_text(
        "schema_version: project.v2\nproject_id: project-a\nlayout_version: 2\n",
        encoding="utf-8",
    )

    guide = WorkspaceScopeService(settings).resource_guide("project-a")

    assert guide["layout_version"] == 2
    resources = {item["path"]: item for item in guide["resources"]}
    assert resources["agents/<agent-id>/"]["required"] == [
        "config.yaml", "role.md",
    ]
    assert "map_lookup" in resources["knowledge/map.yaml"]["purpose"]   # ROUND5: one index per project
    assert "knowledge/map/" not in resources
    assert "runs/ and runtime/" in resources
