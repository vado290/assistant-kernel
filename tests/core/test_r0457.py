"""0.45.7: Supervisor writes down what it infers the user meant (inferred_intent): the user sees it on the card, the PM
gets it as a hypothesis, the ledger keeps it for the next stateless run. Supervisor's prompt also carries the current time
and user and the project's situation, and Supervisor runs get up to 5 minutes."""
from __future__ import annotations

import pytest
import yaml

from assistant_kernel.core import machine
from assistant_kernel.core.dispatch import Dispatcher
from assistant_kernel.core.executors import build_executors
from assistant_kernel.core.gateway import render

from .test_dispatch import settle
from .test_r0453 import TEMPLATE

GUESS = {"text": "用户真正在意的是月底对账来得及", "basis": "回合 1 原话“月底总是来不及”"}


@pytest.fixture
def dispatcher(kernel, kernel_settings, fake_harness, project_files):
    return Dispatcher(kernel, build_executors(), services={"settings": kernel_settings, "harness": fake_harness})


def propose(**extra):
    return {"decision": "propose_next", "target_stage": "requirements", "instruction": "梳理日报需求",
            "message": "先梳理需求", **extra}


def test_inferences_are_cleaned_and_capped():
    raw = [GUESS, "只有一句话的推测", {"text": "  "}, {"text": "x" * 500, "basis": "y"}, {"text": "第五条"}, 42]
    found = machine._inferred(raw)
    assert len(found) == machine.INFERRED_ITEMS and found[1] == {"text": "只有一句话的推测", "basis": ""}
    assert len(found[2]["text"]) == 300 and machine._inferred("not a list") == []


async def test_the_inference_reaches_card_pm_and_ledger(kernel, sim, dispatcher, fake_harness, kernel_settings):
    task_id = (await kernel.receive("p1", role="pm", text="月底总是来不及，帮我做个日报", user_id="u-7"))["task_id"]
    fake_harness.script("supervisor", propose(inferred_intent=[GUESS]))
    await settle(dispatcher)
    item = await sim.item("p1", task_id)
    assert item["content"]["inferred_intent"] == [GUESS]
    shown = render.render_item(item)["text"]
    assert "我的推测（未经你确认" in shown and "月底对账来得及" in shown and "依据：回合 1" in shown

    await kernel.act("p1", item["item_id"], "execute")
    fake_harness.script("pm", {"outcome": "completed", "summary": "需求初稿", "questions": [],
                               "document": {"kind": "task_contract", "markdown": "# 需求\n- 日报"}})
    fake_harness.script("supervisor", propose(message="再看一轮"))
    await settle(dispatcher)
    pm_prompt = next(c.prompt for c in fake_harness.calls if c.prompt.startswith("# 项目 p1 · PM 回合"))
    assert "Supervisor 的推测（假设，不是需求" in pm_prompt and "月底对账来得及" in pm_prompt
    ledger_line = "Supervisor 推测（假设）：用户真正在意的是月底对账来得及"
    assert ledger_line in fake_harness.calls[-1].prompt          # the next stateless Supervisor run sees it in the ledger
    from assistant_kernel.core.executors.pm import STAGE_NAMES
    from assistant_kernel.core.executors.state import render_state
    task = await sim.task(task_id)                               # and so does every later PM session (state.md)
    assert ledger_line in render_state(task, await kernel.task_ledger(task_id), STAGE_NAMES)


async def test_a_revised_card_shows_supervisor_its_earlier_inference(kernel, sim, dispatcher, fake_harness):
    task_id = (await kernel.receive("p1", role="pm", text="做个日报"))["task_id"]
    fake_harness.script("supervisor", propose(inferred_intent=[GUESS]))
    await settle(dispatcher)
    await kernel.act("p1", (await sim.item("p1", task_id))["item_id"], "revise", {"text": "不是对账，是给店长看"})
    fake_harness.script("supervisor", propose(message="改好了"))
    await settle(dispatcher)
    prompt = fake_harness.calls[-1].prompt
    assert "你上次的推测（用户看过后提出了修改）" in prompt and "不是对账，是给店长看" in prompt


async def test_supervisor_knows_time_user_and_the_project_situation(kernel, sim, dispatcher, fake_harness,
                                                                     project_files):
    (project_files / "knowledge").mkdir(exist_ok=True)
    (project_files / "knowledge" / "domain-brief.md").write_text("# 行业速写\n\n## 0 定位参数与有效期\n零售 · 上海\n"
                                                                 + "细节" * 2000, "utf-8")
    earlier = (await kernel.receive("p1", role="pm", text="先做一个周报", user_id="u-7"))["task_id"]
    await kernel.cancel_task(earlier)
    task_id = (await kernel.receive("p1", role="pm", text="做个日报", user_id="u-7"))["task_id"]
    fake_harness.script("supervisor", propose())
    await settle(dispatcher)
    prompt = next(c.prompt for c in fake_harness.calls if "做个日报" in c.prompt)
    assert "当前时间：" in prompt and "当前用户：u-7（用户画像中\"用户 u-7\"一节）" in prompt
    assert "[取消] 先做一个周报" in prompt and f"runs/{earlier}/state.md" in prompt and str(task_id) not in prompt.split("其他任务")[1].split("###")[0]
    assert "零售 · 上海" in prompt and "全文在 knowledge/domain-brief.md" in prompt


def test_supervisor_runs_get_five_minutes():
    data = yaml.safe_load((TEMPLATE / "supervisor" / "config.yaml").read_text("utf-8"))
    assert data["harness"]["timeout_seconds"] == 300
