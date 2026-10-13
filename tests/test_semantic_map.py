from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml

from assistant_kernel.config import KernelSettings
from assistant_kernel.services.semantic_map import SemanticMapService
from assistant_kernel.services.validation_runner import ValidationRunner
from assistant_kernel.storage import ProjectRuntimeManager


def settings_for(tmp_path: Path) -> KernelSettings:
    return KernelSettings.from_environment().model_copy(update={"data_root": tmp_path})


def test_semantic_map_bootstrap_query_and_context_manifest(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    service = SemanticMapService(settings)
    service.bootstrap("demo", purpose="demo project")
    root = ProjectRuntimeManager(settings.data_root).ensure("demo").vault_root
    index = yaml.safe_load((root / "knowledge/map/index.yaml").read_text(encoding="utf-8"))
    index["components"] = [
        {
            "component_id": "demo.entry",
            "type": "python_entrypoint",
            "purpose": "Run the demo service",
            "why": "one stable entrypoint",
            "criticality": 5,
            "change_risk": 4,
            "evidence_quality": 5,
            "source_refs": ["workspace://project/main.py"],
            "relevance": {"build": "required", "incident": "high"},
            "tags": ["service", "entrypoint"],
        },
        {
            "component_id": "demo.archive",
            "type": "history",
            "purpose": "Historical notes",
            "criticality": 1,
            "change_risk": 1,
            "relevance": {"build": "archive"},
        },
    ]
    (root / "knowledge/map/index.yaml").write_text(
        yaml.safe_dump(index, sort_keys=False), encoding="utf-8"
    )
    service.publish_current("demo")
    result = service.query(
        "demo", task_kind="build", query_terms=["service entrypoint"], limit=5,
    )
    assert result["selected"][0]["component_id"] == "demo.entry"
    context = service.materialize_context(
        "demo", "run-1", task_kind="build",
        query_terms=["service"], component_ids=["demo.entry"],
    )
    assert context["schema_version"] == "context-manifest.v3"
    assert context["entries"]
    assert context["selected"][0]["score"] == 100
    assert (root / "runs/run-1/resolved/context-manifest.json").is_file()


def test_critical_component_change_requires_map_impact(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    runtime = ProjectRuntimeManager(settings.data_root).ensure("demo")
    map_root = runtime.vault_root / "knowledge/map"
    map_root.mkdir(parents=True)
    (map_root / "index.yaml").write_text(yaml.safe_dump({
        "schema_version": "semantic-map.v1",
        "project_id": "demo",
        "components": [{
            "component_id": "demo.critical",
            "type": "python_component",
            "purpose": "Critical permission logic",
            "kinds": ["permission"],
            "criticality": 5,
            "source_refs": ["workspace://project/critical.py"],
        }],
    }, sort_keys=False), encoding="utf-8")
    candidate = runtime.vault_root / "runs/run-1/candidate"
    target = candidate / "project/critical.py"
    target.parent.mkdir(parents=True)
    data = b"ALLOWED = False\n"
    target.write_bytes(data)
    (candidate / "manifest.json").write_text(json.dumps({
        "schema_version": "candidate-manifest.v2",
        "files": [{
            "operation": "create",
            "target_uri": "workspace://project/critical.py",
            "candidate_uri": "workspace://candidate/project/critical.py",
            "original_sha256": None,
            "candidate_sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
        }],
    }), encoding="utf-8")
    result = ValidationRunner(settings).validate("demo", "run-1")
    assert result["release_gate"] == "fail"
    assert any(item["code"] == "map_impact_missing" for item in result["issues"])
