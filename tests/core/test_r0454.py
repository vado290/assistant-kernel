"""0.45.4 (system map gen-20261019-69b5264): a separate new request while a task waits for the user becomes a
"create a new task?" card that queues behind it (F8); /version names the latest migration (F4)."""
from __future__ import annotations

from pathlib import Path

from assistant_kernel import __schema_version__

from .sim import next_step
from .test_scenarios import start_task

SQL = Path(__file__).parents[2] / "src" / "assistant_kernel" / "core" / "sql"


def test_the_reported_schema_version_is_the_latest_migration():
    latest = max(p.name[:4] for p in SQL.glob("[0-9][0-9][0-9][0-9]_*.sql"))
    assert __schema_version__ == f"core6.{latest}"


def new_task_items(card):
    return [i for i in (card or {}).get("items", []) if i["content"].get("decision") == "new_task"]


async def test_a_separate_request_while_a_task_waits_queues_behind_it(kernel, sim):
    first = await start_task(kernel, sim)
    sim.script("supervisor:evaluate", next_step("requirements"))
    await sim.drain()
    waiting = await sim.item("p1", first)

    await kernel.receive("p1", role="pm", text="另外帮我写一个 BTC 网格策略的回测")
    sim.script("supervisor:review_input", {"decision": "keep",
                                           "propose_new_task": {"goal": "写 BTC 网格策略回测", "message": "这是一件新工作"}})
    await sim.drain()
    assert (await sim.item("p1", first))["item_id"] == waiting["item_id"]      # the waiting card is untouched
    [proposal] = new_task_items(await kernel.card("p1"))
    assert "之后" in proposal["content"]["message"] and "/cancel" in proposal["content"]["message"]

    created = await kernel.act("p1", proposal["item_id"], "approve")
    second = created["task_id"]
    assert (await sim.task(second))["state"] == "queued" and (await sim.task(first))["state"] == "awaiting_user"

    await kernel.cancel_task(first)                                             # the user ends the first task
    assert (await sim.task(second))["state"] != "queued"                        # the queued one starts on its own


async def test_a_plain_supplement_proposes_nothing(kernel, sim):
    first = await start_task(kernel, sim)
    sim.script("supervisor:evaluate", next_step("requirements"))
    await sim.drain()
    await kernel.receive("p1", role="pm", text="字体大一点")
    sim.script("supervisor:review_input", {"decision": "keep"})
    await sim.drain()
    assert new_task_items(await kernel.card("p1")) == [] and (await sim.item("p1", first)) is not None
