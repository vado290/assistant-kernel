"""Phase 6: the weekly review runs unattended, works from Kernel's digest, and the user decides on follow-ups."""
from __future__ import annotations

import pytest

import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb

from assistant_kernel.core.dispatch import Dispatcher
from assistant_kernel.core.executors.common import read_document
from assistant_kernel.core.executors.pm import execute_pm
from assistant_kernel.core.gateway import render
from assistant_kernel.core.vault_sync import sync_vaults

from .sim import next_step
from .test_dispatch import Scripts, settle


@pytest.fixture
def dispatcher(kernel, kernel_settings, fake_harness, project_files):
    (project_files / "knowledge").mkdir()
    (project_files / "knowledge" / "charter.md").write_text("# 日报 · 项目章程\n\n## 4 组织与行业背景\n- 行业：零售\n",
                                                             "utf-8")
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
                               "document": {"kind": "improvement_proposal", "markdown": "# 改进建议\n1. 默认周报\n## 上期效果复核\n```yaml\n[]\n```"}})
    dispatcher.scripts.add("supervisor:evaluate", {"decision": "propose_followups", "message": "建议排队",
                                                    "tasks": [{"goal": "日报工具默认生成周报", "reason": "P1 用户把日报改成了周报"},
                                                              "需求阶段先问报告周期"]})
    await settle(dispatcher)
    prompt = fake_harness.calls[-1].prompt
    assert "PM turn (weekly review)" in prompt and "改成周报" in prompt and "行业：零售" in prompt
    assert "Skill assistant-evolution-review" in prompt and "Skill assistant-requirements" not in prompt
    assert fake_harness.calls[-1].multi_agent_enabled
    assert fake_harness.calls[-1].max_concurrent_subagents == 3
    assert fake_harness.calls[-1].sandbox == "read-only"
    assert "Self-contained input" in prompt and "database summary has no readable file, attach the relevant original text" in prompt
    assert "of writing domain_brief does not apply" in prompt and "evolution: reviewing the agents' own roles and skills" in prompt
    assert "[] does not prove" in prompt and "config fingerprints do not keep old role texts" in prompt
    evidence = (await kernel.evolution_context("p1"))["recent_agent_runs"]
    assert any(r["run_type"] == "pm_turn" and r["config_sha256"]
               and any(s.startswith("assistant-evolution-review@") for s in r["skills"]) for r in evidence)
    task = await sim.task(evolution["task_id"])
    assert read_document(kernel_settings, "p1", task["documents"]["improvement_proposal"]).startswith("# 改进建议")

    item = await sim.item("p1", evolution["task_id"])
    shown = render.render_item(item)["text"]
    assert "1. 日报工具默认生成周报 (reason: P1 用户把日报改成了周报)" in shown and "2. 需求阶段先问报告周期" in shown
    await kernel.act("p1", item["item_id"], "approve")
    assert (await sim.task(evolution["task_id"]))["close_reason"] == "followups_queued"
    async with kernel.db.tx() as c:
        rows = list(await (await c.execute(
            "SELECT goal, origin, state, reason FROM core.tasks WHERE parent_task_id=%s ORDER BY created_at",
            (evolution["task_id"],))).fetchall())
    assert [(r["goal"], r["origin"]) for r in rows] == [("日报工具默认生成周报", "evolution"), ("需求阶段先问报告周期", "evolution")]
    assert [r["state"] for r in rows] == ["supervising", "queued"]
    assert [r["reason"] for r in rows] == ["P1 用户把日报改成了周报", None]

    # every finished task leaves exactly one record in the change stream (ROUND4)
    records = await kernel.iterations("p1")
    assert sorted(r["kind"] for r in records) == ["review", "task"]          # the cancelled task and the review
    review = next(r for r in records if r["kind"] == "review")
    assert "日报工具默认生成周报" in review["followups"] and review["source"] == "user"
    assert next(r for r in records if r["kind"] == "task")["outcome"] == "stopped (cancelled_by_user)"


def project_files_root(settings):
    return settings.data_root / "vaults" / "p1"


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


async def test_review_material_lists_template_updates_and_other_projects(kernel, kernel_settings, project_files):
    from assistant_kernel.core import skills
    from assistant_kernel.core.executors.digest import evolution_material
    skills.install_templates(project_files, frozenset())
    copy = project_files / "agents" / "pm" / ".agents" / "skills" / "assistant-requirements" / "SKILL.md"
    copy.write_text(copy.read_text("utf-8").replace('\n  version: "', '\n  version: "0.0.', 1), "utf-8")   # older copy
    text = await evolution_material(kernel, kernel_settings, "p1")
    assert "Shared templates were updated" in text and "assistant-requirements" in text and "How the other projects are doing" not in text
    (project_files / "project.yaml").write_text(
        "schema_version: project.v2\nproject_id: p1\nlayout_version: 2\ncapabilities: [kernel_maintenance]\n", "utf-8")
    await kernel.receive("p2", role="pm", text="p2 的一个需求")
    text = await evolution_material(kernel, kernel_settings, "p1")
    assert "How the other projects are doing" in text and "- p2 (improvements): 1 tasks" in text and "p2 的一个需求" in text


async def test_effect_reference_error_can_be_corrected_in_operate(kernel, sim, dispatcher, fake_harness):
    task = await kernel.start_evolution("p1")
    fake_harness.script("pm", {"outcome": "completed", "document": {"kind": "improvement_proposal",
                               "markdown": "## 上期效果复核\n- {iteration: deadbeef, status: effective, note: 缺实际记录}"}})
    dispatcher.scripts.add("supervisor:evaluate", next_step("operate", "按反馈补正记录引用"))
    await settle(dispatcher)
    saved = await sim.task(task["task_id"])
    assert "match no single record" in saved["documents"]["improvement_proposal"]["effect_review_error"]
    await kernel.act("p1", (await sim.item("p1"))["item_id"], "execute")
    fake_harness.script("pm", {"outcome": "completed", "document": {"kind": "improvement_proposal", "markdown": "## 上期效果复核\n[]"}})
    dispatcher.scripts.add("supervisor:evaluate", {"decision": "propose_complete", "message": "无需要复核的有效记录"})
    await settle(dispatcher)
    assert "effect_review_error" not in (await sim.task(task["task_id"]))["documents"]["improvement_proposal"]
    await kernel.act("p1", (await sim.item("p1"))["item_id"], "approve")
    assert (await sim.task(task["task_id"]))["state"] == "completed"


async def test_operating_handoff_references_and_followup_source_stay_project_scoped(kernel, kernel_settings, project_files):
    from psycopg.types.json import Jsonb
    from assistant_kernel.core.executors.common import write_document
    from assistant_kernel.core.executors.digest import evolution_material
    from assistant_kernel.core.executors.state import render_state
    release = await kernel.create_task("p1", goal="published increment", origin="user")
    requirement = write_document(kernel_settings, "p1", str(release["task_id"]), "r1", "task_contract", {"markdown": "approved scope"}, "pm")
    async with kernel.db.tx() as c:
        await c.execute("UPDATE core.tasks SET documents=%s WHERE task_id=%s", (Jsonb({"task_contract": requirement}), release["task_id"]))
        await c.execute("UPDATE core.projects SET operational_baseline=%s WHERE project_id='p1'", (Jsonb({"task_id": str(release["task_id"]), "bundle_sha": "git:bound"}),))
    text = await evolution_material(kernel, kernel_settings, "p1")
    assert requirement["sha256"] in text and "the latest increment" in text and "git:bound" in text
    proposal = write_document(kernel_settings, "p1", str(release["task_id"]), "r2", "improvement_proposal", {"markdown": "P1 with acceptance and evidence"}, "pm")
    async with kernel.db.tx() as c:
        await c.execute("UPDATE core.tasks SET documents=%s WHERE task_id=%s", (Jsonb({"improvement_proposal": proposal}), release["task_id"]))
    child = await kernel.create_task("p1", goal="follow up", origin="evolution", parent_task_id=release["task_id"], source_ref="review:source:P1")
    ledger = await kernel.task_ledger(child["task_id"])
    assert ledger["source_proposal"] == proposal["ref"]
    assert proposal["sha256"] in render_state(child, ledger, {})
    foreign = await kernel.create_task("p2", goal="other project", origin="evolution", parent_task_id=release["task_id"])
    assert (await kernel.task_ledger(foreign["task_id"]))["source_proposal"] is None


@pytest.mark.parametrize("activity", ["job", "service", "recent_failure", "pending", "missing_measure"])
async def test_operational_activity_triggers_one_review_with_concurrent_catchup(kernel, activity):
    from assistant_kernel.core import machine
    sunday = datetime(2027, 1, 3, 13, tzinfo=ZoneInfo("Asia/Shanghai"))
    async with kernel.db.tx() as c:
        if activity in {"pending", "missing_measure"}:
            await machine.insert_iteration(c, "p1", None, kind="task", title="old effect", reason="r",
                expected_effect="fresh daily data", measure="output timestamp" if activity == "pending" else "")
            await c.execute("UPDATE core.iterations SET created_at=%s, effect_status='pending' WHERE project_id='p1'",
                            (sunday - timedelta(days=90),))
        else:
            await c.execute("""INSERT INTO core.jobs(project_id,job_id,kind,spec,manifest_sha,status,last_at,last_status)
                VALUES ('p1','daily',%s,'{}','x',%s,%s,%s)""",
                ("service" if activity == "service" else "job",
                 "paused" if activity == "recent_failure" else "enabled",
                 sunday - timedelta(hours=1) if activity == "recent_failure" else None,
                 "failed" if activity == "recent_failure" else None))
    assert await kernel.schedule_evolution(sunday - timedelta(seconds=1)) == []
    results = await asyncio.gather(*(kernel.schedule_evolution(sunday + timedelta(hours=20)) for _ in range(3)))
    assert sum((r for r in results), []) == ["p1"]
    async with kernel.db.tx() as c:
        row = (await (await c.execute("SELECT * FROM core.tasks WHERE kind='evolution'")).fetchall())[0]
    assert row["idempotency_key"] == "evolution:p1:2026-W53"  # Monday retains Sunday's ISO year/week
    await kernel.cancel_task(row["task_id"])
    assert await kernel.schedule_evolution(sunday + timedelta(hours=23)) == []
    assert await kernel.schedule_evolution(sunday + timedelta(hours=24)) == []


async def test_evolution_optout_inactivity_and_existing_review(kernel):
    sunday = datetime(2026, 12, 27, 13, tzinfo=ZoneInfo("Asia/Shanghai"))
    async with kernel.db.tx() as c:
        for project, status in (("p1", "paused"), ("p2", "retired")):
            await c.execute("""INSERT INTO core.jobs(project_id,job_id,kind,spec,manifest_sha,status)
                VALUES (%s,'old','job','{}','x',%s)""", (project, status))
    assert await kernel.schedule_evolution(sunday) == []
    async with kernel.db.tx() as c:
        await c.execute("UPDATE core.jobs SET status='enabled'")
        await c.execute("UPDATE core.projects SET settings=%s WHERE project_id='p2'", (Jsonb({"weekly_evolution": False}),))
    manual = await kernel.start_evolution("p1", idempotency_key="manual:one")
    assert await kernel.schedule_evolution(sunday) == []
    assert (await kernel.start_evolution("p1", idempotency_key="manual:one"))["task_id"] == manual["task_id"]
    # A distinct explicit request may still queue another review.
    again = await kernel.start_evolution("p1", idempotency_key="manual:two")
    assert again["state"] == "queued" and again["task_id"] != manual["task_id"]


async def test_inactive_project_not_reawakened_by_effect_backlog(kernel):
    from assistant_kernel.core import machine
    async with kernel.db.tx() as c:
        await machine.insert_iteration(c, "p1", None, kind="task", title="effect", reason="r", expected_effect="x", measure="y")
        await c.execute("UPDATE core.projects SET status='archived' WHERE project_id='p1'")
    assert await kernel.schedule_evolution(datetime(2026, 12, 27, 13, tzinfo=ZoneInfo("Asia/Shanghai"))) == []


async def test_evolution_context_is_bounded_and_exposes_original_proposal(kernel, kernel_settings, project_files):
    from assistant_kernel.core.executors.common import write_document
    from assistant_kernel.core.executors.digest import evolution_material
    review = await kernel.start_evolution("p1")
    proposal = write_document(kernel_settings, "p1", str(review["task_id"]), "r", "improvement_proposal", {"markdown": "P1"}, "pm")
    async with kernel.db.tx() as c:
        await c.execute("UPDATE core.tasks SET documents=%s WHERE task_id=%s",
                        (Jsonb({"improvement_proposal": proposal}), review["task_id"]))
        for project in ("p1", "p2"):
            for number in range(3):
                await c.execute("""INSERT INTO core.jobs(project_id,job_id,kind,spec,manifest_sha,status)
                    VALUES (%s,%s,'job','{}','x','paused')""", (project, f"{project}-{number}"))
    child = await kernel.create_task("p1", goal="repair", origin="evolution", parent_task_id=review["task_id"],
                                     source_ref="review:source:P1", reason="retain customized role; verify result")
    data = await kernel.evolution_context("p1", limit=1)
    assert data["runtime_truncated"] and data["tasks_truncated"]
    assert data["runtime"][0]["status"] == "paused" and data["runtime"][0]["job_id"].startswith("p1-")
    assert data["recent_proposals"][0]["proposal_ref"] == proposal["ref"]
    text = await evolution_material(kernel, kernel_settings, "p1")
    assert str(child["task_id"]) in text and "review:source:P1" in text
    assert "retain customized role" in text and "p2-" not in text
    assert "without a way to measure it" in text and "[] does not prove" in text
