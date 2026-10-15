"""Phase 2b: the PM executor turns a scripted model reply into a state-machine result."""
from __future__ import annotations

import pytest

from assistant_kernel.core.dispatch import Dispatcher
from assistant_kernel.core.executors.common import read_document
from assistant_kernel.core.executors.pm import execute_pm

from .sim import next_step
from .test_dispatch import Scripts, settle


@pytest.fixture
def dispatcher(kernel, kernel_settings, fake_harness, project_files):
    scripts = Scripts()
    executors = {**scripts.executors(), "pm_turn": execute_pm}
    d = Dispatcher(kernel, executors, services={"settings": kernel_settings, "harness": fake_harness})
    d.scripts = scripts
    return d


async def start_requirements_turn(kernel, sim, d, text="做一个日报工具"):
    task_id = (await kernel.receive("p1", role="pm", text=text))["task_id"]
    d.scripts.add("supervisor:evaluate", next_step("requirements", "梳理日报需求"))
    await settle(d)
    await kernel.act("p1", (await sim.item("p1"))["item_id"], "execute")
    return task_id


async def test_pm_question_turn(kernel, sim, dispatcher, fake_harness):
    task_id = await start_requirements_turn(kernel, sim, dispatcher)
    fake_harness.script("pm", {"outcome": "needs_input", "summary": "需要确认读者",
                               "questions": [{"id": "q1", "text": "日报给谁看？", "options": ["个人", "团队"]}],
                               "document": None})
    dispatcher.scripts.add("supervisor:evaluate", {"decision": "relay_pm_questions", "message": "请确认",
                                                    "questions": [{"id": "c1", "text": "日报给谁看？", "covers": ["q1"]}]})
    await settle(dispatcher)
    prompt = fake_harness.calls[-1].prompt
    assert "梳理日报需求" in prompt and "做一个日报工具" in prompt and "当前阶段：需求分析" in prompt
    assert fake_harness.calls[-1].physical_session_id is None       # first turn of a stage is fresh
    item = await sim.item("p1", task_id)
    assert item["deliver_to"] == "pm" and item["content"]["pm_questions"][0]["id"] == "q1"

    # the answer goes straight back to the PM, in the same native session
    await kernel.act("p1", item["item_id"], "submit", {"answers": {"c1": "团队"}})
    fake_harness.script("pm", {"outcome": "completed", "summary": "需求已整理",
                               "questions": [], "document": {"kind": "task_contract", "markdown": "# 日报需求\n给团队看"}})
    dispatcher.scripts.add("supervisor:evaluate", next_step("design", "开始设计"))
    await settle(dispatcher)
    last = fake_harness.calls[-1]
    assert "日报给谁看？：团队" in last.prompt
    first_session = next(s for s, key in fake_harness.session_keys.items() if key == "pm")
    assert last.physical_session_id == first_session          # same stage → same native session
    task = await sim.task(task_id)
    contract = task["documents"]["task_contract"]
    assert read_document(dispatcher.services["settings"], "p1", contract) == "# 日报需求\n给团队看"
    assert (await sim.item("p1", task_id))["content"]["approve"] == ["requirements"]


async def test_non_json_reply_is_fixed_in_the_same_session(kernel, sim, dispatcher, fake_harness):
    task_id = await start_requirements_turn(kernel, sim, dispatcher)
    fake_harness.script("pm", "我已经分析完了（忘了输出 JSON）",
                        {"outcome": "completed", "summary": "完成", "questions": [], "document": None})
    dispatcher.scripts.add("supervisor:evaluate", next_step("requirements"))
    await settle(dispatcher)
    first, fix = fake_harness.calls[-2:]
    assert first.physical_session_id is None and fix.physical_session_id is not None   # the fix resumes the turn
    assert "不是一个合法的 JSON" in fix.prompt
    assert (await sim.task(task_id))["state"] == "awaiting_user"


async def test_unparseable_reply_is_retried(kernel, sim, dispatcher, fake_harness):
    task_id = await start_requirements_turn(kernel, sim, dispatcher)
    good = {"outcome": "completed", "summary": "完成", "questions": [], "document": None}
    fake_harness.script("pm", "坏输出", "还是坏输出", good)
    dispatcher.scripts.add("supervisor:evaluate", next_step("requirements"))
    await settle(dispatcher)
    async with kernel.db.tx() as c:
        run = await (await c.execute("SELECT attempt, status FROM core.runs WHERE run_type='pm_turn'")).fetchone()
    assert (run["attempt"], run["status"]) == (2, "succeeded")
    assert (await sim.task(task_id))["state"] == "awaiting_user"


async def test_document_of_another_stage_is_rejected(kernel, sim, dispatcher, fake_harness):
    await start_requirements_turn(kernel, sim, dispatcher)
    wrong = {"outcome": "completed", "summary": "x", "questions": [],
             "document": {"kind": "solution_design", "markdown": "设计"}}
    fake_harness.script("pm", wrong, wrong, wrong)
    await settle(dispatcher)
    item = await sim.item("p1")
    assert item["content"]["decision"] == "run_failed"   # three invalid results → the user decides


async def test_build_without_candidate_files_gets_a_bound_marker(kernel, sim, dispatcher, fake_harness, kernel_settings):
    from assistant_kernel.core.executors.pm import seal_candidates
    assert seal_candidates(kernel_settings, "p1", "00000000-0000-0000-0000-000000000000", "abc") == "no-files:abc"
