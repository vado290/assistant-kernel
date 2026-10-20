"""0.45.7 one-off: give every project's Supervisor up to 5 minutes per run (it was 90 seconds; with the task ledger and
working tools, real runs already took up to 65 s). Only a config still at the old default (90) is changed; a value a
project chose itself is kept. Only the changed config files are committed.

Run with the prod venv after deploying 0.45.7:
  python scripts/r0457_supervisor_timeout.py [--dry-run]
"""
from __future__ import annotations

import argparse

import yaml

from assistant_kernel.config import KernelSettings
from assistant_kernel.core import projectgit

OLD, NEW = 90, 300


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    dry = parser.parse_args().dry_run
    vaults = KernelSettings.from_environment().data_root / "vaults"
    for vault in sorted(p for p in vaults.iterdir() if (p / "project.yaml").is_file()):
        path = vault / "agents" / "supervisor" / "config.yaml"
        if not path.is_file():
            continue
        data = yaml.safe_load(path.read_text("utf-8")) or {}
        harness = data.get("harness") if isinstance(data.get("harness"), dict) else {}
        current = harness.get("timeout_seconds", data.get("timeout_seconds"))
        if current != OLD:
            print(f"{vault.name}: timeout_seconds = {current}，不是旧默认值 {OLD}，保留")
            continue
        print(f"{vault.name}: timeout_seconds {OLD} → {NEW}")
        if dry:
            continue
        if "timeout_seconds" in harness:
            data["harness"] = {**harness, "timeout_seconds": NEW}
        else:
            data["timeout_seconds"] = NEW
        path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), "utf-8")
        commit = projectgit.commit_paths(vault, ["agents/supervisor/config.yaml"],
                                         "Supervisor 每轮限时改为 5 分钟（Kernel 0.45.7）")
        print(f"  已提交：{commit}")
    print("\n（dry-run：没有写入任何文件）" if dry else "\n完成")


if __name__ == "__main__":
    main()
