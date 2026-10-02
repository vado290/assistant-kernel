from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from assistant_kernel.config import KernelSettings
from assistant_kernel.services.capability_bundle import CapabilityBundleService
from assistant_kernel.services.agent_files import AgentFileLoader
from assistant_kernel.services.stage_handoff import StageHandoffService
from assistant_kernel.services.workspace_scope import WorkspaceScopeService
from assistant_kernel.storage import ProjectRuntimeManager


def settings_for(tmp_path: Path) -> KernelSettings:
    return KernelSettings.from_environment().model_copy(update={
        "data_root": tmp_path,
        "provider_secrets_path": tmp_path / "providers.json",
    })


def write_candidate(
    vault: Path, run_id: str, target_relative: str, content: str,
    original_sha256: str | None,
) -> str:
    root = vault / "runs" / run_id / "candidate"
    candidate = root / "project" / target_relative
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_text(content, encoding="utf-8")
    entry = {
        "operation": "update" if original_sha256 else "create",
        "target_uri": f"workspace://project/{target_relative}",
        "candidate_uri": f"workspace://candidate/project/{target_relative}",
        "original_sha256": original_sha256,
        "candidate_sha256": hashlib.sha256(content.encode()).hexdigest(),
        "size": len(content.encode()),
    }
    (root / "manifest.json").write_text(json.dumps({
        "schema_version": "candidate-manifest.v2", "files": [entry],
    }), encoding="utf-8")
    return f"vault://demo/runs/{run_id}/candidate/manifest.json"


def pin_implementation_scope(settings: KernelSettings, run_id: str) -> None:
    scope = WorkspaceScopeService(settings)
    uri, digest = scope.snapshot("demo", run_id, "implementation")
    vault = ProjectRuntimeManager(settings.data_root).ensure("demo").vault_root
    target = vault / "runs" / run_id / "resolved" / "execution-plan.json"
    target.write_text(json.dumps({
        "bindings": {
            "workspace_scope_snapshot_uri": uri,
            "workspace_scope_snapshot_sha256": digest,
        },
    }), encoding="utf-8")


def test_capability_mode_and_full_bundle_hash(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    service = CapabilityBundleService(settings)
    assert service.inspect("demo", "business-skill")["change_mode"] == "create"
    root = service.active_root("demo", "business-skill")
    root.mkdir(parents=True)
    assert service.inspect("demo", "business-skill")["change_mode"] == "repair"
    (root / "SKILL.md").write_text("instructions", encoding="utf-8")
    script = root / "scripts" / "run.py"
    script.parent.mkdir()
    script.write_text("print('v1')", encoding="utf-8")
    baseline = service.inspect("demo", "business-skill")
    assert baseline["change_mode"] == "iterate"
    assert {item["path"] for item in baseline["files"]} == {"SKILL.md", "scripts/run.py"}
    previous = baseline["bundle_hash"]
    script.write_text("print('v2')", encoding="utf-8")
    assert service.inspect("demo", "business-skill")["bundle_hash"] != previous


def test_create_then_iterate_publication_preserves_revision_and_diff(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    service = CapabilityBundleService(settings)
    vault = ProjectRuntimeManager(tmp_path).ensure("demo").vault_root
    baseline = service.inspect("demo", "new-skill")
    manifest_uri = write_candidate(
        vault, "implementation-1", ".agents/skills/new-skill/SKILL.md", "version one", None
    )
    result, artifacts = service.publish(
        project_id="demo", publish_run_id="publish-1", request={
            "capability_change": {
                "target_skill": "new-skill", "change_mode": "create", "baseline": baseline,
            },
            "candidate_manifest_uri": manifest_uri,
            "implementation_run_id": "implementation-1",
            "review_run_id": "review-1",
            "approval_message_id": "approval-1",
        },
    )
    active = service.active_root("demo", "new-skill")
    assert (active / "SKILL.md").read_text(encoding="utf-8") == "version one"
    assert (active / "skill-manifest.json").is_file()
    assert len(artifacts) == 2
    first_revision = result["published_revision"]

    baseline2 = service.inspect("demo", "new-skill")
    old_hash = hashlib.sha256(b"version one").hexdigest()
    manifest_uri2 = write_candidate(
        vault, "implementation-2", ".agents/skills/new-skill/SKILL.md",
        "version two", old_hash,
    )
    result2, _ = service.publish(
        project_id="demo", publish_run_id="publish-2", request={
            "capability_change": {
                "target_skill": "new-skill", "change_mode": "iterate", "baseline": baseline2,
            },
            "candidate_manifest_uri": manifest_uri2,
            "implementation_run_id": "implementation-2",
        },
    )
    assert result2["parent_revision"] == first_revision
    assert result2["published_revision"] != first_revision
    patch_uri = result2["diff_uri"]
    patch = vault / patch_uri.removeprefix("vault://demo/")
    assert "version one" in patch.read_text(encoding="utf-8")
    assert "version two" in patch.read_text(encoding="utf-8")


def test_stage_handoff_reads_only_bound_hash_verified_artifact(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    vault = ProjectRuntimeManager(tmp_path).ensure("demo").vault_root
    artifact = vault / "artifacts" / "requirements.md"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("approved requirements", encoding="utf-8")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    run = {
        "run_id": "run-2", "project_id": "demo", "stage": "implementation",
        "request_json": {
            "task_root_run_id": "run-1", "stage_index": 1,
            "prior_artifacts": [{
                "uri": "vault://demo/artifacts/requirements.md", "sha256": digest,
                "kind": "approved_requirements",
            }],
        },
    }
    service = StageHandoffService(settings)
    payload, uri, handoff_hash = service.materialize(run)
    bound = payload["required_inputs"][0]
    assert uri == "vault://demo/runs/run-2/input/handoff-attempt-0.json"
    assert len(handoff_hash) == 64
    assert service.read_bound_artifact(run, bound["artifact_id"])["text"] == "approved requirements"


def test_stage_config_adds_protected_task_skill_and_hashes_project_scripts(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    vault = ProjectRuntimeManager(tmp_path).ensure("demo").vault_root
    stage = vault / "stages" / "implementation"
    stage.mkdir(parents=True)
    stage.joinpath("role.md").write_text("implement", encoding="utf-8")
    stage.joinpath("agent-config.yaml").write_text(
        "\n".join([
            "agent_key: project:demo:implementation",
            "skills:",
            "  required:",
            "    - name: business-skill",
            "      source: project",
            "  task_kinds:",
            "    skill_change:",
            "      - name: assistant-capability-maintenance",
            "        source: kernel",
            "  available: []",
            "tools: []",
        ]),
        encoding="utf-8",
    )
    business = vault / ".agents" / "skills" / "business-skill"
    business.mkdir(parents=True)
    business.joinpath("SKILL.md").write_text("business instructions", encoding="utf-8")
    business.joinpath("scripts").mkdir()
    business.joinpath("scripts", "run.py").write_text("print('ok')", encoding="utf-8")
    loaded = AgentFileLoader(settings).load(
        "demo", "project:demo:implementation", task_kind="skill_change"
    )
    assert [item["name"] for item in loaded.skill_bindings] == [
        "assistant-volcengine-models", "business-skill",
        "assistant-capability-maintenance",
    ]
    binding = loaded.skill_bindings[1]
    assert {item["path"] for item in binding["files"]} == {"SKILL.md", "scripts/run.py"}
    assert set((
        "volcengine_model_status", "volcengine_generate", "volcengine_ocr",
    )).issubset(set(loaded.allowed_tools))


def test_generic_candidate_publication_uses_manifest_hash_and_backup(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    vault = ProjectRuntimeManager(tmp_path).ensure("demo").vault_root
    scopes = tmp_path / "config" / "project-scopes.yaml"
    scopes.parent.mkdir(parents=True)
    scopes.write_text(
        "projects:\n  default:\n    roots:\n      - key: project\n        path: \"{project_root}\"\n",
        encoding="utf-8",
    )
    target = vault / "role.md"
    target.write_text("old role", encoding="utf-8")
    old_hash = hashlib.sha256(target.read_bytes()).hexdigest()
    scope = WorkspaceScopeService(settings)
    roots = scope.resolve("demo", "implementation-generic", "implementation")
    scope.write_candidate(
        roots, "workspace://project/role.md", "new role", old_hash, "present"
    )
    pin_implementation_scope(settings, "implementation-generic")
    result, artifacts = CapabilityBundleService(settings).publish(
        project_id="demo", publish_run_id="publish-generic", request={
            "candidate_manifest_uri": (
                "vault://demo/runs/implementation-generic/candidate/manifest.json"
            ),
            "implementation_run_id": "implementation-generic",
            "review_run_id": "review-generic",
            "approval_message_id": "approval-generic",
        },
    )
    assert target.read_text(encoding="utf-8") == "new role"
    assert result["outcome"] == "published"
    assert result["files"][0]["target_uri"] == "workspace://project/role.md"
    assert {item["kind"] for item in artifacts} == {
        "publication_receipt", "publication_diff",
    }
    backup = vault / "runs" / "publish-generic" / "publication" / "backup" / "project" / "role.md"
    assert backup.read_text(encoding="utf-8") == "old role"


def test_file_publisher_removes_created_file_after_later_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = settings_for(tmp_path)
    vault = ProjectRuntimeManager(tmp_path).ensure("demo").vault_root
    run_id = "implementation-rollback"
    manifest_uri = write_candidate(vault, run_id, "first.txt", "first", None)
    candidate_root = vault / "runs" / run_id / "candidate"
    second = candidate_root / "project" / "second.txt"
    second.write_text("second", encoding="utf-8")
    manifest_path = candidate_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"].append({
        "operation": "create", "target_uri": "workspace://project/second.txt",
        "candidate_uri": "workspace://candidate/project/second.txt",
        "original_sha256": None,
        "candidate_sha256": hashlib.sha256(b"second").hexdigest(),
        "size": 6,
    })
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    pin_implementation_scope(settings, run_id)
    original_write = Path.write_bytes

    def fail_second(self: Path, data: bytes) -> int:
        if self.name == "second.txt.publish-rollback.tmp":
            raise OSError("injected second write failure")
        return original_write(self, data)

    monkeypatch.setattr(Path, "write_bytes", fail_second)
    with pytest.raises(OSError, match="injected"):
        CapabilityBundleService(settings).publish(
            project_id="demo", publish_run_id="publish-rollback", request={
                "candidate_manifest_uri": manifest_uri,
                "implementation_run_id": run_id,
                "review_run_id": "review-rollback",
                "approval_message_id": "approval-rollback",
            },
        )
    assert not (vault / "first.txt").exists()
    assert not (vault / "second.txt").exists()


def test_external_read_scope_can_hold_candidate_but_cannot_publish(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    ProjectRuntimeManager(tmp_path).ensure("demo")
    external = tmp_path / "external"
    external.mkdir()
    target = external / "strategy.py"
    target.write_text("OLD = True\n", encoding="utf-8")
    old_hash = hashlib.sha256(target.read_bytes()).hexdigest()
    scopes = tmp_path / "config" / "project-scopes.yaml"
    scopes.parent.mkdir(parents=True, exist_ok=True)
    scopes.write_text(
        "projects:\n  demo:\n    roots:\n"
        "      - key: external-reference\n"
        f"        path: {external.as_posix()}\n"
        "        publish: false\n"
        "        includes: ['*.py']\n",
        encoding="utf-8",
    )
    scope = WorkspaceScopeService(settings)
    roots = scope.resolve("demo", "implementation-external", "implementation")
    scope.write_candidate(
        roots, "workspace://external-reference/strategy.py",
        "OLD = False\n", old_hash, "present",
    )
    pin_implementation_scope(settings, "implementation-external")
    with pytest.raises(PermissionError, match="not approved for publication"):
        CapabilityBundleService(settings).publish(
            project_id="demo", publish_run_id="publish-external", request={
                "candidate_manifest_uri": (
                    "vault://demo/runs/implementation-external/candidate/manifest.json"
                ),
                "implementation_run_id": "implementation-external",
                "review_run_id": "review-external",
                "approval_message_id": "approval-external",
            },
        )
    assert target.read_text(encoding="utf-8") == "OLD = True\n"
