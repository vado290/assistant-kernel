"""Acceptance scenarios S1–S22 from docs/REBUILD_PLAN.md section 9.

Phase 1 drives the state machine with injected run results (tests/core/sim.py).
Every Sim.step() asserts the three invariants: one active task per project,
one cursor per active task, one open card per project.
"""
from datetime import datetime, timedelta
from copy import deepcopy
from zoneinfo import ZoneInfo

import pytest

from assistant_kernel.core import rules
from assistant_kernel.core.cards import StaleItem
from assistant_kernel.core.db import all_rows

from .sim import Fail, next_step, pm, relay, report


def reviewed_task():
    task = {"kind": "delivery", "track": "full", "stage": "review", "documents": {
        "task_contract": report("task_contract", "r1"), "solution_design": report("solution_design", "d1"),
        "implementation_report": report("implementation_report", "i1", bundle_sha="b1"),
        "validation": {"passed": True, "bundle_sha": "b1", "report": "checks passed"}},
        "approvals": {"requirements": {"doc_sha": "r1"},
                      "design": {"doc_sha": "d1", "based_on_requirements_sha": "r1"}}}
    task["documents"]["review_report"] = report("review_report", "rv1", verdict="pass", bundle_sha="b1",
                                                review_baseline=rules.review_baseline(task))
    return task


@pytest.mark.parametrize("changed", ["task_contract", "solution_design", "implementation_report", "validation", "bundle"])
def test_review_cannot_cover_changed_inputs_even_with_renewed_approvals(changed):
    task = reviewed_task()
    assert rules.review_passed(task)
    if changed in ("task_contract", "solution_design", "implementation_report"):
        task["documents"][changed]["sha256"] += "-new"
    elif changed == "validation":
        task["documents"]["validation"]["report"] = "different evidence"
    else:
        for name in ("implementation_report", "validation"):
            task["documents"][name]["bundle_sha"] = "b2"
    task["approvals"] = {"requirements": {"doc_sha": rules.doc_sha(task, "task_contract")},
                         "design": {"doc_sha": rules.doc_sha(task, "solution_design"),
                                    "based_on_requirements_sha": rules.doc_sha(task, "task_contract")}}
    assert not rules.review_passed(task)
    for stage in ("review", "release"):
        task["stage"] = stage
        assert rules.move_error(task, "release")   # retries cannot bypass the same gate


def test_review_gate_does_not_redefine_historical_release_approval_or_quick():
    task = reviewed_task()
    task["approvals"]["release"] = {"bundle_sha": "b1", "review_sha": "rv1"}
    task["documents"]["review_report"].pop("review_baseline")
    before = deepcopy(task)
    assert not rules.review_passed(task)
    assert rules.approval_valid(task, "release")   # historical completion/reconcile semantics stay intact
    assert task == before                        # never backfill or mutate old reports
    task.update(track="quick", stage="build")
    task["documents"].pop("solution_design")
    assert rules.move_error(task, "release") is None
    task.update(track="full", stage="release")
    assert rules.stage_moves(task) == {"release"}
    assert rules.move_error(task, "review") is not None


@pytest.mark.parametrize("approval", ["requirements", "design"])
def test_review_needs_current_approvals(approval):
    task = reviewed_task()
    task["approvals"].pop(approval)
    assert not rules.review_passed(task)

deferred = pytest.mark.skip(reason="post-MVP: deferred by the MVP-first rule (see plan section 10)")


async def review_ready(kernel, sim):
    task_id = await start_task(kernel, sim)
    await to_build(kernel, sim, task_id)
    sim.script("pm_turn", pm("ready_for_validation", report=report("implementation_report", "i1", bundle_sha="b1")))
    sim.script("validation", {"passed": True, "report": "checks passed"})
    await execute_next(kernel, sim, task_id, "review")
    return task_id


def reviewed_result(task, sha="rv1"):
    return pm("completed", report=report("review_report", sha, verdict="pass",
                                         bundle_sha=rules.validated_bundle(task), review_baseline=rules.review_baseline(task)))


@pytest.mark.parametrize("change", ["legacy", "requirements", "implementation", "card_revision"])
async def test_expired_release_card_can_reject_rereview_and_publish(kernel, sim, monkeypatch, change):
    from psycopg.types.json import Jsonb

    task_id = await review_ready(kernel, sim)
    result = reviewed_result(await sim.task(task_id))
    if change == "legacy":
        result["report"].pop("review_baseline")
    sim.script("pm_turn", result)
    sim.script("supervisor:evaluate", next_step("release"))
    # A pre-upgrade card was created under the old eligibility rule. No automatic metadata upgrade.
    with monkeypatch.context() as prior:
        if change == "legacy":
            prior.setattr(rules, "review_error", lambda task: None)
        await sim.drain()
    item = await sim.item("p1", task_id)
    task = await sim.task(task_id)
    if change != "legacy":
        docs, approvals = task["documents"], task["approvals"]
        if change == "requirements":
            docs["task_contract"]["sha256"] = "r2"
            approvals["requirements"]["doc_sha"] = "r2"
            approvals["design"]["based_on_requirements_sha"] = "r2"
        elif change == "implementation":
            docs["implementation_report"]["sha256"] = "i2"
        else:
            docs["review_report"]["sha256"] = "rv2"
        # Fault injection into a temporary DB represents a card left over from an older baseline.
        async with kernel.db.tx() as c:
            await c.execute("UPDATE core.tasks SET documents=%s, approvals=%s WHERE task_id=%s",
                            (Jsonb(docs), Jsonb(approvals), task_id))
    with pytest.raises(ValueError, match="Not approved this time.*Decline"):
        await kernel.act("p1", item["item_id"], "approve")
    from assistant_kernel.core.gateway.config import GatewayConfig, ProjectChannels
    from assistant_kernel.core.gateway.service import GatewayService
    from .test_gateway import FakeTransport, PM_CH, SUP_CH, USER
    gateway = GatewayService(kernel, GatewayConfig(guild_id="g", allowed_user_ids=frozenset({USER}),
                             projects={"p1": ProjectChannels("p1", PM_CH, SUP_CH)}), FakeTransport())
    reply = await gateway._act("p1", item["item_id"], "approve", {}, USER, "expired-attempt")
    assert "Not approved this time" in reply.text and "Decline" in reply.text and "card was updated" not in reply.text
    assert (await sim.item("p1", task_id))["item_id"] == item["item_id"]
    assert "release" not in (await sim.task(task_id))["approvals"]
    await sim.assert_consistent()
    await kernel.act("p1", item["item_id"], "reject")
    await execute_next(kernel, sim, task_id, "review", "按当前版本复审")
    with pytest.raises(StaleItem):
        await kernel.act("p1", item["item_id"], "approve")
    if change == "legacy":
        sim.script("pm_turn", pm("completed"))  # no report must not silently stamp the old report
        await execute_next(kernel, sim, task_id, "review", "补齐有效审查报告")
        assert not rules.review_passed(await sim.task(task_id))
    sim.script("pm_turn", reviewed_result(await sim.task(task_id), "rv3"))
    sim.script("supervisor:evaluate", next_step("release"))
    await sim.drain()
    await kernel.act("p1", (await sim.item("p1", task_id))["item_id"], "approve")
    sim.script("publish", {"outcome": "published"})
    await sim.drain()
    assert (await sim.task(task_id))["close_reason"] == "published"


async def confirm_simulated_publisher_stopped(kernel, task_id):
    """The simulator has no worker thread; provide the same positive stop evidence as the real executor."""
    import json
    async with kernel.db.tx() as c:
        run = await (await c.execute("SELECT * FROM core.runs WHERE task_id=%s AND run_type='publish' ORDER BY created_at DESC LIMIT 1", (task_id,))).fetchone()
    path = kernel.settings.data_root / "vaults" / run["project_id"] / "runs" / str(run["run_id"]) / "publication/writer-stopped.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"project_id": run["project_id"], "publish_run_id": str(run["run_id"]),
                                "bundle_sha": run["input"]["bundle_sha"]}), "utf-8")


async def test_publish_failure_can_reapprove_same_review_and_complete(kernel, sim):
    task_id = await review_ready(kernel, sim)
    sim.script("pm_turn", reviewed_result(await sim.task(task_id)))
    sim.script("supervisor:evaluate", next_step("release"))
    await sim.drain()
    first = await sim.item("p1", task_id)
    await kernel.act("p1", first["item_id"], "approve")
    sim.script("publish", {"outcome": "failed", "error": "temporary publish obstacle"})
    sim.script("supervisor:evaluate", next_step("release", "障碍已排除，申请重试同一交付"))
    await sim.drain()
    assert (await sim.task(task_id))["stage"] == "release"
    retry = await sim.item("p1", task_id)
    assert retry["item_id"] != first["item_id"]
    await confirm_simulated_publisher_stopped(kernel, task_id)
    with pytest.raises(StaleItem):
        await kernel.act("p1", first["item_id"], "approve")
    await kernel.act("p1", retry["item_id"], "approve")
    sim.script("publish", {"outcome": "published"})
    await sim.drain()
    task = await sim.task(task_id)
    assert task["state"] == "completed" and rules.approval_valid(task, "release")


async def test_legacy_release_stage_can_stop_after_preserving_work_and_start_followup(kernel, sim, monkeypatch):
    from psycopg.types.json import Jsonb
    task_id = await review_ready(kernel, sim)
    sim.script("pm_turn", reviewed_result(await sim.task(task_id)))
    sim.script("supervisor:evaluate", next_step("release"))
    await sim.drain()
    await kernel.act("p1", (await sim.item("p1", task_id))["item_id"], "approve")
    task = await sim.task(task_id)
    task["documents"]["review_report"].pop("review_baseline")
    async with kernel.db.tx() as c:
        await c.execute("UPDATE core.tasks SET documents=%s WHERE task_id=%s", (Jsonb(task["documents"]), task_id))
    sim.script("publish", {"outcome": "failed", "error": "requires rework"})
    sim.script("supervisor:evaluate", next_step("release"))
    with monkeypatch.context() as prior:
        prior.setattr(rules, "review_error", lambda task: None)
        await sim.drain()  # an old retry card, left in release by the previous version
    retry = await sim.item("p1", task_id)
    with pytest.raises(ValueError, match="not confirmed stopped"):
        await kernel.cancel_task(task_id)
    await confirm_simulated_publisher_stopped(kernel, task_id)  # maintenance confirmed the legacy worker exited
    with pytest.raises(ValueError, match="the release stage cannot go straight back to review"):
        await kernel.act("p1", retry["item_id"], "approve")
    await kernel.act("p1", retry["item_id"], "reject")
    sim.script("supervisor:evaluate", {"decision": "propose_cancel", "message": "已保留工作，请确认停止后另建任务"})
    await sim.drain()
    failed = await sim.task(task_id)
    assert failed["stage"] == "release" and rules.move_error(failed, "review") and rules.move_error(failed, "release")
    await kernel.act("p1", (await sim.item("p1", task_id))["item_id"], "approve")
    old = await sim.task(task_id)
    assert old["state"] == "cancelled" and old["documents"] == failed["documents"]
    followup = await kernel.create_task("p1", goal="承接已保存的返工材料", origin="user", parent_task_id=task_id)
    assert followup["stage"] == "requirements" and followup["parent_task_id"] == old["task_id"]
    await sim.assert_consistent()
phase2 = pytest.mark.skip(reason="rebuild phase 2: executors + dispatcher")
phase3 = pytest.mark.skip(reason="rebuild phase 3: ingress + Discord cards")


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
    sim.script("pm_turn", pm("completed", report=report("review_report", "rv1", verdict="pass", bundle_sha="b1",
                                                       review_baseline=rules.review_baseline(await sim.task(task_id)))))
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
    notices = [m["body"]["text"] for m in await sim.outbound("p1")]
    assert any(n.startswith("PM (Requirements): ") for n in notices)       # every PM turn reports to the PM channel
    assert any(n.startswith("✅ Task completed") for n in notices)
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


async def test_s03b_relay_label_on_a_supervisor_card_is_accepted(kernel, sim):
    """Real-run finding: the model labelled an upgraded Supervisor question card relay_pm_questions."""
    task_id = await start_task(kernel, sim)
    sim.script("supervisor:evaluate", {"decision": "ask_user", "message": "缺内容",
                                       "questions": [{"id": "c1", "text": "三件待办是什么？"}]})
    await sim.drain()
    await kernel.receive("p1", role="pm", text="改成写 done.md")
    sim.script("supervisor:review_input", {"decision": "upgrade", "item": {
        "decision": "relay_pm_questions", "message": "已改为 done.md",
        "questions": [{"id": "c1", "text": "今天完成的三件事是什么？"}]}})
    await sim.drain()
    item = await sim.item("p1", task_id)
    assert item["content"]["questions"][0]["text"] == "今天完成的三件事是什么？" and item["deliver_to"] == "supervisor"


async def test_s03c_message_during_review_is_reviewed_again(kernel, sim):
    task_id = await start_task(kernel, sim)
    sim.script("supervisor:evaluate", next_step("requirements", "日报"))
    await sim.drain()
    await kernel.receive("p1", role="pm", text="补充：字体大一点")
    review = await kernel.claim("w1")                       # first review is running ...
    await kernel.receive("p1", role="pm", text="不对，改成周报")   # ... when the user writes again
    await kernel.complete(review, {"decision": "keep"})
    sim.script("supervisor:review_input", {"decision": "upgrade", "item": next_step("requirements", "周报")})
    await sim.drain()
    assert (await sim.item("p1", task_id))["content"]["instruction"] == "周报"


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


async def test_s14_quick_track_skips_design_and_review(kernel, sim):
    """R2.3: the Supervisor proposes the quick track; requirements approval leads straight to build, and a
    validated bundle may go to release without a review. Design is not reachable on the quick track."""
    task_id = await start_task(kernel, sim, text="把日报标题改成周报")
    sim.script("supervisor:evaluate", {**next_step("requirements", "写需求和简要方案"), "track": "quick"})
    await sim.drain()
    item = await sim.item("p1", task_id)
    assert item["content"]["track"] == "quick" and item["content"]["track_changed"]
    await kernel.act("p1", item["item_id"], "execute")
    assert (await sim.task(task_id))["track"] == "quick"

    sim.script("pm_turn", pm("completed", report=report("task_contract", "r1")))
    sim.script("supervisor:evaluate", next_step("design"), next_step("design"), next_step("design"))
    await sim.drain()
    assert (await sim.item("p1", task_id))["content"]["decision"] == "run_failed"   # no design on the quick track
    await kernel.act("p1", (await sim.item("p1", task_id))["item_id"], "retry")
    sim.script("supervisor:evaluate", next_step("build", "按简要方案实施"))
    await sim.drain()
    build = await sim.item("p1", task_id)
    assert build["content"]["approve"] == ["requirements"]
    await kernel.act("p1", build["item_id"], "execute")
    assert (await sim.task(task_id))["stage"] == "build"

    sim.script("pm_turn", pm("ready_for_validation", report=report("implementation_report", "i1", bundle_sha="b1")))
    sim.script("validation", {"passed": True})
    sim.script("supervisor:evaluate", next_step("release"))
    await sim.drain()
    release = await sim.item("p1", task_id)
    assert release["content"]["decision"] == "release" and release["content"]["data"]["review_sha"] is None
    await kernel.act("p1", release["item_id"], "approve")
    sim.script("publish", {"outcome": "published"})
    await sim.drain()
    task = await sim.task(task_id)
    assert (task["state"], task["close_reason"]) == ("completed", "published")


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
    sim.script("pm_turn", pm("ready_for_validation", report=report("implementation_report", "i1", bundle_sha="no-files:i1")))
    sim.script("validation", {"passed": True})
    await execute_next(kernel, sim, task_id, "review")
    sim.script("pm_turn", pm("completed", report=report("review_report", "rv1", verdict="pass", bundle_sha="no-files:i1",
                                                       review_baseline=rules.review_baseline(await sim.task(task_id)))))
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


async def test_s21_weekly_evolution_queues_once(kernel, sim):
    """Weekly evolution tick while the project is busy → evolution task queued; a second tick in the
    same week creates nothing; idle projects and opted-out projects are skipped."""
    await start_task(kernel, sim)
    await start_task(kernel, sim, project="p2")
    async with kernel.db.tx() as c:
        await c.execute("""UPDATE core.projects SET settings='{"weekly_evolution": false}' WHERE project_id='p2'""")
    sunday = datetime(2026, 10, 18, 13, 5, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert await kernel.schedule_evolution(sunday - timedelta(minutes=10)) == []
    assert await kernel.schedule_evolution(sunday) == ["p1"]
    assert await kernel.schedule_evolution(sunday + timedelta(hours=5)) == []
    async with kernel.db.tx() as c:
        rows = await all_rows(c, "SELECT state, origin FROM core.tasks WHERE kind='evolution'")
    assert rows == [{"state": "queued", "origin": "schedule"}]


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
