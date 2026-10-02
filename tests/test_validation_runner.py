from __future__ import annotations

import hashlib
import json
import socket
import sys
from pathlib import Path

import pytest

from assistant_kernel.config import KernelSettings
from assistant_kernel.services.candidate_bundle import CandidateBundleService
from assistant_kernel.services.validation_runner import ValidationRunner
from assistant_kernel.storage import ProjectRuntimeManager


def settings_for(tmp_path: Path) -> KernelSettings:
    return KernelSettings.from_environment().model_copy(update={
        "data_root": tmp_path,
        "provider_secrets_path": tmp_path / "providers.json",
    })


def write_manifest(candidate_root: Path, files: dict[str, str]) -> None:
    entries = []
    for relative, content in files.items():
        target = candidate_root / "project" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        payload = target.read_bytes()
        entries.append({
            "operation": "create",
            "target_uri": f"workspace://project/{relative}",
            "candidate_uri": f"workspace://candidate/project/{relative}",
            "original_sha256": None,
            "candidate_sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
        })
    (candidate_root / "manifest.json").write_text(
        json.dumps({
            "schema_version": "candidate-manifest.v2",
            "files": entries,
        }),
        encoding="utf-8",
    )


def report_from_artifact(settings: KernelSettings, artifact: dict) -> dict:
    prefix = "vault://demo/"
    path = ProjectRuntimeManager(settings.data_root).resolve_vault_file(
        "demo", artifact["uri"][len(prefix):]
    )
    return json.loads(path.read_text(encoding="utf-8"))


def test_validation_runner_accepts_skill_and_python_candidate(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    vault = ProjectRuntimeManager(settings.data_root).ensure("demo").vault_root
    candidate_root = vault / "runs" / "implementation-1" / "candidate"
    skill = """---
name: demo-skill
description: Complete one demo task.
---

# Demo

## 任务目标

Produce a deterministic result.
"""
    write_manifest(candidate_root, {
        ".agents/skills/demo-skill/SKILL.md": skill,
        ".agents/skills/demo-skill/scripts/run.py": "print('ok')\n",
    })

    artifact = ValidationRunner(settings).validate("demo", "implementation-1")
    report = report_from_artifact(settings, artifact)

    assert artifact["kind"] == "validation_report"
    assert artifact["status"] == "passed"
    assert report["status"] == "passed"
    assert report["schema_version"] == "validation-report.v2"
    assert report["release_gate"] == "pass"
    assert len(report["checked_files"]) == 2
    assert report["issues"] == []


def test_validation_runner_reports_invalid_skill_python_and_hash(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    vault = ProjectRuntimeManager(settings.data_root).ensure("demo").vault_root
    candidate_root = vault / "runs" / "implementation-2" / "candidate"
    write_manifest(candidate_root, {
        ".agents/skills/broken/SKILL.md": "---\nname: broken\n---\n\n# Broken\n",
        ".agents/skills/broken/scripts/run.py": "if True print('broken')\n",
    })
    manifest_path = candidate_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][0]["candidate_sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    artifact = ValidationRunner(settings).validate("demo", "implementation-2")
    report = report_from_artifact(settings, artifact)
    codes = {item["code"] for item in report["issues"]}

    assert artifact["status"] == "failed"
    assert {"candidate_hash", "skill_description", "skill_task_goal", "python_syntax"} <= codes


def test_validation_runner_fails_when_no_candidate_exists(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    artifact = ValidationRunner(settings).validate("demo", "implementation-3")
    report = report_from_artifact(settings, artifact)

    assert artifact["status"] == "failed"
    assert report["status"] == "failed"
    assert report["issues"][0]["code"] == "candidate_manifest_missing"


def test_validation_runner_allows_explicit_nonblocking_deferred_e2e(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    vault = ProjectRuntimeManager(settings.data_root).ensure("demo").vault_root
    candidate_root = vault / "runs" / "implementation-4" / "candidate"
    write_manifest(candidate_root, {"scripts/job.py": "print('ok')\n"})
    manifest_path = candidate_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["validation_plan"] = [{
        "target_id": "workflow:three-day-run",
        "scope": "workflow_e2e",
        "required": False,
        "deferred": True,
        "reason": "the complete workflow requires 72 hours",
        "acceptance_run_ref": "post-release:workflow:three-day-run",
    }]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    artifact = ValidationRunner(settings).validate("demo", "implementation-4")
    report = report_from_artifact(settings, artifact)

    assert report["release_gate"] == "pass"
    assert report["deferred_targets"][0]["target_id"] == "workflow:three-day-run"
    assert report["residual_risks"]


def test_validation_runner_marks_required_unexecuted_scope_incomplete(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    vault = ProjectRuntimeManager(settings.data_root).ensure("demo").vault_root
    candidate_root = vault / "runs" / "implementation-5" / "candidate"
    write_manifest(candidate_root, {"scripts/job.py": "print('ok')\n"})
    manifest_path = candidate_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["validation_plan"] = [{
        "target_id": "mcp:live-start",
        "scope": "component",
        "required": True,
    }]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    artifact = ValidationRunner(settings).validate("demo", "implementation-5")
    report = report_from_artifact(settings, artifact)

    assert artifact["status"] == "failed"
    assert report["release_gate"] == "incomplete"
    assert report["unresolved_required_targets"] == ["mcp:live-start"]


def test_kernel_validation_policy_cannot_be_omitted_by_candidate(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    vault = ProjectRuntimeManager(settings.data_root).ensure("demo").vault_root
    candidate_root = vault / "runs" / "implementation-6" / "candidate"
    write_manifest(candidate_root, {"notes.md": "documented change\n"})

    artifact = ValidationRunner(settings).validate(
        "demo", "implementation-6",
        validation_policy={"required_scopes": ["structure", "contract"]},
    )
    report = report_from_artifact(settings, artifact)

    assert report["release_gate"] == "incomplete"
    assert "kernel-policy:contract" in report["unresolved_required_targets"]


def test_candidate_cannot_override_kernel_validation_target(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    vault = ProjectRuntimeManager(settings.data_root).ensure("demo").vault_root
    candidate_root = vault / "runs" / "implementation-7" / "candidate"
    write_manifest(candidate_root, {"scripts/job.py": "print('ok')\n"})
    manifest_path = candidate_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["validation_plan"] = [{
        "target_id": "kernel:python-syntax",
        "scope": "component",
        "required": False,
        "deferred": True,
        "reason": "candidate attempted to weaken the gate",
        "acceptance_run_ref": "later",
    }]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    artifact = ValidationRunner(settings).validate("demo", "implementation-7")
    report = report_from_artifact(settings, artifact)

    assert report["release_gate"] == "fail"
    assert any(
        issue["code"] == "validation_target_duplicate" for issue in report["issues"]
    )


def test_mcp_manifest_requires_tool_effect(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    vault = ProjectRuntimeManager(settings.data_root).ensure("demo").vault_root
    candidate_root = vault / "runs" / "implementation-8" / "candidate"
    write_manifest(candidate_root, {
        "mcp/demo/mcp-manifest.yaml": (
            "server_id: demo\ntransport: stdio\nentrypoint: server.py\n"
            "tools:\n  - name: demo_read\n"
        ),
    })

    artifact = ValidationRunner(settings).validate("demo", "implementation-8")
    report = report_from_artifact(settings, artifact)

    assert report["release_gate"] == "fail"
    assert any(issue["code"] == "mcp_tool_effect" for issue in report["issues"])


def test_python_unittest_profile_runs_candidate_overlay_with_network_enabled(
    tmp_path: Path,
) -> None:
    settings = settings_for(tmp_path)
    vault = ProjectRuntimeManager(settings.data_root).ensure("demo").vault_root
    candidate_root = vault / "runs" / "implementation-9" / "candidate"
    write_manifest(candidate_root, {
        "test_sample.py": (
            "import socket\nimport unittest\n\n"
            "class CandidateTests(unittest.TestCase):\n"
            "    def test_value(self): self.assertEqual(1 + 1, 2)\n"
            "    def test_network_is_enabled(self):\n"
            "        import os\n"
            "        self.assertEqual(os.environ['TASK_VALIDATION_NETWORK'], 'enabled')\n"
        ),
    })
    artifact = ValidationRunner(settings).validate(
        "demo", "implementation-9",
        validation_policy={
            "required_scopes": ["structure", "component", "workflow_e2e"],
            "python_unittest": [{
                "target_id": "candidate-tests", "root_key": "project",
                "module": "test_sample", "min_tests": 2,
                "timeout_seconds": 30, "required": True,
            }],
        },
    )
    report = report_from_artifact(settings, artifact)
    assert report["release_gate"] == "pass"
    target = next(item for item in report["targets"] if item["target_id"] == "candidate-tests")
    assert target["status"] == "passed"
    assert target["scope"] == "workflow_e2e"


@pytest.mark.skipif(sys.platform != "linux", reason="requires Linux network namespaces")
def test_validation_overlay_excludes_deleted_formal_file(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    vault = ProjectRuntimeManager(settings.data_root).ensure("demo").vault_root
    (vault / "pkg").mkdir()
    (vault / "pkg" / "removed.py").write_text("OLD = True\n", encoding="utf-8")
    candidate_root = vault / "runs" / "delete-check" / "candidate"
    write_manifest(candidate_root, {
        "test_removed.py": (
            "import importlib.util\nimport unittest\n\n"
            "class DeletedTests(unittest.TestCase):\n"
            "    def test_absent(self):\n"
            "        self.assertIsNone(importlib.util.find_spec('pkg.removed'))\n"
        ),
    })
    manifest_path = candidate_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"].append({
        "operation": "delete", "target_uri": "workspace://project/pkg/removed.py",
        "candidate_uri": None,
        "original_sha256": hashlib.sha256(b"OLD = True\n").hexdigest(),
        "candidate_sha256": None, "size": 0,
    })
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    artifact = ValidationRunner(settings).validate(
        "demo", "delete-check", validation_policy={
            "required_scopes": ["structure", "component", "workflow_e2e"],
            "python_unittest": [{
                "target_id": "delete-overlay", "root_key": "project",
                "module": "test_removed", "min_tests": 1,
            }],
        },
    )
    report = report_from_artifact(settings, artifact)
    assert report["release_gate"] == "pass"
    assert not (
        vault / "runs" / "delete-check" / "validation" / "overlay"
        / "project" / "pkg" / "removed.py"
    ).exists()


@pytest.mark.skipif(sys.platform != "linux", reason="requires Linux network namespaces")
def test_validation_allows_python_child_network_access(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    vault = ProjectRuntimeManager(settings.data_root).ensure("demo").vault_root
    candidate_root = vault / "runs" / "network-check" / "candidate"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        write_manifest(candidate_root, {
            "test_network.py": (
                "import subprocess\nimport sys\nimport unittest\n\n"
                "class NetworkTests(unittest.TestCase):\n"
                "    def test_child_can_connect(self):\n"
                "        code = 'import socket;socket.create_connection((\"127.0.0.1\","
                f"{port})).close()'\n"
                "        result = subprocess.run([sys.executable, '-S', '-c', code],"
                " capture_output=True)\n"
                "        self.assertEqual(result.returncode, 0, result.stderr.decode())\n"
            ),
        })
        artifact = ValidationRunner(settings).validate(
            "demo", "network-check", validation_policy={
                "required_scopes": ["structure", "component", "workflow_e2e"],
                "python_unittest": [{
                    "target_id": "network-namespace", "root_key": "project",
                    "module": "test_network", "min_tests": 1,
                }],
            },
        )
    report = report_from_artifact(settings, artifact)
    assert report["release_gate"] == "pass"
    command = json.loads((
        vault / "runs" / "network-check" / "validation" / "commands"
        / "network-namespace.json"
    ).read_text(encoding="utf-8"))
    assert command["network_guard"] == "network-enabled"


def test_validation_uses_sealed_candidate_bytes_and_rejects_manifest_drift(
    tmp_path: Path,
) -> None:
    settings = settings_for(tmp_path)
    vault = ProjectRuntimeManager(settings.data_root).ensure("demo").vault_root
    candidate_root = vault / "runs" / "sealed-check" / "candidate"
    write_manifest(candidate_root, {"safe.py": "SAFE = True\n"})
    bundle = CandidateBundleService(settings).seal("demo", "sealed-check")
    (candidate_root / "project" / "safe.py").write_text(
        "not valid python !\n", encoding="utf-8"
    )
    validated = ValidationRunner(settings).validate(
        "demo", "sealed-check", sealed_bundle=bundle,
        validation_policy={"required_scopes": ["structure", "component"]},
    )
    assert report_from_artifact(settings, validated)["release_gate"] == "pass"
    manifest_path = candidate_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["validation_plan"] = []
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    rejected = ValidationRunner(settings).validate(
        "demo", "sealed-check", sealed_bundle=bundle,
    )
    report = report_from_artifact(settings, rejected)
    assert report["release_gate"] == "fail"
    assert any(item["code"] == "sealed_candidate_mismatch" for item in report["issues"])
