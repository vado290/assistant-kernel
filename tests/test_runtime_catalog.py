from __future__ import annotations

import json
from pathlib import Path

import yaml

from assistant_kernel.config import KernelSettings
from assistant_kernel.services.incidents import IncidentService
from assistant_kernel.services.runtime_catalog import (
    ConversationDigestService, RuntimeCatalogService,
)
from assistant_kernel.services.semantic_map import SemanticMapService
from assistant_kernel.storage import ProjectRuntimeManager


def settings_for(tmp_path: Path) -> KernelSettings:
    return KernelSettings.from_environment().model_copy(update={"data_root": tmp_path})


def test_runtime_catalog_is_immutable_and_has_stable_latest(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    config = tmp_path / "config"
    config.mkdir(parents=True)
    (config / "project-directory.yaml").write_text(yaml.safe_dump({
        "schema_version": "project-directory.v1",
        "projects": [{"project_id": "demo", "purpose": "Demo", "status": "active"}],
    }, sort_keys=False), encoding="utf-8")
    vault = tmp_path / "vaults" / "demo"
    (vault / "knowledge" / "map").mkdir(parents=True)
    (vault / "project.yaml").write_text(yaml.safe_dump({
        "schema_version": "project.v2", "project_id": "demo", "purpose": "Demo",
    }), encoding="utf-8")
    maps = SemanticMapService(settings)
    maps.bootstrap("demo", purpose="Demo", base_pack_version="test")
    project_map = maps.publish_current("demo")
    service = RuntimeCatalogService(settings)
    first = service.refresh()
    second = service.refresh()
    assert first["generation_id"] == second["generation_id"]
    latest = json.loads((ProjectRuntimeManager(tmp_path).ensure("system").vault_root / "knowledge" /
                         "runtime-map" / "latest.json").read_text(encoding="utf-8"))
    assert latest["complete"] is True
    manifest = json.loads(
        (ProjectRuntimeManager(tmp_path).ensure("system").vault_root / "knowledge" /
         "runtime-map" / latest["manifest"]).read_text(encoding="utf-8")
    )
    catalog = json.loads(
        (ProjectRuntimeManager(tmp_path).ensure("system").vault_root / "knowledge" /
         "runtime-map" / manifest["catalog"]).read_text(encoding="utf-8")
    )
    map_ref = catalog["projects"][0]["semantic_map_ref"]
    assert map_ref["generation_id"] == project_map["generation_id"]
    assert map_ref["uri"].endswith(project_map["manifest"])
    assert map_ref["sha256"] == project_map["manifest_sha256"]


def test_conversation_digest_and_incident_are_idempotent(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    digests = ConversationDigestService(settings)
    args = {
        "project_id": "demo", "channel": "discord", "chat_id": "chat",
        "messages": [{"message_id": "m1"}, {"message_id": "m2"}],
        "cumulative_state": {
            "decisions": ["d"], "confirmed_facts": ["f"],
            "constraints": [], "open_questions": [],
        },
        "task_refs": ["t1"], "quality": {"mode": "deterministic"},
    }
    first = digests.materialize(**args)
    second = digests.materialize(**args)
    assert first == second

    incidents = IncidentService(settings)
    one = incidents.record(
        project_id="demo", incident_key="same", severity="high",
        summary="failure", evidence_refs=[], source_run_id="r1",
    )
    two = incidents.record(
        project_id="demo", incident_key="same", severity="high",
        summary="different ignored by stable key", evidence_refs=[], source_run_id="r1",
    )
    assert one == two
