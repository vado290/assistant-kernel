"""ROUND3 S2: domain brief and user profile archived on requirements approval, subagent delegations recorded."""
from __future__ import annotations

import json

from assistant_kernel.core import drafts, skills
from assistant_kernel.core.executors.common import write_document
from assistant_kernel.core.executors.digest import render_digest
from assistant_kernel.core.vault_sync import sync_vaults

from .sim import next_step
from .test_dispatch import settle
from .test_pm_executor import dispatcher, editing, start_requirements_turn  # noqa: F401  (dispatcher is a fixture)

CONTRACT = ("# 需求\n\n## 2 目标\n日报\n\n## 10 背景信息存档\n无\n\n"
            "## 11 用户画像观察\n- 决策风格 · 倾向直接接受推荐 · 回合 2、3 都选了推荐项 · [推断]\n")


async def approved(kernel, settings, markdown: str, *, brief: str | None = None, user: str = "u-42"):
    task_id = (await kernel.receive("p1", role="pm", text="做一个日报工具", user_id=user))["task_id"]
    docs = {"task_contract": write_document(settings, "p1", str(task_id), "r1", "task_contract", {"markdown": markdown}, "pm")}
    if brief:
        docs["domain_brief"] = write_document(settings, "p1", str(task_id), "r1", "domain_brief", {"markdown": brief}, "pm")
    async with kernel.db.tx() as c:
        await c.execute("""UPDATE core.tasks SET documents=%s::jsonb,
                           approvals=jsonb_build_object('requirements', jsonb_build_object('doc_sha', %s::text))
                           WHERE task_id=%s""",
                        (json.dumps(docs, default=str), docs["task_contract"]["sha256"], task_id))
    return task_id


async def test_profile_and_brief_are_archived_once_on_approval(kernel, kernel_settings, project_files):
    await approved(kernel, kernel_settings, CONTRACT, brief="## 0 定位参数与有效期\n零售 · 上海 · 小型\n")
    await sync_vaults(kernel, kernel_settings)
    await sync_vaults(kernel, kernel_settings)   # idempotent
    profile = (project_files / "knowledge" / "user-profile.md").read_text("utf-8")
    assert profile.count("倾向直接接受推荐") == 1 and "倾向直接接受推荐" in skills.section_text(profile, "User u-42")
    brief = (project_files / "knowledge" / "domain-brief.md").read_text("utf-8")
    assert brief.startswith("# Industry brief") and brief.count("零售 · 上海 · 小型") == 1

    # a later task's newer research replaces the brief; the old one is kept under knowledge/archive/
    await approved(kernel, kernel_settings, CONTRACT.replace("回合 2、3", "回合 4"), brief="## 0 定位参数与有效期\n零售 · 北京\n")
    await sync_vaults(kernel, kernel_settings)
    assert "北京" in (project_files / "knowledge" / "domain-brief.md").read_text("utf-8")
    assert any("上海" in p.read_text("utf-8") for p in (project_files / "knowledge" / "archive").glob("domain-brief-*.md"))
    assert (project_files / "knowledge" / "user-profile.md").read_text("utf-8").count("倾向直接接受推荐") == 2


async def test_no_observation_writes_no_profile(kernel, kernel_settings, project_files):
    await approved(kernel, kernel_settings, CONTRACT.split("## 11")[0] + "## 11 用户画像观察\n无\n")
    await sync_vaults(kernel, kernel_settings)
    assert not (project_files / "knowledge" / "user-profile.md").exists()


async def test_brief_draft_profile_context_and_delegations(kernel, sim, dispatcher, fake_harness,  # noqa: F811
                                                           kernel_settings, project_files):
    (project_files / "knowledge").mkdir(exist_ok=True)
    (project_files / "knowledge" / "user-profile.md").write_text("# 用户画像\n\n## 用户 u-42\n- 偏好选择题\n", "utf-8")
    task_id = await start_requirements_turn(kernel, sim, dispatcher)
    reply = {"outcome": "needs_input", "summary": "调研后提问", "document": None,
             "questions": [{"id": "q1", "text": "门店有几家？"}],
             "delegations": [{"name": "industry-research", "goal": "零售日报调研", "result": "已写行业速写"},
                             "不是对象的条目会被丢弃"]}
    fake_harness.script("pm", editing(kernel_settings, reply, content="## 1 行业速写\n便利店日报常看客单价",
                                      kind=drafts.BRIEF))
    dispatcher.scripts.add("supervisor:evaluate", {"decision": "relay_pm_questions", "message": "请回答",
                                                    "questions": [{"id": "c1", "text": "门店有几家？", "covers": ["q1"]}]})
    await settle(dispatcher)
    assert "偏好选择题" in fake_harness.calls[-1].prompt                    # the profile reaches the PM
    task = await sim.task(task_id)
    assert task["documents"]["domain_brief"]["sha256"]                      # the brief was saved without approval
    async with kernel.db.tx() as c:
        run = await (await c.execute("SELECT result FROM core.runs WHERE task_id=%s AND run_type='pm_turn'",
                                     (task_id,))).fetchone()
    assert run["result"]["delegations"] == [{"name": "industry-research", "goal": "零售日报调研", "result": "已写行业速写"}]
    digest = await kernel.project_digest("p1")
    assert digest["delegations"] == [("industry-research", 1)] and "industry-research × 1" in render_digest(digest)

    # the ledger shows the delegation to whoever takes over
    await kernel.act("p1", (await sim.item("p1", task_id))["item_id"], "submit", {"answers": {"c1": "3 家"}})
    fake_harness.script("pm", {"outcome": "completed", "summary": "好", "questions": [], "document": None})
    dispatcher.scripts.add("supervisor:evaluate", next_step("requirements"))
    await settle(dispatcher)
    from assistant_kernel.core.executors.state import state_path
    assert 'Subagent "industry-research": 零售日报调研 → 已写行业速写' in state_path(kernel_settings, "p1", task_id).read_text("utf-8")


def test_brief_is_editable_only_in_requirements():
    assert drafts.BRIEF in drafts.editable("requirements") and drafts.BRIEF not in drafts.editable("design")
    assert drafts.NOTES in drafts.editable("design")


async def test_pm_executor_checks_the_charter_of_a_creation_task(kernel, sim, dispatcher, fake_harness,  # noqa: F811
                                                                 kernel_settings):
    from .test_bootstrap import CHARTER
    task = await kernel.create_task("p1", goal="创建销售日报项目", origin="user", kind="bootstrap")
    dispatcher.scripts.add("supervisor:evaluate", next_step("requirements", "商定章程"))
    await settle(dispatcher)
    await kernel.act("p1", (await sim.item("p1", task["task_id"]))["item_id"], "execute")
    fake_harness.script("pm", editing(kernel_settings, {"outcome": "completed", "summary": "章程已写好", "questions": [],
                                                        "document": {"kind": "task_contract"}}, content=CHARTER))
    dispatcher.scripts.add("supervisor:evaluate", next_step("requirements"))
    await settle(dispatcher)
    assert "Creation parameters" in fake_harness.calls[-1].prompt and "Kernel parses directly" in fake_harness.calls[-1].prompt
    contract = (await sim.task(task["task_id"]))["documents"]["task_contract"]
    assert contract["spec"]["project_id"] == "sales-report" and "spec_error" not in contract
