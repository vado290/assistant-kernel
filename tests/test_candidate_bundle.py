from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from assistant_kernel.config import KernelSettings
from assistant_kernel.core.executors.delivery import _reseal
from assistant_kernel.core.executors.pm import seal_candidates
from assistant_kernel.services.candidate_bundle import CandidateBundleService
from assistant_kernel.storage import ProjectRuntimeManager


def test_candidate_bundle_seals_hash_verified_files(tmp_path):
    settings = KernelSettings.from_environment().model_copy(update={
        "data_root": tmp_path,
        "provider_secrets_path": tmp_path / "providers.json",
    })
    vault = ProjectRuntimeManager(tmp_path).ensure("demo").vault_root
    candidate = vault / "runs" / "task-1" / "candidate"
    source = candidate / "project" / "module.py"
    source.parent.mkdir(parents=True)
    data = b"VALUE = 1\n"
    source.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    (candidate / "manifest.json").write_text(json.dumps({
        "schema_version": "candidate-manifest.v2",
        "files": [{
            "operation": "create",
            "target_uri": "workspace://project/module.py",
            "candidate_uri": "workspace://candidate/project/module.py",
            "original_sha256": None,
            "candidate_sha256": digest,
            "size": len(data),
        }],
    }), encoding="utf-8")
    first = CandidateBundleService(settings).seal("demo", "task-1")
    second = CandidateBundleService(settings).seal("demo", "task-1")
    assert first["bundle_sha256"] == second["bundle_sha256"]
    assert first["file_artifacts"] == [{
        "uri": (
            f"vault://demo/runtime/candidate-snapshots/"
            f"{first['bundle_sha256']}/files/project/module.py"
        ),
        "sha256": digest,
        "size": len(data),
        "kind": "candidate_file",
        "target_uri": "workspace://project/module.py",
        "operation": "create",
        "required": True,
    }]
    bundle = vault / "runtime" / "candidate-snapshots" / first["bundle_sha256"]
    assert (bundle / "files" / "project" / "module.py").read_bytes() == data


@pytest.fixture
def candidate_case(tmp_path):
    settings = KernelSettings.from_environment().model_copy(update={"data_root": tmp_path})
    vault = ProjectRuntimeManager(tmp_path).ensure("demo").vault_root
    candidate = vault / "runs" / "task-1" / "candidate"
    source = candidate / "project" / "module.py"
    source.parent.mkdir(parents=True)
    data = b"VALUE = 1\n"
    source.write_bytes(data)
    manifest = candidate / "manifest.json"
    manifest.write_text(json.dumps({
        "schema_version": "candidate-manifest.v2",
        "files": [{
            "operation": "create",
            "target_uri": "workspace://project/module.py",
            "candidate_uri": "workspace://candidate/project/module.py",
            "original_sha256": None,
            "candidate_sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
        }],
    }), "utf-8")
    return settings, manifest, source


def test_pm_sealing_rejects_a_missing_registered_file(candidate_case):
    settings, _, source = candidate_case
    source.unlink()
    with pytest.raises(FileNotFoundError):
        seal_candidates(settings, "demo", "task-1", "report")
    bundle, error = _reseal(settings, "demo", "task-1", "expected")
    assert bundle is None and "workspace://candidate/project/module.py" in error


def test_pm_sealing_keeps_document_only_delivery(candidate_case):
    settings, manifest, _ = candidate_case
    manifest.unlink()
    assert seal_candidates(settings, "demo", "task-1", "report") == "no-files:report"
    bundle, error = _reseal(settings, "demo", "task-1", "expected")
    assert bundle is None and "manifest is missing" in error


def test_pm_sealing_rejects_a_manifest_directory(candidate_case):
    settings, manifest, _ = candidate_case
    manifest.unlink()
    manifest.mkdir()
    with pytest.raises((ValueError, OSError)):
        seal_candidates(settings, "demo", "task-1", "report")


@pytest.mark.parametrize("target_name", ["manifest", "source"])
@pytest.mark.parametrize("error_type", [FileNotFoundError, PermissionError])
def test_pm_sealing_propagates_read_errors(candidate_case, monkeypatch, target_name, error_type):
    settings, manifest, source = candidate_case
    target = manifest if target_name == "manifest" else source
    original = Path.read_bytes

    def unavailable(path):
        if path == target:
            raise error_type("unavailable during read")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", unavailable)
    with pytest.raises(error_type, match="unavailable during read"):
        seal_candidates(settings, "demo", "task-1", "report")


def test_pm_sealing_does_not_hide_manifest_stat_permission_error(candidate_case, monkeypatch):
    settings, manifest, _ = candidate_case
    original = Path.stat

    def denied(path, *args, **kwargs):
        if path == manifest:
            raise PermissionError("manifest stat denied")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", denied)
    with pytest.raises(PermissionError, match="manifest stat denied"):
        seal_candidates(settings, "demo", "task-1", "report")


@pytest.mark.parametrize("fault", ["hash", "json"])
def test_pm_sealing_keeps_corruption_failures(candidate_case, fault):
    settings, manifest, source = candidate_case
    if fault == "hash":
        source.write_bytes(b"CHANGED = 2\n")
    else:
        manifest.write_text("{", "utf-8")
    with pytest.raises(ValueError):
        seal_candidates(settings, "demo", "task-1", "report")


def test_pm_sealing_preserves_regular_and_delete_only_bundles(candidate_case):
    settings, manifest, source = candidate_case
    service = CandidateBundleService(settings)
    expected = service.seal("demo", "task-1")["bundle_sha256"]
    assert seal_candidates(settings, "demo", "task-1", "report") == expected
    body = json.loads(manifest.read_text("utf-8"))
    body["files"][0].update(operation="delete", candidate_uri=None,
                            candidate_sha256=None, original_sha256="original", size=0)
    manifest.write_text(json.dumps(body), "utf-8")
    source.unlink()
    deleted = service.seal("demo", "task-1")
    assert deleted["files"][0]["operation"] == "delete" and deleted["file_artifacts"] == []
    assert seal_candidates(settings, "demo", "task-1", "report") == deleted["bundle_sha256"]

