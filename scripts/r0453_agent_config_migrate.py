"""0.45.3 one-off: clean every project's agent configs (system map gen-20261019-fef3d31, F2/F3).

- Drop keys the loader never reads (aliases, entrypoints, triggers, scope_profile, mcp_servers, skills, harness.sandbox):
  they suggested settings that have no effect.
- Keep only current tool names; the retired names did nothing. The PM gets the three tools its skills use but its old
  list lacked (read_project_document, vault_list, vault_read_text).
A project's own narrowing (current names it chose to leave out) is kept. Only the changed config files are committed.

Run with the prod venv after deploying 0.45.3:
  python scripts/r0453_agent_config_migrate.py [--dry-run]
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import yaml

from assistant_kernel.config import KernelSettings
from assistant_kernel.core import projectgit
from assistant_kernel.core.executors.common import MVP_TOOLS

LEGACY = ("aliases", "entrypoints", "triggers", "scope_profile", "mcp_servers", "skills")
PM_ADDED = ("read_project_document", "vault_list", "vault_read_text")


def clean(data: dict[str, Any], agent: str) -> tuple[dict[str, Any], list[str]]:
    """The cleaned config and what changed (empty when nothing did)."""
    data, notes = dict(data), []
    for key in LEGACY:
        if key in data:
            data.pop(key)
            notes.append(f"删除字段 {key}")
    if isinstance(data.get("harness"), dict) and "sandbox" in data["harness"]:
        data["harness"] = {k: v for k, v in data["harness"].items() if k != "sandbox"}
        notes.append("删除字段 harness.sandbox")
    tools = [str(t) for t in data.get("tools") or []]
    kept = [t for t in tools if t in MVP_TOOLS]
    if len(kept) < len(tools):
        notes.append(f"删除 {len(tools) - len(kept)} 个已退役的工具名：{', '.join(t for t in tools if t not in MVP_TOOLS)}")
    if agent == "pm":
        added = [t for t in PM_ADDED if t not in kept]
        if added:
            notes.append(f"增加工具：{', '.join(added)}")
        kept += added
    data["tools"] = kept
    return data, notes


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    options = parser.parse_args()
    vaults = KernelSettings.from_environment().data_root / "vaults"
    for vault in sorted(p for p in vaults.iterdir() if (p / "project.yaml").is_file()):
        changed = []
        for path in sorted((vault / "agents").glob("*/config.yaml")):
            data = yaml.safe_load(path.read_text("utf-8")) or {}
            new, notes = clean(data, path.parent.name)
            if not notes:
                continue
            print(f"\n=== {vault.name}/{path.relative_to(vault).as_posix()}")
            for note in notes:
                print(f"  - {note}")
            print(f"  结果 tools：{new['tools']}")
            if not options.dry_run:
                path.write_text(yaml.safe_dump(new, allow_unicode=True, sort_keys=False), "utf-8")
                changed.append(path.relative_to(vault).as_posix())
        if changed:
            commit = projectgit.commit_paths(vault, changed, "清理 agent 配置：删除不生效的字段和已退役的工具名（Kernel 0.45.3）")
            print(f"  已提交 {vault.name}：{commit}")
    print("\n（dry-run：没有写入任何文件）" if options.dry_run else "\n完成")


if __name__ == "__main__":
    main()
