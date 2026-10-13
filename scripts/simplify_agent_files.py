"""Convert active Agent files to the direct-edit first-release format."""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path

import yaml

from assistant_kernel.config import KernelSettings


HISTORY_DIRECTORY_NAMES = {
    "config-history",
    "config-versions",
    "skill-history",
}


def active_config_paths(root: Path) -> list[Path]:
    paths: list[Path] = []
    direct = root / "agent-config.yaml"
    if direct.is_file():
        paths.append(direct)
    stages = root / "stages"
    if stages.is_dir():
        paths.extend(sorted(stages.glob("*/agent-config.yaml")))
    return paths


def simplify(path: Path) -> None:
    current = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    harness = current.get("harness_defaults") or {}
    required = (current.get("skills") or {}).get("required") or []
    skills = [
        str(item.get("skill") if isinstance(item, dict) else item)
        for item in required
    ]
    stage = path.parent.name if path.parent.parent.name == "stages" else "manager"
    simple = {
        "agent_key": str(current.get("agent_id") or "project-manager"),
        "reasoning_effort": str(harness.get("reasoning_effort") or "medium"),
        "timeout_seconds": int(harness.get("timeout_seconds") or 600),
        "sandbox": str(
            harness.get("sandbox")
            or ("workspace-write" if stage == "implementation" else "read-only")
        ),
        "skills": skills,
    }
    if harness.get("provider"):
        simple["provider"] = str(harness["provider"])
    if harness.get("model"):
        simple["model"] = str(harness["model"])
    path.write_text(
        yaml.safe_dump(simple, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not args.apply:
        print("DRY_RUN_OK; add --apply after creating a recoverable backup")
        return
    settings = KernelSettings.from_environment()
    roots = []
    vaults = settings.data_root / "vaults"
    if vaults.is_dir():
        roots.extend(sorted(path for path in vaults.iterdir() if path.is_dir() and path.name not in {"system", "butler-maintenance"}))
    rewritten = 0
    removed = 0
    for root in roots:
        for path in active_config_paths(root):
            simplify(path)
            rewritten += 1
        candidates = sorted(
            (
                path
                for path in root.rglob("*")
                if path.is_dir()
                and (
                    path.name in HISTORY_DIRECTORY_NAMES
                    or (path.name in {"history", "versions"} and path.parent.name == "roles")
                )
            ),
            key=lambda item: len(item.parts),
            reverse=True,
        )
        for path in candidates:
            if path.exists():
                shutil.rmtree(path)
                removed += 1
    print(f"AGENT_FILES_SIMPLIFIED_OK; rewritten={rewritten}; removed_history_dirs={removed}")


if __name__ == "__main__":
    main()
