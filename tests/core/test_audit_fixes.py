"""Fixes from docs/AUDIT_2026-10-17.md: each test names the finding it covers."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from psycopg.types.json import Jsonb

from assistant_kernel.core import grants, jobs, machine, runtime
from assistant_kernel.core.db import one
from assistant_kernel.core.dispatch import Dispatcher
from assistant_kernel.core.executors.delivery import check_repo_manifest

from .sim import next_step, pm, report
from .test_dispatch import Scripts
from .test_gateway import PM_CH, USER, gateway  # noqa: F401  (a fixture)
from .test_git_delivery import git, repo  # noqa: F401  (a fixture)
from .test_r2 import tasks, triaging  # noqa: F401  (a fixture)
from .test_runtime import MANIFEST, control, release, vault  # noqa: F401  (fixtures)
from .test_scenarios import execute_next, start_task, to_build


async def test_h1_failing_maintenance_does_not_stop_claiming(kernel, sim, monkeypatch):
    async def broken():
        raise RuntimeError("supervisorctl timed out")
    monkeypatch.setattr(kernel, "recover_expired", broken)
    scripts = Scripts()
    scripts.add("supervisor:evaluate", next_step("requirements"))
    d = Dispatcher(kernel, scripts.executors(), poll_seconds=0.02, recover_every_seconds=0)
    await kernel.receive("p1", role="pm", text="x")
    stop = asyncio.Event()
    serving = asyncio.create_task(d.serve(stop))
    for _ in range(100):
        if await sim.item("p1"):
            break
        await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(serving, timeout=10)
    assert await sim.item("p1") is not None


async def test_m1_a_kernel_error_fails_the_run_at_once(kernel, sim, monkeypatch):
    async def bug(c, task, run, result):
        raise KeyError("missing field")
    monkeypatch.setattr(machine, "_supervisor_result", bug)
    task_id = await start_task(kernel, sim)
    sim.script("supervisor:evaluate", next_step("requirements"))
    await sim.drain()
    async with kernel.db.tx() as c:
        run = await one(c, "SELECT * FROM core.runs WHERE task_id=%s AND run_type='supervisor'", (task_id,))
    assert (run["status"], run["attempt"], run["error"]["type"]) == ("failed", 1, "kernel_error")   # not re-run
    assert (await sim.item("p1", task_id))["content"]["decision"] == "run_failed"


async def test_m2_undeliverable_messages_stop_blocking(gateway, kernel):  # noqa: F811
    await kernel.ensure_project("gone")                                      # a project whose routing was removed
    async with kernel.db.tx() as c:
        for age in (timedelta(hours=1), timedelta(seconds=1)):
            await c.execute("""INSERT INTO core.messages(project_id, channel, direction, role, body, created_at, delivery_status)
                               VALUES ('gone', 'kernel', 'outbound', 'pm', %s, %s, 'pending')""",
                            (Jsonb({"text": "x"}), datetime.now(timezone.utc) - age))
    await gateway.deliver_outbox()
    async with kernel.db.tx() as c:
        states = [r["delivery_status"] for r in await (await c.execute(
            "SELECT delivery_status FROM core.messages WHERE project_id='gone' ORDER BY created_at")).fetchall()]
    assert states == ["failed", "pending"]          # the fresh one may still get its routing


async def test_m3_grant_keys_never_collide(kernel, tmp_path, monkeypatch, kernel_settings):
    monkeypatch.setenv("CORE_GRANT_ROOT", str(tmp_path))
    for path in ("project", "a/crypto", "b/crypto"):
        (tmp_path / path).mkdir(parents=True)
    check = lambda p: grants.check_directory({"path": str(tmp_path / p), "purpose": "x"}, "p1",  # noqa: E731
                                             kernel_settings.data_root)
    assert check("project")["key"] == "dir-project"
    async with kernel.db.tx() as c:
        for path in ("a/crypto", "b/crypto", "a/crypto"):
            await machine.grant_directory(c, "p1", check(path), reason="r")
    keys = {g["resource_key"]: g["policy_json"]["path"] for g in await kernel.grants("p1")}
    assert len(keys) == 2 and keys["crypto"].endswith("a/crypto")      # a second "crypto" does not replace the first


async def test_m4_and_l2_input_after_the_last_turn_is_reported_and_records_survive_reconcile(kernel, sim):
    task_id = await start_task(kernel, sim)
    await to_build(kernel, sim, task_id)
    record = {"title": "日报工具", "reason": "用户需要", "changes": [], "significance": "major",
              "verification": "", "expected_effect": "", "measure": "", "followups": ""}
    sim.script("pm_turn", pm("ready_for_validation",
                             report=report("implementation_report", "i1", bundle_sha="b1", iteration=record)))
    sim.script("validation", {"passed": True})
    await execute_next(kernel, sim, task_id, "review")
    sim.script("pm_turn", pm("completed", report=report("review_report", "rv1", verdict="pass", bundle_sha="b1")))
    sim.script("supervisor:evaluate", next_step("release"))
    await sim.drain()
    await kernel.act("p1", (await sim.item("p1", task_id))["item_id"], "approve")
    await kernel.receive("p1", role="pm", text="顺便把标题改成蓝色")          # arrives while publishing
    run = await kernel.claim("w1")
    async with kernel.db.tx() as c:
        await c.execute("UPDATE core.runs SET lease_until=now() - interval '1 second' WHERE run_id=%s", (run["run_id"],))
    await kernel.recover_expired()
    await kernel.act("p1", (await sim.item("p1", task_id))["item_id"], "published")
    assert any("顺便把标题改成蓝色" in m["body"]["text"] and "还没有处理" in m["body"]["text"]
               for m in await sim.outbound("p1"))
    [row] = await kernel.iterations("p1")
    assert (row["title"], row["complete"]) == ("日报工具", True)       # the PM's record, not the fallback


async def test_l1_a_message_triaged_while_a_task_started_becomes_its_supplement(triaging):  # noqa: F811
    kernel, sim = triaging
    await kernel.receive("p1", role="pm", text="做日报", external_event_id="m1")
    await kernel.receive("p1", role="pm", text="日报要发邮件", external_event_id="m2")
    sim.script("triage", {"decision": "start_task", "goal": "做日报"}, {"decision": "start_task", "goal": "发邮件"})
    sim.script("supervisor:evaluate", next_step("requirements"))
    await sim.drain()
    assert [t["goal"] for t in await tasks(kernel)] == ["做日报"]
    async with kernel.db.tx() as c:
        attached = await one(c, "SELECT task_id FROM core.messages WHERE body->>'text' = '日报要发邮件'")
    assert attached["task_id"] == (await tasks(kernel))[0]["task_id"]


async def test_l3_no_cancel_while_publishing(gateway, kernel, sim):  # noqa: F811
    task_id = await start_task(kernel, sim)
    async with kernel.db.tx() as c:
        await c.execute("UPDATE core.tasks SET state='publishing' WHERE task_id=%s", (task_id,))
    assert "无法中途取消" in await gateway.cancel_active(PM_CH, USER)
    assert (await sim.task(task_id))["state"] == "publishing"


async def test_m5_only_a_released_manifest_is_registered(kernel, sim, kernel_settings, vault, control):  # noqa: F811
    (vault / runtime.MANIFEST).write_text(MANIFEST, "utf-8")                 # written straight from a shell
    assert await jobs.sync_runtime(kernel, kernel_settings) == 0
    assert await jobs.sync_runtime(kernel, kernel_settings) == 0
    assert await jobs.list_jobs(kernel, "p1") == [] and control.applied == []
    assert len([m for m in await sim.outbound("p1") if "被直接修改过" in m["body"]["text"]]) == 1   # said once
    await release(kernel, vault, MANIFEST)
    assert await jobs.sync_runtime(kernel, kernel_settings) == 1
    assert {r["job_id"] for r in await jobs.list_jobs(kernel, "p1")} == {"daily-report", "listener", "listener-restart"}


async def test_m5_code_delivery_registers_jobs_yaml_from_main(kernel, kernel_settings, repo, control):  # noqa: F811
    (repo / "scripts").mkdir()
    (repo / "scripts" / "report.py").write_text("print(1)\n", "utf-8")
    (repo / runtime.MANIFEST).write_text(MANIFEST.split("services:")[0], "utf-8")
    git(repo, "checkout", "-qb", "work")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=u", "-c", "user.email=u@x", "commit", "-qm", "jobs")
    await kernel.ensure_project("p1")
    assert await jobs.sync_runtime(kernel, kernel_settings) == 0             # not on main yet
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--ff-only", "work")
    assert await jobs.sync_runtime(kernel, kernel_settings) == 1
    assert {r["job_id"] for r in await jobs.list_jobs(kernel, "p1")} == {"daily-report"}


def test_m5_code_delivery_validation_checks_the_manifest(kernel_settings, repo, tmp_path):  # noqa: F811
    from assistant_kernel.core import gitdelivery
    code = gitdelivery.config(kernel_settings, "p1")
    worktree = gitdelivery.ensure_worktree(code, "t1")
    assert check_repo_manifest(kernel_settings, "p1", code, "t1") is None    # no jobs.yaml: nothing to check
    (worktree / runtime.MANIFEST).write_text(MANIFEST.split("services:")[0], "utf-8")
    assert "scripts/report.py" in check_repo_manifest(kernel_settings, "p1", code, "t1")


async def test_l7_a_failed_service_registration_is_retried(kernel, kernel_settings, vault, control):  # noqa: F811
    calls = []

    def flaky(project_id, vault, services, removed, home=None):
        calls.append([s["id"] for s in services])
        if len(calls) == 1:
            raise RuntimeError("supervisorctl unavailable")
    control.apply = flaky
    await release(kernel, vault, MANIFEST)
    with pytest.raises(RuntimeError):
        await jobs._sync_project(kernel, kernel_settings, "p1")
    assert await jobs.sync_runtime(kernel, kernel_settings) == 1
    assert calls == [["listener"], ["listener"]]
    assert await jobs.sync_runtime(kernel, kernel_settings) == 0


# ------------------------------------------------------------------ second round (0.43.1)

async def test_h2_a_deleted_card_message_does_not_stop_the_cards(gateway, kernel, sim):  # noqa: F811
    first = await start_task(kernel, sim)
    sim.script("supervisor:evaluate", next_step("requirements"))
    await sim.drain()
    await gateway.sync_cards()

    async def deleted(*args, **kwargs):
        raise RuntimeError("404 Unknown Message")
    gateway.transport.edit = deleted                                   # the user deleted the card message
    await kernel.cancel_task(first)
    await gateway.sync_cards()                                         # retiring it fails quietly
    assert await kernel.displayed_cards("p1") == []
    await start_task(kernel, sim, text="做周报")
    sim.script("supervisor:evaluate", next_step("requirements"))
    await sim.drain()
    sent = len(gateway.transport.order)
    await gateway.sync_cards()
    assert len(gateway.transport.order) > sent                         # the next card still shows


async def test_m7_input_of_a_failed_pm_turn_reaches_the_retry(kernel, sim):
    from .sim import Fail
    task_id = await start_task(kernel, sim)
    await execute_next(kernel, sim, task_id, "requirements")
    await kernel.receive("p1", role="pm", text="预算控制在 500 以内")
    sim.script("pm_turn", Fail(), Fail(), Fail())
    await sim.drain()
    await kernel.act("p1", (await sim.item("p1", task_id))["item_id"], "retry")
    run = await kernel.claim("w1")
    assert "预算控制在 500 以内" in [m["body"].get("text") for m in await kernel.turn_inputs(run)]


async def test_m6_a_login_refreshed_mid_call_is_shared_before_the_call_ends(kernel_settings, monkeypatch):
    from types import SimpleNamespace

    from assistant_kernel.core.executors import common
    from assistant_kernel.core.executors.auth_sync import master_path
    from assistant_kernel.storage import ProjectRuntimeManager
    monkeypatch.setattr(common, "AUTH_SYNC_SECONDS", 0.05)
    master = master_path(kernel_settings)
    master.parent.mkdir(parents=True, exist_ok=True)
    master.write_text('{"t": "old"}')
    local = ProjectRuntimeManager(kernel_settings.data_root).ensure("p1").codex_home / "auth.json"
    seen = {}

    class Harness:
        async def run_turn(self, request):
            await asyncio.sleep(0.05)
            local.write_text('{"t": "refreshed"}')                    # Codex refreshes at the start of the turn
            await asyncio.sleep(0.3)
            seen["master"] = master.read_text()                        # ... while the turn is still running
            return SimpleNamespace(final_text="{}")

    config = SimpleNamespace(allowed_tools=(), model=None, reasoning_effort=None, provider=None, context_policy=None,
                             model_context_window_tokens=None, model_auto_compact_token_limit=None, timeout_seconds=60)
    ctx = SimpleNamespace(services={"settings": kernel_settings, "harness": Harness()}, cancel=asyncio.Event())
    run = {"project_id": "p1", "run_id": "r1", "attempt": 1}
    await common.call_model(ctx, run, config, "x", task_id=None, stage=None, session_id=None, tools=frozenset(),
                            sandbox="read-only")
    assert seen["master"] == '{"t": "refreshed"}'


async def test_l8_only_input_after_the_last_decision_is_reported(kernel, sim):
    task_id = await start_task(kernel, sim)
    await execute_next(kernel, sim, task_id, "requirements")
    async with kernel.db.tx() as c:   # said before the user's decision: Supervisor and the user already saw it
        await c.execute("""INSERT INTO core.messages(project_id, task_id, channel, direction, role, body, created_at)
                           VALUES ('p1', %s, 'api', 'inbound', 'pm', %s, now() - interval '1 hour')""",
                        (task_id, Jsonb({"text": "看起来不错"})))
        task = await machine.load_task(c, task_id)
        await machine.close(c, task, "completed", "published")
    assert not any("还没有处理" in m["body"]["text"] for m in await sim.outbound("p1"))


async def test_l9_one_stuck_worktree_does_not_block_the_others(kernel, kernel_settings, repo, monkeypatch):  # noqa: F811
    from assistant_kernel.core import gitdelivery, vault_sync
    ids = []
    async with kernel.db.tx() as c:
        for goal in ("a", "b"):
            task = await machine.create_task(c, "p1", goal=goal, origin="user")
            await c.execute("UPDATE core.tasks SET closed_at=now() WHERE task_id=%s", (task["task_id"],))
            ids.append(str(task["task_id"]))
    removed = []

    def remove(code, task_id):
        if task_id == ids[0]:
            raise gitdelivery.GitError("worktree is locked")
        removed.append(task_id)
        return True
    monkeypatch.setattr(gitdelivery, "remove_worktree", remove)
    await vault_sync._remove_worktrees(kernel, kernel_settings)
    await vault_sync._remove_worktrees(kernel, kernel_settings)        # nothing is tried twice
    assert removed == [ids[1]]
