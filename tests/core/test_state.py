"""ROUND3 S1: the task state ledger, the PM's working notes and session rotation."""
from __future__ import annotations

from datetime import datetime, timezone

from assistant_kernel.core import drafts, machine
from assistant_kernel.core.executors.common import read_document
from assistant_kernel.core.executors.pm import STAGE_NAMES
from assistant_kernel.core.executors.state import render_state, state_path

from .sim import next_step
from .test_dispatch import settle
from .test_pm_executor import ask_then_answer, dispatcher, editing, start_requirements_turn  # noqa: F401

NOTES = "## 当前理解\n用户要团队日报\n## 下一步\n问清发送时间"


async def test_a_new_session_takes_over_from_ledger_and_notes(kernel, sim, dispatcher, fake_harness,  # noqa: F811
                                                              kernel_settings):
    reply = editing(kernel_settings, {"outcome": "completed", "summary": "读者已确认", "questions": [],
                                      "document": None}, content=NOTES, kind="working_notes")
    task_id, fresh, resumed = await ask_then_answer(kernel, sim, dispatcher, fake_harness, reply)
    assert "## Task ledger" in fresh and "no completed PM turn yet" in fresh
    assert "## Task ledger" not in resumed and f"runs/{task_id}/state.md" in resumed   # a resumed session lived it

    task = await sim.task(task_id)
    notes = task["documents"]["working_notes"]
    assert read_document(kernel_settings, "p1", notes) == NOTES
    assert task["pm_session"]["turns"] == 2 and task["pm_session"]["seen"]["working_notes"] == notes["sha256"]

    # the session has run its share of turns: the next one starts fresh from the ledger, notes and documents
    async with kernel.db.tx() as c:
        await c.execute("UPDATE core.tasks SET pm_session = pm_session || %s::jsonb WHERE task_id=%s",
                        (f'{{"turns": {machine.SESSION_TURNS}}}', task_id))
    await kernel.act("p1", (await sim.item("p1", task_id))["item_id"], "execute")
    fake_harness.script("pm", {"outcome": "completed", "summary": "已补充", "questions": [], "document": None})
    dispatcher.scripts.add("supervisor:evaluate", next_step("requirements"))
    await settle(dispatcher)
    third = fake_harness.calls[-1]
    assert third.physical_session_id is None
    assert "Q: 日报给谁看？ → A: 团队" in third.prompt   # "卡片问答" (0.45.12)
    assert "PM working notes" in third.prompt and "问清发送时间" in third.prompt
    assert (await sim.task(task_id))["pm_session"]["turns"] == 1
    ledger = state_path(kernel_settings, "p1", task_id).read_text("utf-8")   # written at the start of the turn
    assert "PM turns completed: 2" in ledger and "Card questions" in ledger and "日报给谁看？" in ledger and "working_notes" in ledger


def test_notes_are_editable_in_every_stage():
    assert drafts.editable("design") == {"solution_design", "working_notes"}
    assert drafts.editable("release") == {"working_notes"}


def test_early_turns_shrink_to_one_line():
    at = datetime(2026, 10, 16, 9, 0, tzinfo=timezone.utc)
    turns = [{"run_id": n, "created_at": at, "supplements": [],
              "input": {"stage": "requirements", "kind": "instruction", "instruction": f"指令{n}"},
              "result": {"outcome": "completed", "summary": f"小结{n}"}} for n in range(12)]
    task = {"goal": "日报", "stage": "requirements", "track": "full", "approvals": {}, "documents": {}}
    text = render_state(task, {"turns": turns, "actions": []}, STAGE_NAMES)
    assert "Turn 1 (requirements, 10-16 09:00): 小结0" in text and "指令0" not in text
    assert "指令11" in text and "PM turns completed: 12" in text


async def test_an_outcome_of_another_stage_is_corrected_in_the_same_session(kernel, sim, dispatcher,  # noqa: F811
                                                                            fake_harness):
    task_id = await start_requirements_turn(kernel, sim, dispatcher)
    fake_harness.script("pm", {"outcome": "ready_for_validation", "summary": "需求已写好", "questions": [],
                               "document": None},
                        {"outcome": "completed", "summary": "需求已写好", "questions": [], "document": None})
    dispatcher.scripts.add("supervisor:evaluate", next_step("requirements"))
    await settle(dispatcher)
    first, fix = fake_harness.calls[-2:]
    assert "only for the build stage" in fix.prompt and fix.physical_session_id is not None
    assert (await sim.task(task_id))["state"] == "awaiting_user"   # completed → Supervisor → card, not a failure
