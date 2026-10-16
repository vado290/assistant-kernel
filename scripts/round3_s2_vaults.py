"""Round 3 / S2 rollout: install the new template skills into existing projects and bring the copies the
projects never edited up to the current templates (edited copies are kept and reported).

    python scripts/round3_s2_vaults.py --data-root /home/v/AI/assistant-data kernel trading-risk-bot ...
"""
from __future__ import annotations

import argparse
from pathlib import Path

from assistant_kernel.core import skills


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("projects", nargs="+")
    parser.add_argument("--data-root", required=True, type=Path)
    args = parser.parse_args()
    for project_id in args.projects:
        vault = args.data_root / "vaults" / project_id
        installed = skills.install_templates(vault, skills.project_config(vault)["capabilities"])
        refreshed, kept = skills.refresh_unmodified(vault)
        print(f"== {project_id}: installed {installed}; refreshed {refreshed}")
        if kept:
            print(f"   kept (edited by the project): {kept}")


if __name__ == "__main__":
    main()
