from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


STAGES = {"requirements", "implementation", "review"}


def read_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"YAML must contain an object: {path}")
    return data


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def convert_config(
    source: dict[str, Any], *, project_id: str, agent_id: str, kind: str,
) -> dict[str, Any]:
    harness_source = source.get("harness_defaults") or {}
    harness = {
        "provider": source.get("provider") or harness_source.get("provider"),
        "model": source.get("model") or harness_source.get("model"),
        "reasoning_effort": source.get("reasoning_effort")
        or harness_source.get("reasoning_effort") or "medium",
        "timeout_seconds": int(
            source.get("timeout_seconds") or harness_source.get("timeout_seconds") or 600
        ),
        "sandbox": "workspace-write" if agent_id == "implementation" else "read-only",
    }
    harness = {key: value for key, value in harness.items() if value is not None}
    tools = [str(item) for item in source.get("tools") or []]
    if agent_id == "pm":
        for tool in (
            "list_project_agents", "handoff_to_agent", "current_conversation_route",
            "list_recent_history", "list_source_discovery_roots",
            "inspect_source_path", "propose_source_scope", "activate_source_scope",
        ):
            if tool not in tools:
                tools.append(tool)
    skills = source.get("skills") or []
    if agent_id == "pm":
        skills = [
            *skills,
            {"name": "assistant-history-retrieval", "source": "kernel"},
            {"name": "assistant-source-scope", "source": "kernel"},
        ]
    result = {
        "schema_version": "agent-config.v2",
        "agent_id": agent_id,
        "agent_key": str(source.get("agent_key") or (
            f"project:{project_id}:manager" if agent_id == "pm"
            else f"project:{project_id}:{agent_id}"
        )),
        "kind": kind,
        "status": "active" if source.get("status") != "archived" else "archived",
        "display_name": "Project Manager" if agent_id == "pm" else agent_id.title(),
        "aliases": ["manager"] if agent_id == "pm" else [],
        "role": "role.md",
        "scope_profile": str(source.get("scope_profile") or "current-project"),
        "harness": harness,
        "skills": skills,
        "mcp_servers": source.get("mcp_servers") or {},
        "entrypoints": {
            "pm_handoff": agent_id == "pm",
            "feishu_slash": agent_id == "pm",
        },
        "triggers": ["user_message", "handoff"] if agent_id == "pm" else ["stage"],
        "tools": tools,
    }
    for key in ("input_schema", "output_schema"):
        if source.get(key):
            result[key] = source[key]
    return result


def migrate_project(data_root: Path, project_id: str, purpose: str, apply: bool) -> dict[str, Any]:
    root = data_root / "vaults" / project_id
    if not root.is_dir():
        raise FileNotFoundError(f"Project Vault is missing: {root}")
    project_path = root / "project.yaml"
    if project_path.is_file():
        project = read_yaml(project_path)
        if int(project.get("layout_version") or 0) == 2:
            return {"project_id": project_id, "status": "already_v2"}
        raise ValueError(f"Project already has an unsupported project.yaml: {project_id}")
    manager_config_path = root / "agent-config.yaml"
    manager_role_path = root / "role.md"
    if not manager_config_path.is_file() or not manager_role_path.is_file():
        raise FileNotFoundError(f"legacy PM files are incomplete: {project_id}")
    sources = [manager_config_path, manager_role_path]
    for stage in sorted(STAGES):
        stage_root = root / "stages" / stage
        if not (stage_root / "agent-config.yaml").is_file() or not (stage_root / "role.md").is_file():
            raise FileNotFoundError(f"legacy Stage files are incomplete: {project_id}/{stage}")
        sources.extend([stage_root / "agent-config.yaml", stage_root / "role.md"])
    report = {
        "schema_version": "project-runtime-v2-migration.v1",
        "project_id": project_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_files": [
            {"path": path.relative_to(root).as_posix(), "sha256": sha256(path)}
            for path in sources
        ],
        "target_layout_version": 2,
        "status": "planned" if not apply else "applied",
    }
    if not apply:
        return report
    agents_root = root / "agents"
    if agents_root.exists():
        raise FileExistsError(f"agents directory already exists without project.yaml: {project_id}")
    build_root = root / "runtime" / "project-v2-build"
    if build_root.exists():
        raise FileExistsError(f"unfinished v2 build directory requires review: {build_root}")
    build_agents = build_root / "agents"
    manager_config = convert_config(
        read_yaml(manager_config_path), project_id=project_id, agent_id="pm", kind="manager"
    )
    atomic_write(
        build_agents / "pm" / "config.yaml",
        yaml.safe_dump(manager_config, sort_keys=False, allow_unicode=True),
    )
    shutil.copy2(manager_role_path, build_agents / "pm" / "role.md")
    for stage in sorted(STAGES):
        source_root = root / "stages" / stage
        config = convert_config(
            read_yaml(source_root / "agent-config.yaml"),
            project_id=project_id,
            agent_id=stage,
            kind="stage",
        )
        config["entrypoints"] = {"pm_handoff": False, "feishu_slash": False}
        atomic_write(
            build_agents / stage / "config.yaml",
            yaml.safe_dump(config, sort_keys=False, allow_unicode=True),
        )
        shutil.copy2(source_root / "role.md", build_agents / stage / "role.md")
    build_agents.replace(agents_root)
    atomic_write(
        project_path,
        yaml.safe_dump({
            "schema_version": "project.v2",
            "project_id": project_id,
            "layout_version": 2,
            "status": "active",
            "display_name": project_id,
            "purpose": purpose,
            "routing": {
                "aliases": [], "topics": [], "typical_requests": [],
                "exclusions": [], "related_projects": [],
            },
            "default_agent": "pm",
        }, sort_keys=False, allow_unicode=True),
    )
    report_root = root / "runtime" / "migrations"
    report_root.mkdir(parents=True, exist_ok=True)
    atomic_write(
        report_root / "project-runtime-v2.json",
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2),
    )
    if build_root.exists():
        build_root.rmdir()
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--project", action="append", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    directory_path = args.data_root / "config" / "project-directory.yaml"
    directory = read_yaml(directory_path)
    purposes = {
        str(item["project_id"]): str(item.get("purpose") or "")
        for item in directory.get("projects") or []
    }
    results = [
        migrate_project(
            args.data_root, project_id, purposes.get(project_id, ""), args.apply
        )
        for project_id in args.project
    ]
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
