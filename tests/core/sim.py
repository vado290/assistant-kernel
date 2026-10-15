"""Phase-1 simulator: claims runs and injects scripted results instead of executing them."""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any

from assistant_kernel.core import invariants
from assistant_kernel.core.db import one
from assistant_kernel.core.kernel import Kernel


@dataclass
class Fail:
    message: str = "boom"
    retryable: bool = True


def key_of(run: dict[str, Any]) -> str:
    return run["run_type"] if run["purpose"] == "default" else f"{run['run_type']}:{run['purpose']}"


class Sim:
    def __init__(self, kernel: Kernel):
        self.k = kernel
        self.scripts: dict[str, deque] = defaultdict(deque)
        self.history: list[str] = []

    def script(self, key: str, *results: Any) -> "Sim":
        self.scripts[key].extend(results)
        return self

    async def step(self, worker: str = "w1") -> dict[str, Any] | None:
        run = await self.k.claim(worker)
        if run is None:
            return None
        key = key_of(run)
        if not self.scripts[key]:
            raise AssertionError(f"no scripted result for {key}; history={self.history}")
        result = self.scripts[key].popleft()
        self.history.append(key)
        if isinstance(result, Fail):
            await self.k.fail(run, {"message": result.message}, retryable=result.retryable)
        else:
            await self.k.complete(run, result)
        await self.assert_consistent()
        return run

    async def drain(self, limit: int = 50) -> list[str]:
        start = len(self.history)
        for _ in range(limit):
            if await self.step() is None:
                return self.history[start:]
        raise AssertionError("simulation did not settle")

    async def assert_consistent(self) -> None:
        async with self.k.db.tx() as c:
            problems = await invariants.violations(c)
        assert not problems, problems

    # -------------------------------------------------- read helpers

    async def task(self, task_id) -> dict[str, Any]:
        async with self.k.db.tx() as c:
            return await one(c, "SELECT * FROM core.tasks WHERE task_id=%s", (task_id,))

    async def item(self, project_id: str, task_id=None) -> dict[str, Any] | None:
        card = await self.k.card(project_id)
        items = (card or {}).get("items", [])
        if task_id is not None:
            items = [i for i in items if i["target"]["id"] == str(task_id)]
        assert len(items) <= 1, items
        return items[0] if items else None

    async def outbound(self, project_id: str) -> list[dict[str, Any]]:
        async with self.k.db.tx() as c:
            return list(await (await c.execute(
                "SELECT * FROM core.messages WHERE project_id=%s AND direction='outbound' ORDER BY created_at",
                (project_id,))).fetchall())


# ------------------------------------------------------------------ scripted results

def pm(outcome: str, **extra: Any) -> dict[str, Any]:
    return {"outcome": outcome, "summary": f"PM {outcome}", **extra}


def report(kind: str, sha: str, **meta: Any) -> dict[str, Any]:
    return {"kind": kind, "sha256": sha, "uri": f"vault://p/{kind}/{sha}", **meta}


def next_step(target: str, instruction: str = "继续") -> dict[str, Any]:
    return {"decision": "propose_next", "target_stage": target, "instruction": instruction, "message": "下一步"}


def relay(*pm_ids: str) -> dict[str, Any]:
    return {"decision": "relay_pm_questions", "message": "帮你整理了问题",
            "questions": [{"id": f"c_{q}", "text": f"关于 {q}", "covers": [q]} for q in pm_ids]}
