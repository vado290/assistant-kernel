"""Structural rules of the rebuilt core: write ownership (P2) and claim exclusivity (P6/P7)."""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

from .sim import next_step

SRC = Path(__file__).parents[2] / "src" / "assistant_kernel"
OWNERS = {
    "core.tasks": {"core/machine.py"},
    "core.interactions": {"core/cards.py"},
    "core.runs": {"core/kernel.py", "core/machine.py"},
    "core.jobs": {"core/jobs.py", "core/machine.py"},   # ROUND3 S3: manifest sync and job results
    "core.iterations": {"core/machine.py"},             # ROUND4: the change stream
    "core.project_grants": {"core/machine.py"},         # ROUND4: directory grants
}


def test_only_owning_modules_write_core_tables():
    offenders = []
    for path in SRC.rglob("*.py"):
        relative = path.relative_to(SRC).as_posix()
        text = path.read_text(encoding="utf-8")
        for table, owners in OWNERS.items():
            if re.search(rf"(UPDATE|INSERT INTO|DELETE FROM)\s+{re.escape(table)}\b", text) and relative not in owners:
                offenders.append(f"{relative} writes {table}")
    assert not offenders, offenders


async def test_concurrent_claims_take_a_project_turn_once(kernel, sim):
    await kernel.receive("p1", role="pm", text="并发测试")
    sim.script("supervisor:evaluate", next_step("requirements"))
    await sim.drain()
    await kernel.act("p1", (await sim.item("p1"))["item_id"], "execute")
    claims = await asyncio.gather(*(kernel.claim(f"w{i}") for i in range(6)))
    assert len([run for run in claims if run]) == 1
    await sim.assert_consistent()
