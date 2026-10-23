"""Phase 2d: validation and publish run against real candidate files in a temporary vault."""
from __future__ import annotations

import pytest

from assistant_kernel.core import rules
from assistant_kernel.core.dispatch import Dispatcher
from assistant_kernel.core.executors.delivery import execute_publish, execute_validation
from assistant_kernel.services.candidate_bundle import CandidateBundleService
from assistant_kernel.services.workspace_scope import WorkspaceScopeService

from .sim import next_step, pm, report
from .test_dispatch import Scripts, settle
from .test_scenarios import to_build


@pytest.fixture
def dispatcher(kernel, kernel_settings, project_files):
    scripts = Scripts()
    executors = {**scripts.executors(), "validation": execute_validation, "publish": execute_publish}
    d = Dispatcher(kernel, executors, services={"settings": kernel_settings})
    d.scripts = scripts
    return d


class ScriptsAsSim:
    """Lets the phase-1 helpers (to_build) drive a dispatcher instead of the simulator."""

    def __init__(self, dispatcher, sim):
        self.d, self.sim = dispatcher, sim

    def script(self, key, *results):
        self.d.scripts.add(key, *results)

    async def drain(self):
        await settle(self.d)

    async def item(self, project, task_id=None):
        return await self.sim.item(project, task_id)

    history = []


def write_candidate(settings, task_id, content="# Hello\n", files=None):
    """What a build turn does: pin the build scope, then write candidate files through it."""
    import hashlib
    from assistant_kernel.core.executors.common import workspace_snapshot
    workspace_snapshot(settings, "p1", task_id, "test-build", candidates=True)
    scopes = WorkspaceScopeService(settings)
    roots = scopes.resolve("p1", task_id, None, candidate_enabled=True, shared_agreement=True)
    vault = settings.data_root / "vaults" / "p1"
    for relative, text in (files or {"docs/hello.md": content}).items():
        current = vault / relative
        sha = hashlib.sha256(current.read_bytes()).hexdigest() if current.is_file() else None
        scopes.write_candidate(roots, f"workspace://project/{relative}", text, sha, None if sha else "absent")
    return CandidateBundleService(settings).seal("p1", task_id)["bundle_sha256"]


async def test_build_validate_review_publish_writes_the_real_file(kernel, sim, dispatcher, kernel_settings, project_files):
    await _publish(kernel, sim, dispatcher, kernel_settings, None)
    assert (project_files / "docs" / "hello.md").read_text("utf-8") == "# Hello\n"


@pytest.mark.parametrize("delete_manifest,lease_loss", [(False, False), (True, False), (False, True)])
async def test_reconcile_restores_runtime_evidence_without_republishing(
        kernel, sim, dispatcher, kernel_settings, project_files, delete_manifest, lease_loss, monkeypatch):
    import hashlib
    from assistant_kernel.core import jobs, runtime
    from assistant_kernel.core.db import event
    manifest = "schema_version: runtime-manifest.v1\njobs:\n  - id: daily\n    type: script\n    entry: scripts/report.py\n    schedule: {kind: cron, expression: '0 9 * * *'}\n"
    if delete_manifest:
        (project_files / "scripts").mkdir()
        (project_files / "scripts/report.py").write_text("print('ok')\n", "utf-8")
        (project_files / "jobs.yaml").write_text(manifest, "utf-8")
        async with kernel.db.tx() as c:
            await event(c, "p1", "runtime_manifest_published", manifest_sha=runtime.manifest_sha(manifest))
        await jobs.sync_runtime(kernel, kernel_settings)
    task_id = (await kernel.receive("p1", role="pm", text="runtime publication"))["task_id"]
    await to_build(kernel, ScriptsAsSim(dispatcher, sim), task_id)
    files = {"docs/result.md": "result"} if delete_manifest else {"jobs.yaml": manifest, "scripts/report.py": "print('ok')\n"}
    bundle = write_candidate(kernel_settings, task_id, files=files)
    if delete_manifest:
        scopes = WorkspaceScopeService(kernel_settings)
        scopes.delete_candidate(scopes.resolve("p1", task_id, None, candidate_enabled=True, shared_agreement=True),
                                "workspace://project/jobs.yaml", hashlib.sha256((project_files / "jobs.yaml").read_bytes()).hexdigest())
        bundle = CandidateBundleService(kernel_settings).seal("p1", task_id)["bundle_sha256"]
    dispatcher.scripts.add("pm_turn", pm("ready_for_validation", report=report("implementation_report", "i1", bundle_sha=bundle)))
    dispatcher.scripts.add("supervisor:evaluate", next_step("review"))
    await settle(dispatcher)
    await kernel.act("p1", (await sim.item("p1"))["item_id"], "execute")
    dispatcher.scripts.add("pm_turn", pm("completed", report=report("review_report", "rv1", verdict="pass", bundle_sha=bundle,
                                                                  review_baseline=rules.review_baseline(await sim.task(task_id)))))
    dispatcher.scripts.add("supervisor:evaluate", next_step("release"))
    await settle(dispatcher)
    await kernel.act("p1", (await sim.item("p1"))["item_id"], "approve")
    if lease_loss:
        import asyncio
        import json
        import threading
        from pathlib import Path
        started, resume = threading.Event(), threading.Event()
        original = Path.replace
        def replace(path, target):
            value = original(path, target)
            if Path(target).name == "journal.json" and len(json.loads(Path(target).read_text())["applied_target_uris"]) == 2:
                started.set()
                assert resume.wait(30)
                raise OSError("journal failure after all target writes")
            return value
        monkeypatch.setattr(Path, "replace", replace)
        try:
            await dispatcher.claim_and_start()
            for _ in range(1000):
                if started.is_set(): break
                await asyncio.sleep(.01)
            assert started.is_set()
            async with kernel.db.tx() as c:
                await c.execute("UPDATE core.runs SET lease_until=now()-interval '1 second' WHERE task_id=%s AND run_type='publish'", (task_id,))
            assert await kernel.recover_expired() == 1
            item = await sim.item("p1")
            assert item["content"]["decision"] == "reconcile"
            assert (project_files / "jobs.yaml").read_text() == manifest
            with pytest.raises(ValueError, match="not confirmed stopped"):
                await kernel.act("p1", item["item_id"], "published")
            with pytest.raises(ValueError, match="not confirmed stopped"):
                await kernel.act("p1", item["item_id"], "failed")
            with pytest.raises(ValueError, match="not confirmed stopped"):
                await kernel.cancel_task(task_id)
            assert (await sim.task(task_id))["state"] == "awaiting_user"
        finally:
            resume.set()
            await dispatcher.drain()
        assert not (project_files / "jobs.yaml").exists()
        assert not (project_files / "scripts/report.py").exists()
        assert (await sim.item("p1"))["item_id"] == item["item_id"]
        with pytest.raises(ValueError, match="final state is not confirmed"):
            await kernel.act("p1", item["item_id"], "published")
        await kernel.act("p1", item["item_id"], "failed")
        assert (await sim.task(task_id))["state"] == "supervising"
        return
    async def lost_result(run, ctx):
        await execute_publish(run, ctx)
        raise RuntimeError("worker lost result after file effects")
    dispatcher.executors["publish"] = lost_result
    await settle(dispatcher)
    item = await sim.item("p1")
    assert item["content"]["decision"] == "reconcile"
    if not delete_manifest:
        async with kernel.db.tx() as c:
            published_run = await (await c.execute("SELECT run_id FROM core.runs WHERE task_id=%s AND run_type='publish'", (task_id,))).fetchone()
        root = project_files / "runs" / str(published_run["run_id"]) / "publication"
        journal, stopped = root / "journal.json", root / "writer-stopped.json"
        journal_bytes, stopped_bytes = journal.read_bytes(), stopped.read_bytes()
        journal.unlink()
        stopped.unlink()  # legacy task with no positive stop evidence
        with pytest.raises(ValueError, match="not confirmed stopped"):
            await kernel.act("p1", item["item_id"], "failed")
        with pytest.raises(ValueError, match="not confirmed stopped"):
            await kernel.cancel_task(task_id)
        assert await kernel.expire_idle_tasks(days=0) == []
        stopped.write_bytes(stopped_bytes)  # maintenance confirms this already-returned test worker has stopped
        with pytest.raises(ValueError, match="is not verified"):
            await kernel.act("p1", item["item_id"], "published")
        journal.write_bytes(journal_bytes)
    target = project_files / ("docs/result.md" if delete_manifest else "scripts/report.py")
    expected = target.read_bytes()
    target.write_bytes(b"unverified external change")
    with pytest.raises(ValueError, match="is not verified"):
        await kernel.act("p1", item["item_id"], "published")
    assert (await sim.item("p1"))["item_id"] == item["item_id"]
    target.write_bytes(expected)
    # Changed mutable candidates must not be re-sealed or re-applied during reconciliation.
    (project_files / "runs" / task_id / "candidate/manifest.json").write_text("{}", "utf-8")
    await kernel.act("p1", item["item_id"], "published")
    assert (await sim.task(task_id))["close_reason"] == "published_after_reconcile"
    async with kernel.db.tx() as c:
        rows = list(await (await c.execute("SELECT payload->>'manifest_sha' AS sha FROM core.task_events WHERE task_id=%s AND kind='runtime_manifest_published'", (task_id,))).fetchall())
    assert rows == [{"sha": None if delete_manifest else runtime.manifest_sha(manifest)}]
    await jobs.sync_runtime(kernel, kernel_settings)
    assert bool(await jobs.list_jobs(kernel, "p1")) is not delete_manifest
    assert target.read_bytes() == expected


async def test_interrupted_file_publish_waits_for_rollback(kernel_settings, project_files, monkeypatch):
    import asyncio
    import threading
    import uuid
    from pathlib import Path
    from types import SimpleNamespace
    from assistant_kernel.core.executors.delivery import _publish
    (project_files / "docs").mkdir()
    for name in ("one.md", "two.md"):
        (project_files / "docs" / name).write_text("old", "utf-8")
    task_id = str(uuid.uuid4())
    bundle = write_candidate(kernel_settings, task_id, files={"docs/one.md": "new", "docs/two.md": "new"})
    started, resume = threading.Event(), threading.Event()
    original = Path.replace
    def replace(path, target):
        value = original(path, target)
        if Path(target) == project_files / "docs/one.md" and str(path).endswith(".tmp") and not started.is_set():
            started.set()
            assert resume.wait(10)
        return value
    monkeypatch.setattr(Path, "replace", replace)
    ctx = SimpleNamespace(services={"settings": kernel_settings}, cancel=threading.Event())
    work = asyncio.create_task(_publish({"project_id": "p1", "task_id": task_id, "run_id": "interrupted", "input": {"bundle_sha": bundle}}, ctx))
    for _ in range(1000):
        if started.is_set(): break
        await asyncio.sleep(.01)
    assert started.is_set()
    work.cancel()
    await asyncio.sleep(.01)
    assert ctx.cancel.is_set() and not work.done()
    work.cancel()   # repeated cancellation must not skip the join either
    await asyncio.sleep(.01)
    assert not work.done()
    resume.set()
    with pytest.raises(asyncio.CancelledError):
        await work
    assert all((project_files / "docs" / name).read_text() == "old" for name in ("one.md", "two.md"))
    import json
    journal = json.loads((project_files / "runs/interrupted/publication/journal.json").read_text())
    assert journal["status"] == "rolled_back"


@pytest.mark.parametrize("snapshot_failure", [False, True])
async def test_failure_before_journal_still_records_writer_stop(kernel_settings, project_files, monkeypatch, snapshot_failure):
    import json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from assistant_kernel.core import projectgit
    from assistant_kernel.core.executors import delivery
    async def fail(*args): raise ValueError("bad candidate before any target write")
    def snapshot(*args, **kwargs):
        if snapshot_failure: raise ValueError("bad candidate before any target write")
        return [], []
    monkeypatch.setattr(delivery, "_publish", fail)
    monkeypatch.setattr(projectgit, "commit_release", snapshot)
    ctx = SimpleNamespace(services={"settings": kernel_settings},
                          kernel=SimpleNamespace(get_task=AsyncMock(return_value={}), grants=AsyncMock(return_value=[])))
    run = {"project_id": "p1", "task_id": "t1", "run_id": "r1", "input": {"bundle_sha": "abc"}}
    with pytest.raises(ValueError, match="bad candidate"):
        await execute_publish(run, ctx)
    root = project_files / "runs/r1/publication"
    assert not (root / "journal.json").exists()
    assert json.loads((root / "writer-stopped.json").read_text()) == {
        "project_id": "p1", "publish_run_id": "r1", "bundle_sha": "abc"}


async def test_a_project_can_publish_its_charter_role_and_skills(kernel, sim, dispatcher, kernel_settings, project_files):
    """Round 2 / I6: improvements to a project's own charter, roles and skills go through the normal publish."""
    skill = (project_files / "agents/pm/.agents/skills/assistant-operation/SKILL.md").read_text("utf-8")
    files = {"knowledge/charter.md": "# 章程\n\n## 1 项目用途\n日报\n",
             "agents/pm/role.md": "# PM 角色\n\n涉及金额时二次确认。\n",
             "agents/pm/.agents/skills/assistant-operation/SKILL.md": skill + "\n本项目补充：每周一汇报。\n"}
    await _publish(kernel, sim, dispatcher, kernel_settings, files)
    for relative, text in files.items():
        assert (project_files / relative).read_text("utf-8") == text, relative


async def _publish(kernel, sim, dispatcher, kernel_settings, files):
    driver = ScriptsAsSim(dispatcher, sim)
    task_id = (await kernel.receive("p1", role="pm", text="写一份 hello 文档"))["task_id"]
    await to_build(kernel, driver, task_id)
    bundle = write_candidate(kernel_settings, task_id, files=files)
    dispatcher.scripts.add("pm_turn", pm("ready_for_validation",
                                         report=report("implementation_report", "i1", bundle_sha=bundle)))
    dispatcher.scripts.add("supervisor:evaluate", next_step("review", "独立 Review"))
    await settle(dispatcher)
    task = await sim.task(task_id)
    assert task["documents"]["validation"]["passed"], task["documents"]["validation"]
    await kernel.act("p1", (await sim.item("p1", task_id))["item_id"], "execute")

    dispatcher.scripts.add("pm_turn", pm("completed", report=report("review_report", "rv1", verdict="pass", bundle_sha=bundle,
                                                                  review_baseline=rules.review_baseline(task))))
    dispatcher.scripts.add("supervisor:evaluate", next_step("release"))
    await settle(dispatcher)
    await kernel.act("p1", (await sim.item("p1", task_id))["item_id"], "approve")
    await settle(dispatcher)

    async with kernel.db.tx() as c:
        publish = await (await c.execute("SELECT status, error FROM core.runs WHERE run_type='publish'")).fetchone()
    assert publish["status"] == "succeeded", publish["error"]
    assert (await sim.task(task_id))["state"] == "completed"


async def test_files_changed_after_the_report_fail_validation(kernel, sim, dispatcher, kernel_settings):
    driver = ScriptsAsSim(dispatcher, sim)
    task_id = (await kernel.receive("p1", role="pm", text="写文档"))["task_id"]
    await to_build(kernel, driver, task_id)
    bundle = write_candidate(kernel_settings, task_id)
    scopes = WorkspaceScopeService(kernel_settings)
    roots = scopes.resolve("p1", task_id, None, candidate_enabled=True, shared_agreement=True)
    scopes.write_candidate(roots, "workspace://project/docs/extra.md", "late change", None, "absent")
    dispatcher.scripts.add("pm_turn", pm("ready_for_validation",
                                         report=report("implementation_report", "i1", bundle_sha=bundle)))
    dispatcher.scripts.add("supervisor:evaluate", next_step("build", "重新提交实施报告"))
    await settle(dispatcher)
    validation = (await sim.task(task_id))["documents"]["validation"]
    assert not validation["passed"] and "changed after the implementation report" in validation["report"]


async def test_document_only_delivery_needs_no_files(kernel, sim, dispatcher):
    driver = ScriptsAsSim(dispatcher, sim)
    task_id = (await kernel.receive("p1", role="pm", text="只要一份分析"))["task_id"]
    await to_build(kernel, driver, task_id)
    dispatcher.scripts.add("pm_turn", pm("ready_for_validation",
                                         report=report("implementation_report", "i1", bundle_sha="no-files:i1")))
    dispatcher.scripts.add("supervisor:evaluate", next_step("review"))
    await settle(dispatcher)
    assert (await sim.task(task_id))["documents"]["validation"]["passed"]
