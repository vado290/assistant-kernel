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


async def test_evolution_supervisor_reads_queued_basis_and_original_report_without_ledger(
        kernel, dispatcher, fake_harness, kernel_settings, project_files):
    from psycopg.types.json import Jsonb
    from assistant_kernel.core.executors.common import write_document
    from assistant_kernel.core.executors.supervisor import project_situation
    from assistant_kernel.services.document_runtime import TaskDocumentService

    parent = await kernel.start_evolution("p1")
    proposal = write_document(kernel_settings, "p1", str(parent["task_id"]), "source", "improvement_proposal",
        {"markdown": "# ORIGINAL_PROPOSAL_EVIDENCE\nP1 保留定制，只修加载\nP2 只审角色，不改加载"}, "pm")
    async with kernel.db.tx() as c:
        await c.execute("UPDATE core.tasks SET documents=%s WHERE task_id=%s",
                        (Jsonb({"improvement_proposal": proposal}), parent["task_id"]))
    await kernel.cancel_task(parent["task_id"])
    current = await kernel.start_evolution("p1")
    basis = "P1 原批准边界：" + "必须保留定制；" * 150 + "BASIS_END_MUST_SURVIVE"
    children = []
    for goal, reason, ref in (("优化运营技能", basis, "P1"),
                              ("优化运营技能", "P2_SCOPE_ONLY_REVIEW_ROLE", "P2"),
                              ("加载故障修复", "同一 P1 已有任务，先核对", "P1")):
        children.append(await kernel.create_task("p1", goal=goal, reason=reason, origin="evolution",
                        parent_task_id=parent["task_id"], source_ref=f"review:{str(parent['task_id'])[:8]}:{ref}"))
    legacy = await kernel.create_task("p1", goal="legacy no basis", origin="user")
    assert all(t["state"] == "queued" for t in [*children, legacy])
    assert all(not (project_files / "runs" / str(t["task_id"]) / "state.md").exists() for t in children)
    fake_harness.script("pm", {"outcome": "completed", "summary": "核对已有任务",
        "document": {"kind": "improvement_proposal", "markdown": "## 上期效果复核\n[]"}})
    fake_harness.script("supervisor", {"decision": "propose_complete", "message": "已有任务承接"})
    await settle(dispatcher)
    call = next(c for c in reversed(fake_harness.calls) if "· Supervisor" in c.prompt)
    assert "BASIS_END_MUST_SURVIVE" in call.prompt and "P2_SCOPE_ONLY_REVIEW_ROLE" in call.prompt
    assert call.prompt.count("ORIGINAL_PROPOSAL_EVIDENCE") == 1  # shared parent read once
    assert f"review:{str(parent['task_id'])[:8]}:P1" in call.prompt
    assert "排队中，不依赖 state.md" in call.prompt and '"source_proposal_ref": null' in call.prompt
    assert "read_project_document" not in call.scoped_tools  # default Supervisor tools were not widened
    rows = await kernel.recent_tasks("p1", exclude=current["task_id"], include_basis=True)
    row = next(r for r in rows if r["task_id"] == children[0]["task_id"])
    assert TaskDocumentService(kernel_settings).read("p1", row["source_proposal_ref"])["payload"]["markdown"].startswith("# ORIGINAL")
    for child in children:
        assert (await kernel.get_task(child["task_id"]))["state"] == "queued"  # evidence does not merge/change tasks
    normal = await project_situation(kernel, kernel_settings, "p1", {**current, "kind": "delivery"})
    assert "BASIS_END_MUST_SURVIVE" not in normal and "ORIGINAL_PROPOSAL_EVIDENCE" not in normal
    defaults = await kernel.recent_tasks("p1", exclude=current["task_id"])
    assert all("source_proposal_ref" not in row and "reason" not in row for row in defaults)


@pytest.mark.parametrize("fault", ["missing", "bad_hash", "foreign", "foreign_parent"])
async def test_evolution_parent_report_failure_is_explicit_and_project_scoped(
        kernel, kernel_settings, project_files, fault):
    from psycopg.types.json import Jsonb
    from assistant_kernel.core.executors.common import write_document
    from assistant_kernel.core.executors.supervisor import project_situation
    parent = await kernel.start_evolution("p2" if fault == "foreign_parent" else "p1")
    doc = write_document(kernel_settings, "p2" if fault in {"foreign", "foreign_parent"} else "p1", str(parent["task_id"]),
                         "r", "improvement_proposal", {"markdown": "UNREADABLE_SOURCE_MUST_NOT_LEAK"}, "pm")
    if fault == "missing":
        (kernel_settings.data_root / "vaults" / "p1" / doc["ref"]["uri"].split("vault://p1/", 1)[1]).unlink()
    elif fault == "bad_hash":
        doc["ref"]["sha256"] = "0" * 64
    async with kernel.db.tx() as c:
        await c.execute("UPDATE core.tasks SET documents=%s WHERE task_id=%s", (Jsonb({"improvement_proposal": doc}), parent["task_id"]))
    await kernel.cancel_task(parent["task_id"])
    current = await kernel.start_evolution("p1")
    await kernel.create_task("p1", goal="queued", origin="evolution", parent_task_id=parent["task_id"], source_ref="P1")
    prompt = await project_situation(kernel, kernel_settings, "p1", current)
    assert "UNREADABLE_SOURCE_MUST_NOT_LEAK" not in prompt
    assert "安排 PM 在 operate 定向补证" in prompt
    assert ('"source_proposal_ref": null' if fault == "foreign_parent" else "缺失、校验失败或无正文：尚未核实") in prompt


async def test_evolution_original_report_expansion_is_bounded_with_continuation(kernel, kernel_settings, project_files):
    from psycopg.types.json import Jsonb
    from assistant_kernel.core.executors.common import write_document
    from assistant_kernel.core.executors.supervisor import project_situation
    parents = []
    for index in range(6):
        task = await kernel.start_evolution("p1")
        doc = write_document(kernel_settings, "p1", str(task["task_id"]), f"r{index}", "improvement_proposal",
                             {"markdown": f"REPORT_BODY_{index}\n" + "x" * 8100 + "UNSHOWN_END"}, "pm")
        async with kernel.db.tx() as c:
            await c.execute("UPDATE core.tasks SET documents=%s WHERE task_id=%s",
                            (Jsonb({"improvement_proposal": doc}), task["task_id"]))
        await kernel.cancel_task(task["task_id"])
        parents.append((task, doc))
    current = await kernel.start_evolution("p1")
    for parent, doc in parents:
        await kernel.create_task("p1", goal="queued", reason="retained basis", origin="evolution",
                                 parent_task_id=parent["task_id"], source_ref="P1")
    prompt = await project_situation(kernel, kernel_settings, "p1", current)
    assert prompt.count("REPORT_BODY_") == 5 and "UNSHOWN_END" not in prompt
    assert prompt.count("正文超过 8000 字符，已截断") == 5
    assert "原报告正文入口超过 5 份" in prompt and "安排 PM 在 operate 定向补证" in prompt
    assert all(doc["ref"]["sha256"] in prompt for _, doc in parents)
