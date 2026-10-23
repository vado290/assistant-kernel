"""Phase 2c: PM and Supervisor both run through their real executors against a scripted model."""
from __future__ import annotations

import pytest

from assistant_kernel.core.dispatch import Dispatcher
from assistant_kernel.core.executors import build_executors

from .test_dispatch import settle


@pytest.fixture
def dispatcher(kernel, kernel_settings, fake_harness, project_files):
    return Dispatcher(kernel, build_executors(), services={"settings": kernel_settings, "harness": fake_harness})


def propose(target, instruction="继续", message="下一步"):
    return {"decision": "propose_next", "target_stage": target, "instruction": instruction, "message": message}


async def test_mainline_with_real_executors(kernel, sim, dispatcher, fake_harness):
    task_id = (await kernel.receive("p1", role="pm", text="帮我做个日报工具"))["task_id"]
    fake_harness.script("supervisor", propose("requirements", "梳理日报工具的需求", "我先让 PM 梳理需求"))
    await settle(dispatcher)
    start_prompt = fake_harness.calls[-1].prompt
    assert "帮我做个日报工具" in start_prompt and "Reason for this evaluation: start" in start_prompt
    assert "- design: not possible now" in start_prompt                     # Kernel tells Supervisor the gates
    item = await sim.item("p1", task_id)
    assert item["content"]["message"] == "我先让 PM 梳理需求"
    await kernel.act("p1", item["item_id"], "execute")

    fake_harness.script("pm", {"outcome": "needs_input", "summary": "要确认读者", "document": None,
                               "questions": [{"id": "q1", "text": "日报给谁看？"}]})
    fake_harness.script("supervisor", {"decision": "relay_pm_questions", "message": "请回答",
                                       "questions": [{"id": "c1", "text": "日报主要给谁看？", "options": ["自己", "团队"],
                                                      "covers": ["q1"]}]})
    await settle(dispatcher)
    assert "[q1] (required) 日报给谁看？" in fake_harness.calls[-1].prompt
    await kernel.act("p1", (await sim.item("p1", task_id))["item_id"], "submit", {"answers": {"c1": "团队"}})

    fake_harness.script("pm", {"outcome": "completed", "summary": "需求完成", "questions": [],
                               "document": {"kind": "task_contract", "markdown": "# 需求\n- 给团队看"}})
    fake_harness.script("supervisor", propose("design", "按需求设计方案", "需求已写好，请确认后进入设计"))
    await settle(dispatcher)
    evaluate_prompt = fake_harness.calls[-1].prompt
    assert "# 需求\n- 给团队看" in evaluate_prompt
    assert "design: possible (executing it also approves requirements)" in evaluate_prompt
    item = await sim.item("p1", task_id)
    assert item["content"]["approve"] == ["requirements"]
    await kernel.act("p1", item["item_id"], "execute")
    task = await sim.task(task_id)
    assert (task["state"], task["stage"]) == ("pm", "design")
    await sim.assert_consistent()


async def test_consultation_reply_does_not_touch_the_task(kernel, sim, dispatcher, fake_harness):
    task_id = (await kernel.receive("p1", role="pm", text="做日报"))["task_id"]
    fake_harness.script("supervisor", propose("requirements"))
    await settle(dispatcher)
    before = await sim.task(task_id)
    await kernel.receive("p1", role="supervisor", text="现在卡在哪一步？")
    fake_harness.script("supervisor", {"reply": "在等你确认需求分析的指令。"})
    await settle(dispatcher)
    consult_prompt = fake_harness.calls[-1].prompt
    assert "现在卡在哪一步？" in consult_prompt and "[next_step]" in consult_prompt
    assert (await sim.outbound("p1"))[-1]["body"]["text"] == "在等你确认需求分析的指令。"
    assert (await sim.task(task_id))["state_revision"] == before["state_revision"]


async def test_new_pm_channel_input_is_reviewed_against_the_card(kernel, sim, dispatcher, fake_harness):
    task_id = (await kernel.receive("p1", role="pm", text="做日报"))["task_id"]
    fake_harness.script("supervisor", propose("requirements", "做日报需求"))
    await settle(dispatcher)
    old = await sim.item("p1", task_id)
    await kernel.receive("p1", role="pm", text="不对，改成周报")
    fake_harness.script("supervisor", {"decision": "upgrade", "item": propose("requirements", "做周报需求", "已改为周报")})
    await settle(dispatcher)
    assert "不对，改成周报" in fake_harness.calls[-1].prompt
    new = await sim.item("p1", task_id)
    assert new["item_id"] != old["item_id"] and new["content"]["instruction"] == "做周报需求"


async def test_invalid_supervisor_output_is_retried(kernel, sim, dispatcher, fake_harness):
    task_id = (await kernel.receive("p1", role="pm", text="做日报"))["task_id"]
    fake_harness.script("supervisor", {"decision": "jump_to_build"}, propose("requirements"))
    await settle(dispatcher)
    assert (await sim.item("p1", task_id))["kind"] == "next_step"
