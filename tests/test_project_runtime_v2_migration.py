from __future__ import annotations

from pathlib import Path

import yaml

from scripts.migrate_project_runtime_v2 import migrate_project


def test_migration_creates_explicit_v2_layout_without_deleting_legacy(tmp_path: Path) -> None:
    root = tmp_path / "vaults" / "demo"
    root.mkdir(parents=True)
    (root / "agent-config.yaml").write_text(yaml.safe_dump({
        "schema_version": "agent-config.v1",
        "agent_key": "project:demo:manager",
        "skills": ["assistant-project-manager"],
        "tools": ["get_authorized_workspace"],
    }), encoding="utf-8")
    (root / "role.md").write_text("# PM\n", encoding="utf-8")
    for stage in ("requirements", "implementation", "review"):
        stage_root = root / "stages" / stage
        stage_root.mkdir(parents=True)
        (stage_root / "agent-config.yaml").write_text(yaml.safe_dump({
            "schema_version": "agent-config.v1",
            "agent_key": f"project:demo:{stage}",
            "skills": [],
            "tools": ["get_authorized_workspace"],
        }), encoding="utf-8")
        (stage_root / "role.md").write_text(f"# {stage}\n", encoding="utf-8")

    planned = migrate_project(tmp_path, "demo", "Demo", False)
    applied = migrate_project(tmp_path, "demo", "Demo", True)
    repeated = migrate_project(tmp_path, "demo", "Demo", True)

    assert planned["status"] == "planned"
    assert applied["status"] == "applied"
    assert repeated["status"] == "already_v2"
    assert yaml.safe_load((root / "project.yaml").read_text(encoding="utf-8"))[
        "layout_version"
    ] == 2
    pm = yaml.safe_load((root / "agents" / "pm" / "config.yaml").read_text(encoding="utf-8"))
    assert pm["schema_version"] == "agent-config.v2"
    assert "handoff_to_agent" in pm["tools"]
    assert (root / "agents" / "review" / "role.md").is_file()
    assert (root / "agent-config.yaml").is_file()
