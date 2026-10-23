"""0.45.12 (collaboration data review O1): every question card is recorded in task_events and the state ledger
shows all of them in one "卡片问答" section — including the Supervisor's own questions and the user's answers."""
from __future__ import annotations

from datetime import datetime, timezone

from assistant_kernel.core.executors.pm import STAGE_NAMES
from assistant_kernel.core.executors.state import render_state

from .sim import next_step, pm, relay
from .test_scenarios import execute_next, start_task

ASK = {"decision": "ask_user", "message": "先确认一下", "questions": [{"id": "q1", "text": "日报发给谁？"}]}


async def ledger_text(kernel, sim, task_id) -> str:
    return render_state(await sim.task(task_id), await kernel.task_ledger(task_id), STAGE_NAMES)


async def test_supervisor_questions_and_answers_reach_the_ledger(kernel, sim):
    task_id = await start_task(kernel, sim)
    sim.script("supervisor:evaluate", ASK)
    await sim.drain()
    item = await sim.item("p1", task_id)
    await kernel.act("p1", item["item_id"], "submit", {"answers": {"q1": "门店经理和区域经理"}})
    await execute_next(kernel, sim, task_id, "requirements")
    text = await ledger_text(kernel, sim, task_id)
    assert "Supervisor 提问（已回答）" in text and "问：日报发给谁？ → 答：门店经理和区域经理" in text


async def test_pm_questions_are_listed_once(kernel, sim):
    task_id = await start_task(kernel, sim)
    await execute_next(kernel, sim, task_id, "requirements")
    sim.script("pm_turn", pm("needs_input", questions=[{"id": "q1", "text": "用什么格式？"}]))
    sim.script("supervisor:evaluate", relay("q1"))
    await sim.drain()
    item = await sim.item("p1", task_id)
    await kernel.act("p1", item["item_id"], "submit", {"answers": {"c_q1": "Markdown"}})
    sim.script("pm_turn", pm("completed"))
    sim.script("supervisor:evaluate", next_step("requirements"))
    await sim.drain()
    text = await ledger_text(kernel, sim, task_id)
    assert "PM 提问，经 Supervisor 转述（已回答）" in text and text.count("Markdown") == 1
    assert "用户回答：" not in text and "PM 提出了 1 个问题" in text


async def test_unanswered_questions_say_why(kernel, sim):
    task_id = await start_task(kernel, sim)
    sim.script("supervisor:evaluate", ASK)
    await sim.drain()
    first = await sim.item("p1", task_id)
    await kernel.receive("p1", role="pm", text="其实先别管发给谁")           # card review replaces the card
    sim.script("supervisor:review_input", {"decision": "upgrade", "item": {**ASK, "questions": [
        {"id": "q2", "text": "先只做汇总表吗？"}]}})
    await sim.drain()
    second = await sim.item("p1", task_id)
    assert second["item_id"] != first["item_id"]
    text = await ledger_text(kernel, sim, task_id)
    assert "（已被新卡片替换）" in text and "（等待回答）" in text
    await kernel.act("p1", second["item_id"], "revise", {"text": "先只做汇总表"})
    text = await ledger_text(kernel, sim, task_id)
    assert "未回答：用户改为提出修改「先只做汇总表」" in text


def test_tasks_without_card_records_render_as_before():
    task = {"goal": "日报", "stage": "requirements", "track": "full", "approvals": {}, "documents": {},
            "state": "pm", "task_id": "t"}
    turn = {"input": {"kind": "answers", "stage": "requirements", "questions": [{"id": "q1", "text": "格式？"}],
                      "answers": {"q1": "表格"}},
            "result": {"outcome": "completed", "summary": "好"}, "created_at": datetime.now(timezone.utc),
            "supplements": []}
    text = render_state(task, {"turns": [turn], "actions": []}, STAGE_NAMES)
    assert "用户回答：格式？ → 表格" in text and "卡片问答" not in text
