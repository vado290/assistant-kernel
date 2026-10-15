"""Acceptance scenarios S1–S22 from docs/REBUILD_PLAN.md section 9.

Phase 1 drives the state machine with injected run results (tests/core/sim.py).
Every Sim.step() asserts the three invariants: one active task per project,
one cursor per active task, one open card per project.
"""
import pytest

from assistant_kernel.core import rules
from assistant_kernel.core.cards import StaleItem

from .sim import Fail, next_step, pm, relay, report

deferred = pytest.mark.skip(reason="post-MVP: deferred by the MVP-first rule (see plan section 10)")
phase2 = pytest.mark.skip(reason="rebuild phase 2: executors + dispatcher")
phase3 = pytest.mark.skip(reason="rebuild phase 3: ingress + Discord cards")
phase6 = pytest.mark.skip(reason="rebuild phase 6: weekly evolution")


async def start_task(kernel, sim, text="做一个日报工具", project="p1"):
    received = await kernel.receive(project, role="pm", text=text)
    assert received["action"] == "created"
    return received["task_id"]


async def execute_next(kernel, sim, task_id, target, instruction="继续", *, project="p1"):
    """Supervisor proposes `target`; the user clicks execute."""
    sim.script("supervisor:evaluate", next_step(target, instruction))
    await sim.drain()
    item = await sim.item(project, task_id)
    assert item and item["kind"] == "next_step", (item, sim.history[-4:])
    await kernel.act(project, item["item_id"], "execute")
    return item


async def to_build(kernel, sim, task_id):
    """Run a task from creation to a running build turn: requirements and design approved."""
    await execute_next(kernel, sim, task_id, "requirements")
    sim.script("pm_turn", pm("completed", report=report("task_contract", "r1")))
    await execute_next(kernel, sim, task_id, "design")
    sim.script("pm_turn", pm("completed", report=report("solution_design", "d1")))
    await execute_next(kernel, sim, task_id, "build")


async def test_s01_mainline_to_completion(kernel, sim):
    task_id = await start_task(kernel, sim)
    second = await kernel.create_task("p1", goal="下一个需求", origin="user")
    assert second["state"] == "queued"

    # requirements: PM asks, Supervisor relays, user answers straight back to PM
    await execute_next(kernel, sim, task_id, "requirements", "需求分析")
    sim.script("pm_turn", pm("needs_input", questions=[{"id": "q1", "text": "给谁看？"}]))
    sim.script("supervisor:evaluate", relay("q1"))
    await sim.drain()
    item = await sim.item("p1", task_id)
    assert (item["kind"], item["deliver_to"]) == ("questions", "pm")
    await kernel.act("p1", item["item_id"], "submit", {"answers": {"c_q1": "团队"}})
    assert (await sim.task(task_id))["state"] == "pm"

    # requirements done → approve + design → approve + build
    sim.script("pm_turn", pm("completed", report=report("task_contract", "r1")))
    design_item = await execute_next(kernel, sim, task_id, "design")
    assert design_item["content"]["approve"] == ["requirements"]
    sim.script("pm_turn", pm("completed", report=report("solution_design", "d1")))
    build_item = await execute_next(kernel, sim, task_id, "build")
    assert build_item["content"]["approve"] == ["design"]

    # build → validation → independent review (fresh session) → release approval → publish
    sim.script("pm_turn", pm("ready_for_validation", report=report("implementation_report", "i1", bundle_sha="b1")))
    sim.script("validation", {"passed": True})
    await execute_next(kernel, sim, task_id, "review", "独立 Review")
    task = await sim.task(task_id)
    assert task["stage"] == "review"
    sim.script("pm_turn", pm("completed", report=report("review_report", "rv1", verdict="pass", bundle_sha="b1")))
    sim.script("supervisor:evaluate", next_step("release"))
    await sim.drain()
    release = await sim.item("p1", task_id)
    assert release["content"]["decision"] == "release"
    await kernel.act("p1", release["item_id"], "approve")
    sim.script("publish", {"outcome": "published"})
    sim.script("supervisor:evaluate", next_step("requirements"))  # the second task starts
    await sim.drain()

    task = await sim.task(task_id)
    assert task["state"] == "completed"
    assert all(rules.approval_valid(task, k) for k in ("requirements", "design", "release"))
    assert (await sim.task(second["task_id"]))["state"] == "awaiting_user"
    async with kernel.db.tx() as c:
        project = await (await c.execute("SELECT operational_baseline FROM core.projects WHERE project_id='p1'")).fetchone()
    assert project["operational_baseline"]["bundle_sha"] == "b1"


async def test_s02_pm_channel_input_while_pm_running(kernel, sim):
    task_id = await start_task(kernel, sim)
    await execute_next(kernel, sim, task_id, "requirements")
    run = await kernel.claim("w1")
    assert run["run_type"] == "pm_turn"
    late = await kernel.receive("p1", role="pm", text="补充：要支持导出")
    assert late["action"] == "attached"
    assert [m["body"]["text"] for m in await kernel.turn_inputs(run)] == ["做一个日报工具"]
    await kernel.complete(run, pm("completed"))
    await execute_next(kernel, sim, task_id, "requirements")
    run2 = await kernel.claim("w1")
    assert [m["body"]["text"] for m in await kernel.turn_inputs(run2)] == ["补充：要支持导出"]


async def test_s03_directional_input_upgrades_card(kernel, sim):
    task_id = await start_task(kernel, sim)
    sim.script("supervisor:evaluate", next_step("requirements", "按日报方向分析"))
    await sim.drain()
    old = await sim.item("p1", task_id)
    await kernel.save_draft("p1", task_id, {"c_q1": "草稿答案"})

    await kernel.receive("p1", role="pm", text="改成做周报")
    sim.script("supervisor:review_input", {"decision": "upgrade", "item": next_step("requirements", "按周报方向分析")})
    await sim.drain()
    new = await sim.item("p1", task_id)
    assert new["item_id"] != old["item_id"] and new["content"]["instruction"] == "按周报方向分析"
    assert (await kernel.card("p1"))["drafts"][str(task_id)] == {"c_q1": "草稿答案"}
    with pytest.raises(StaleItem):
        await kernel.act("p1", old["item_id"], "execute")
    assert (await sim.task(task_id))["state"] == "awaiting_user"


async def test_s04_plain_supplement_keeps_card(kernel, sim):
    task_id = await start_task(kernel, sim)
    await execute_next(kernel, sim, task_id, "requirements")
    sim.script("pm_turn", pm("needs_input", questions=[{"id": "q1", "text": "用什么格式？"}]))
    sim.script("supervisor:evaluate", relay("q1"))
    await sim.drain()
    item = await sim.item("p1", task_id)

    note = await kernel.receive("p1", role="pm", text="另外字体要大一点")
    sim.script("supervisor:review_input", {"decision": "keep"})
    await sim.drain()
    assert (await sim.item("p1", task_id))["item_id"] == item["item_id"]
    assert (await kernel.card("p1"))["supplements"][str(task_id)] == [note["message_id"]]

    await kernel.act("p1", item["item_id"], "submit", {"answers": {"c_q1": "Markdown"}})
    run = await kernel.claim("w1")
    assert run["input"]["answers"] == {"c_q1": "Markdown"}
    assert [m["body"]["text"] for m in await kernel.turn_inputs(run)] == ["另外字体要大一点"]


async def test_s05_supervisor_consult_is_read_only(kernel, sim):
    task_id = await start_task(kernel, sim)
    sim.script("supervisor:evaluate", next_step("requirements"))
    await sim.drain()
    before = await sim.task(task_id)
    await kernel.receive("p1", role="supervisor", text="现在进展到哪了？")
    sim.script("supervisor:consult", {"reply": "正在等你确认需求分析的指令"})
    await sim.drain()
    after = await sim.task(task_id)
    assert (after["state"], after["state_revision"]) == (before["state"], before["state_revision"])
    assert (await sim.outbound("p1"))[-1]["body"]["text"] == "正在等你确认需求分析的指令"


async def test_s06_new_work_from_supervisor_channel(kernel, sim):
    task_id = await start_task(kernel, sim)
    sim.script("supervisor:evaluate", next_step("requirements"))
    await sim.drain()
    await kernel.receive("p1", role="supervisor", text="另外帮我做个周报")
    sim.script("supervisor:consult", {"reply": "可以作为新任务", "propose_new_task": {"goal": "做周报"}})
    await sim.drain()
    card = await kernel.card("p1")
    proposal = next(i for i in card["items"] if i["target"]["kind"] == "consultation")
    await kernel.act("p1", proposal["item_id"], "approve")
    async with kernel.db.tx() as c:
        new = await (await c.execute("SELECT * FROM core.tasks WHERE goal='做周报'")).fetchone()
    assert new["state"] == "queued" and new["origin"] == "supervisor"
    assert (await sim.task(task_id))["state"] == "awaiting_user"


@deferred
async def test_s07_urgent_task_suspends_and_resumes():
    """Urgent task approved → active task suspended at a safe point → urgent task runs and completes →
    original task resumes through a supervisor(resume) evaluation. MVP: urgent tasks only jump the queue."""


async def test_s08_cancel_while_pm_running(kernel, sim):
    task_id = await start_task(kernel, sim)
    nxt = await kernel.create_task("p1", goal="排队任务", origin="user")
    await execute_next(kernel, sim, task_id, "requirements")
    run = await kernel.claim("w1")
    await kernel.cancel_task(task_id)
    await kernel.complete(run, pm("completed", report=report("task_contract", "r1")))
    task = await sim.task(task_id)
    assert task["state"] == "cancelled" and "task_contract" not in task["documents"]
    assert (await sim.task(nxt["task_id"]))["state"] == "supervising"
    await sim.assert_consistent()


async def test_s09_supervisor_exhausted_falls_back(kernel, sim):
    task_id = await start_task(kernel, sim)
    await execute_next(kernel, sim, task_id, "requirements")
    sim.script("pm_turn", pm("needs_input", questions=[{"id": "q1", "text": "预算多少？"}]))
    sim.script("supervisor:evaluate", Fail(), Fail(), Fail())
    await sim.drain()
    item = await sim.item("p1", task_id)
    assert item["origin"] == "kernel" and item["content"]["questions"][0]["text"] == "预算多少？"


async def test_s10_pm_exhausted_asks_user(kernel, sim):
    task_id = await start_task(kernel, sim)
    await execute_next(kernel, sim, task_id, "requirements")
    sim.script("pm_turn", Fail(), Fail(), Fail())
    await sim.drain()
    item = await sim.item("p1", task_id)
    assert item["content"]["decision"] == "run_failed"
    await kernel.act("p1", item["item_id"], "retry")
    assert (await sim.task(task_id))["state"] == "pm"


async def test_s11_validation_failure_returns_to_build(kernel, sim):
    task_id = await start_task(kernel, sim)
    await to_build(kernel, sim, task_id)
    sim.script("pm_turn", pm("ready_for_validation", report=report("implementation_report", "i1", bundle_sha="b1")))
    sim.script("validation", {"passed": False, "report": "2 tests failed"})
    await execute_next(kernel, sim, task_id, "build", "修复失败的测试")
    task = await sim.task(task_id)
    assert (task["state"], task["stage"]) == ("pm", "build")
    assert rules.move_error(task, "review") == "the current implementation has not passed validation"


async def test_s12_review_requests_design_change(kernel, sim):
    task_id = await start_task(kernel, sim)
    await to_build(kernel, sim, task_id)
    sim.script("pm_turn", pm("ready_for_validation", report=report("implementation_report", "i1", bundle_sha="b1")))
    sim.script("validation", {"passed": True})
    await execute_next(kernel, sim, task_id, "review")
    sim.script("pm_turn", pm("return_to_design"))
    await execute_next(kernel, sim, task_id, "design", "按 Review 意见修改设计")
    sim.script("pm_turn", pm("completed", report=report("solution_design", "d2")))
    item = await execute_next(kernel, sim, task_id, "build")
    assert item["content"]["approve"] == ["design"]  # the old design approval no longer counts


async def test_s13_requirements_change_invalidates_design_approval(kernel, sim):
    task_id = await start_task(kernel, sim)
    await to_build(kernel, sim, task_id)
    assert rules.approval_valid(await sim.task(task_id), "design")
    sim.script("pm_turn", pm("return_to_requirements"))
    await execute_next(kernel, sim, task_id, "requirements")
    sim.script("pm_turn", pm("completed", report=report("task_contract", "r2")))
    sim.script("supervisor:evaluate", next_step("design"))
    await sim.drain()
    task = await sim.task(task_id)
    assert not rules.approval_valid(task, "requirements") and not rules.approval_valid(task, "design")


async def test_s14_turn_budget_exceeded(kernel, sim):
    task_id = await start_task(kernel, sim)
    async with kernel.db.tx() as c:
        await c.execute("UPDATE core.tasks SET max_turns=1 WHERE task_id=%s", (task_id,))
    await execute_next(kernel, sim, task_id, "requirements")
    sim.script("pm_turn", pm("completed"))
    await execute_next(kernel, sim, task_id, "requirements")
    sim.script("pm_turn", pm("completed"))
    await sim.drain()
    item = await sim.item("p1", task_id)
    assert item["content"]["decision"] == "turn_budget"
    await kernel.act("p1", item["item_id"], "continue")
    assert (await sim.task(task_id))["state"] == "supervising"


async def test_s15_duplicate_channel_event_is_idempotent(kernel, sim):
    first = await kernel.receive("p1", role="pm", text="hi", channel="discord", external_event_id="e1")
    again = await kernel.receive("p1", role="pm", text="hi", channel="discord", external_event_id="e1")
    assert first["action"] == "created" and again == {"action": "duplicate"}
    sim.script("supervisor:evaluate", next_step("requirements"))
    await sim.drain()
    item = await sim.item("p1")
    assert (await kernel.act("p1", item["item_id"], "execute", external_event_id="click1"))["action"] == "execute"
    assert await kernel.act("p1", item["item_id"], "execute", external_event_id="click1") == {"action": "duplicate"}


async def test_s16_two_projects_run_in_parallel(kernel, sim):
    a = await start_task(kernel, sim, project="p1")
    b = await start_task(kernel, sim, project="p2")
    sim.script("supervisor:evaluate", next_step("requirements"), next_step("requirements"))
    await sim.drain()
    for project in ("p1", "p2"):
        await kernel.act(project, (await sim.item(project))["item_id"], "execute")
    first, second = await kernel.claim("w1"), await kernel.claim("w2")
    assert {first["project_id"], second["project_id"]} == {"p1", "p2"}
    assert await kernel.claim("w3") is None
    assert await sim.item("p1") is None and await sim.item("p2") is None


async def test_s17_publish_crash_goes_to_reconciliation(kernel, sim):
    task_id = await start_task(kernel, sim)
    await to_build(kernel, sim, task_id)
    sim.script("pm_turn", pm("ready_for_validation", report=report("implementation_report", "i1", bundle_sha="b1")))
    sim.script("validation", {"passed": True})
    await execute_next(kernel, sim, task_id, "review")
    sim.script("pm_turn", pm("completed", report=report("review_report", "rv1", verdict="pass", bundle_sha="b1")))
    sim.script("supervisor:evaluate", next_step("release"))
    await sim.drain()
    await kernel.act("p1", (await sim.item("p1", task_id))["item_id"], "approve")
    run = await kernel.claim("w1")
    assert run["run_type"] == "publish"
    async with kernel.db.tx() as c:
        await c.execute("UPDATE core.runs SET lease_until=now() - interval '1 second' WHERE run_id=%s", (run["run_id"],))
    assert await kernel.recover_expired() == 1
    item = await sim.item("p1", task_id)
    assert item["content"]["decision"] == "reconcile"  # never retried blindly
    await kernel.act("p1", item["item_id"], "published")
    assert (await sim.task(task_id))["state"] == "completed"


@deferred
async def test_s18_runtime_question_shares_the_card():
    """Card open for the task; a runtime program asks → new item on the same card; answering only that
    item routes the answer to the program. MVP: runtime programs are not active yet (tables empty)."""


async def test_s19_dispatcher_restart_mid_run(kernel, sim):
    task_id = await start_task(kernel, sim)
    await execute_next(kernel, sim, task_id, "requirements")
    run = await kernel.claim("w1")
    async with kernel.db.tx() as c:
        await c.execute("UPDATE core.runs SET lease_until=now() - interval '1 second' WHERE run_id=%s", (run["run_id"],))
    await kernel.recover_expired()
    again = await kernel.claim("w2")
    assert again["run_id"] == run["run_id"] and again["attempt"] == 2
    await sim.assert_consistent()


@phase3
async def test_s20_credential_never_reaches_model_or_logs():
    """Credential item → modal → stored in the credential store; value absent from model context,
    logs and messages; PM resumes with 'provided'."""


@phase6
async def test_s21_weekly_evolution_queues_once():
    """Weekly evolution tick while the project is busy → evolution task queued; a second tick in the
    same week creates nothing."""


@phase2
async def test_s22_config_change_between_turns():
    """Published agent package changes between turns → next turn loads the new config; nothing is
    invalidated or rejected."""


# ------------------------------------------------------------------ extra rules

async def test_invalid_supervisor_move_is_retried_not_applied(kernel, sim):
    task_id = await start_task(kernel, sim)
    sim.script("supervisor:evaluate", next_step("build"), next_step("requirements"))  # skipping stages is refused
    await sim.drain()
    assert (await sim.item("p1", task_id))["content"]["target_stage"] == "requirements"


async def test_relay_that_drops_a_pm_question_falls_back_to_raw(kernel, sim):
    task_id = await start_task(kernel, sim)
    await execute_next(kernel, sim, task_id, "requirements")
    sim.script("pm_turn", pm("needs_input", questions=[{"id": "q1", "text": "A?"}, {"id": "q2", "text": "B?"}]))
    sim.script("supervisor:evaluate", relay("q1"))
    await sim.drain()
    item = await sim.item("p1", task_id)
    assert item["origin"] == "kernel" and [q["id"] for q in item["content"]["questions"]] == ["q1", "q2"]
