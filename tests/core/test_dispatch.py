"""Phase 2a: the dispatcher drives runs end to end with scripted executors."""
from __future__ import annotations

import asyncio
from collections import defaultdict, deque

from assistant_kernel.core.dispatch import Dispatcher

from .sim import key_of, next_step, pm, report


class Scripts:
    """Executors that return scripted results; a callable script is awaited with (run, ctx)."""

    def __init__(self):
        self.queues = defaultdict(deque)
        self.seen: list[str] = []

    def add(self, key, *results):
        self.queues[key].extend(results)

    async def execute(self, run, ctx):
        key = key_of(run)
        self.seen.append(key)
        result = self.queues[key].popleft()
        if callable(result):
            result = await result(run, ctx)
        if isinstance(result, Exception):
            raise result
        return result

    def executors(self):
        return {t: self.execute for t in ("pm_turn", "supervisor", "validation", "publish")}


async def settle(dispatcher: Dispatcher, limit: int = 50) -> None:
    """Start everything runnable and wait until nothing is running or runnable."""
    for _ in range(limit):
        if await dispatcher.claim_and_start() is not None:
            continue
        if dispatcher.inflight:
            await dispatcher.drain()
            continue
        # A run may have been re-queued between the claim and the in-flight check: confirm once more.
        if await dispatcher.claim_and_start() is None and not dispatcher.inflight:
            return
    raise AssertionError("dispatcher did not settle")


async def test_dispatcher_runs_a_task_through_its_cards(kernel, sim):
    scripts = Scripts()
    d = Dispatcher(kernel, scripts.executors())
    task_id = (await kernel.receive("p1", role="pm", text="写一份说明"))["task_id"]
    scripts.add("supervisor:evaluate", next_step("requirements", "分析需求"))
    await settle(d)
    await kernel.act("p1", (await sim.item("p1"))["item_id"], "execute")
    scripts.add("pm_turn", pm("completed", report=report("task_contract", "r1")))
    scripts.add("supervisor:evaluate", {"decision": "propose_complete", "message": "研究型任务已完成"})
    await settle(d)
    item = await sim.item("p1", task_id)
    assert item["content"]["decision"] == "complete"
    await kernel.act("p1", item["item_id"], "approve")
    assert (await sim.task(task_id))["state"] == "completed"
    assert scripts.seen == ["supervisor:evaluate", "pm_turn", "supervisor:evaluate"]
    await sim.assert_consistent()


async def test_two_projects_execute_concurrently(kernel, sim):
    scripts = Scripts()
    d = Dispatcher(kernel, scripts.executors())
    for project in ("p1", "p2"):
        await kernel.receive(project, role="pm", text=f"{project} 的任务")
    scripts.add("supervisor:evaluate", next_step("requirements"), next_step("requirements"))
    await settle(d)
    for project in ("p1", "p2"):
        await kernel.act(project, (await sim.item(project))["item_id"], "execute")

    both_running = asyncio.Event()
    started = []

    async def slow_pm(run, ctx):
        started.append(run["project_id"])
        if len(started) == 2:
            both_running.set()
        await asyncio.wait_for(both_running.wait(), timeout=10)
        return pm("completed")

    scripts.add("pm_turn", slow_pm, slow_pm)
    scripts.add("supervisor:evaluate", next_step("requirements"), next_step("requirements"))
    await settle(d)
    assert sorted(started) == ["p1", "p2"]


async def test_executor_errors_are_retried_then_handed_to_the_user(kernel, sim):
    scripts = Scripts()
    d = Dispatcher(kernel, scripts.executors())
    task_id = (await kernel.receive("p1", role="pm", text="任务"))["task_id"]
    scripts.add("supervisor:evaluate", next_step("requirements"))
    await settle(d)
    await kernel.act("p1", (await sim.item("p1"))["item_id"], "execute")
    scripts.add("pm_turn", RuntimeError("codex crashed"), RuntimeError("codex crashed"), RuntimeError("codex crashed"))
    await settle(d)
    item = await sim.item("p1", task_id)
    assert item["content"]["decision"] == "run_failed" and "codex crashed" in item["content"]["message"]


async def test_cancelled_task_stops_its_running_executor(kernel, sim):
    scripts = Scripts()
    d = Dispatcher(kernel, scripts.executors(), lease_seconds=3)
    task_id = (await kernel.receive("p1", role="pm", text="长任务"))["task_id"]
    scripts.add("supervisor:evaluate", next_step("requirements"))
    await settle(d)
    await kernel.act("p1", (await sim.item("p1"))["item_id"], "execute")

    entered = asyncio.Event()

    async def never_finishes(run, ctx):
        entered.set()
        await asyncio.sleep(60)

    scripts.add("pm_turn", never_finishes)
    run = await d.claim_and_start()
    await entered.wait()
    await kernel.cancel_task(task_id)
    await asyncio.wait_for(d.drain(), timeout=10)   # heartbeat notices the cancel and stops the executor
    async with kernel.db.tx() as c:
        status = (await (await c.execute("SELECT status FROM core.runs WHERE run_id=%s", (run["run_id"],))).fetchone())["status"]
    assert status == "cancelled"
    assert (await sim.task(task_id))["state"] == "cancelled"


async def test_model_cap_limits_parallel_model_runs(kernel, sim):
    scripts = Scripts()
    d = Dispatcher(kernel, scripts.executors(), model_cap=1)
    release = asyncio.Event()

    async def wait_release(run, ctx):
        await release.wait()
        return next_step("requirements")

    for project in ("p1", "p2"):
        await kernel.receive(project, role="pm", text="x")
    scripts.add("supervisor:evaluate", wait_release, wait_release)
    assert await d.claim_and_start() is not None
    assert await d.claim_and_start() is None          # the valve holds the second model run back
    release.set()
    await settle(d)
    assert await sim.item("p1") and await sim.item("p2")


async def test_serve_stops_gracefully(kernel, sim):
    scripts = Scripts()
    d = Dispatcher(kernel, scripts.executors(), poll_seconds=0.05)
    await kernel.receive("p1", role="pm", text="x")
    scripts.add("supervisor:evaluate", next_step("requirements"))
    stop = asyncio.Event()
    serving = asyncio.create_task(d.serve(stop))
    for _ in range(100):
        if await sim.item("p1"):
            break
        await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(serving, timeout=10)
    assert await sim.item("p1") is not None
