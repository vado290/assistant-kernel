from __future__ import annotations

import hashlib
import json

from assistant_kernel.config import KernelSettings
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

