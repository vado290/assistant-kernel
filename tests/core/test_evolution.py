"""Phase 6: the weekly review runs unattended, works from Kernel's digest, and the user decides on follow-ups."""
from __future__ import annotations

import pytest

from assistant_kernel.core.dispatch import Dispatcher
from assistant_kernel.core.executors.common import read_document
from assistant_kernel.core.executors.pm import execute_pm
from assistant_kernel.core.gateway import render

from .sim import next_step
from .test_dispatch import Scripts, settle


@pytest.fixture
def dispatcher(kernel, kernel_settings, fake_harness, project_files):
    (project_files / "knowledge").mkdir()
    (project_files / "knowledge" / "business-profile.md").write_text("- 行业：零售", "utf-8")
    scripts = Scripts()
    d = Dispatcher(kernel, {**scripts.executors(), "pm_turn": execute_pm},
                   services={"settings": kernel_settings, "harness": fake_harness})
    d.scripts = scripts
    return d


async def test_evolution_review_to_followups(kernel, sim, dispatcher, fake_harness, kernel_settings):
    # a week of use: the user rewrote a proposed step, then stopped the task
    task_id = (await kernel.receive("p1", role="pm", text="做一个日报工具"))["task_id"]
    dispatcher.scripts.add("supervisor:evaluate", next_step("requirements", "梳理日报需求"),
                           next_step("requirements", "梳理周报需求"))
    await settle(dispatcher)
    await kernel.act("p1", (await sim.item("p1"))["item_id"], "revise", {"text": "改成周报"})
    await settle(dispatcher)
    await kernel.cancel_task(task_id)

    evolution = await kernel.start_evolution("p1")
    assert evolution["kind"] == "evolution" and evolution["state"] == "pm"      # no click needed to start
    fake_harness.script("pm", {"outcome": "completed", "summary": "建议默认周报", "questions": [],
                               "document": {"kind": "improvement_proposal", "markdown": "# 改进建议\n1. 默认周报"}})
    dispatcher.scripts.add("supervisor:evaluate", {"decision": "propose_followups", "message": "建议排队",
                                                    "tasks": ["日报工具默认生成周报", "需求阶段先问报告周期"]})
    await settle(dispatcher)
    prompt = fake_harness.calls[-1].prompt
    assert "周度复盘" in prompt and "改成周报" in prompt and "行业：零售" in prompt and "运营阶段" in prompt
    task = await sim.task(evolution["task_id"])
    assert read_document(kernel_settings, "p1", task["documents"]["improvement_proposal"]).startswith("# 改进建议")

    item = await sim.item("p1", evolution["task_id"])
    shown = render.render_item(item)["text"]
    assert "1. 日报工具默认生成周报" in shown and "2. 需求阶段先问报告周期" in shown
    await kernel.act("p1", item["item_id"], "approve")
    assert (await sim.task(evolution["task_id"]))["close_reason"] == "followups_queued"
    async with kernel.db.tx() as c:
        rows = list(await (await c.execute(
            "SELECT goal, origin, state FROM core.tasks WHERE parent_task_id=%s ORDER BY created_at",
            (evolution["task_id"],))).fetchall())
    assert [(r["goal"], r["origin"]) for r in rows] == [("日报工具默认生成周报", "evolution"), ("需求阶段先问报告周期", "evolution")]
    assert [r["state"] for r in rows] == ["supervising", "queued"]


async def test_rejected_followups_go_back_to_the_supervisor(kernel, sim):
    evolution = await kernel.start_evolution("p1")
    sim.script("pm_turn", {"outcome": "completed", "summary": "没什么"})
    sim.script("supervisor:evaluate", {"decision": "propose_followups", "tasks": ["a"]},
               {"decision": "propose_complete", "message": "那就结束"})
    await sim.drain()
    await kernel.act("p1", (await sim.item("p1"))["item_id"], "reject", {"text": "不需要"})
    run = await kernel.claim("w1")
    assert run["input"]["reason"] == "followups_rejected" and run["input"]["text"] == "不需要"


async def test_too_many_followups_are_invalid(kernel, sim):
    await kernel.start_evolution("p1")
    sim.script("pm_turn", {"outcome": "completed", "summary": "很多"})
    bad = {"decision": "propose_followups", "tasks": [str(n) for n in range(6)]}
    sim.script("supervisor:evaluate", bad, bad, bad)
    await sim.drain()
    assert (await sim.item("p1"))["content"]["decision"] == "run_failed"
