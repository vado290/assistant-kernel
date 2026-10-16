"""Round 2 / R2 rollout: bring project skill copies the projects never edited up to the R2 templates.

Copies installed by the R1 migration carry no fingerprint yet, so their R1 template body fingerprints are
listed here. A copy whose body differs from both its recorded fingerprint and this list was edited by the
project: it is kept as it is and reported (docs/ROUND2_PLAN.md R2.5).

    python scripts/round2_r2_vaults.py --data-root /home/v/AI/assistant-data kernel trading-risk-bot ...
"""
from __future__ import annotations

import argparse
from pathlib import Path

from assistant_kernel.core import skills

R1_TEMPLATE_BODIES = {
    "assistant-agent-design": "a31a45970c06093d",
    "assistant-capability-maintenance": "f4913b1061e0e13a",
    "assistant-evolution-review": "014f8f2541e4dc8e",
    "assistant-git-delivery": "63fcb1da67582b63",
    "assistant-goal-supervisor": "c97ed13c9c5be39e",
    "assistant-history-retrieval": "b7b40d3a693aaf53",
    "assistant-implementation": "3553c684bf8cf66c",
    "assistant-kernel-maintenance": "cf43e1f2a4cc0d96",
    "assistant-mcp-design": "49ac6bfc6e6ac26c",
    "assistant-operation": "43be5be6d051f4e8",
    "assistant-project-agent": "361f6485eae5bde3",
    "assistant-project-chartering": "023d093025ed0ae4",
    "assistant-project-structure": "ed6a1ac6eb2f7011",
    "assistant-release-audit": "f6bd48b5e1b81154",
    "assistant-requirements": "5fb37eada261e274",
    "assistant-shared-context": "efedb319815fc1ee",
    "assistant-solution-design": "f54c456f8a24ccb9",
    "assistant-source-scope": "d271d9a3e609c476",
    "assistant-subagent-orchestration": "320b56e7fa0d284b",
    "assistant-task-continuity": "0dd505510e43a112",
    "assistant-triage-routing": "6bd3f84cab885cc8",
    "assistant-user-questions": "ada9a23192492b68",
    "assistant-volcengine-models": "3e291d1f4fddf9d2",
    "assistant-workflow-design": "09e9d248912d33af",
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("projects", nargs="+")
    parser.add_argument("--data-root", required=True, type=Path)
    args = parser.parse_args()
    for project_id in args.projects:
        vault = args.data_root / "vaults" / project_id
        refreshed, kept = skills.refresh_unmodified(vault, R1_TEMPLATE_BODIES)
        print(f"== {project_id}: refreshed {len(refreshed)} {refreshed}")
        if kept:
            print(f"   kept (edited by the project): {kept}")


if __name__ == "__main__":
    main()
