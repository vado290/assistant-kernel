"""Enable Project credential tools and Skill for every active Runtime v2 Project."""
from __future__ import annotations

import argparse
from pathlib import Path

import yaml

from assistant_kernel.services.project_credentials import ProjectCredentialsStore


TOOLS = (
    "list_project_credentials",
    "get_project_credential",
    "request_project_credential",
    "delete_project_credential",
)
SKILL = {"name": "assistant-project-credentials", "source": "kernel"}


def skill_name(item: object) -> str:
    return str(item.get("name") if isinstance(item, dict) else item)


def atomic_yaml(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        yaml.safe_dump(value, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    temporary.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, default=Path("/home/v/AI/assistant-data"))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    directory_path = args.data_root / "config" / "project-directory.yaml"
    directory = yaml.safe_load(directory_path.read_text(encoding="utf-8")) or {}
    projects = [
        str(item["project_id"]) for item in directory.get("projects") or []
        if str(item.get("status") or "active") == "active"
    ]
    changed: list[Path] = []
    store = ProjectCredentialsStore(args.data_root)
    for project_id in projects:
        vault = args.data_root / "vaults" / project_id
        for agent in ("pm", "requirements", "implementation", "review"):
            path = vault / "agents" / agent / "config.yaml"
            if not path.is_file():
                raise FileNotFoundError(f"active Project agent config is missing: {path}")
            config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            before = yaml.safe_dump(config, allow_unicode=True, sort_keys=False)
            tools = list(config.get("tools") or [])
            for tool in TOOLS:
                if tool not in tools:
                    tools.append(tool)
            config["tools"] = tools
            skills = config.get("skills")
            if agent == "pm":
                items = list(skills or [])
                if not any(skill_name(item) == SKILL["name"] for item in items):
                    items.append(dict(SKILL))
                config["skills"] = items
            else:
                skills = dict(skills or {})
                required = list(skills.get("required") or [])
                if not any(skill_name(item) == SKILL["name"] for item in required):
                    required.insert(1, dict(SKILL))
                skills["required"] = required
                config["skills"] = skills
            after = yaml.safe_dump(config, allow_unicode=True, sort_keys=False)
            if after != before:
                changed.append(path)
                if args.apply:
                    atomic_yaml(path, config)
        credential_path = store.path(project_id)
        if not credential_path.exists():
            changed.append(credential_path)
            if args.apply:
                store.ensure(project_id)
    print(
        "PROJECT_CREDENTIALS_SYNC_"
        + ("OK" if args.apply else "PREVIEW")
        + f" projects={','.join(projects)} files={len(changed)}"
    )


if __name__ == "__main__":
    main()
