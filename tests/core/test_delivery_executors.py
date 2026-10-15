"""Phase 2d: validation and publish run against real candidate files in a temporary vault."""
from __future__ import annotations

import pytest

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


def write_candidate(settings, task_id, content="# Hello\n"):
    """What a build turn does: pin the build scope, then write a candidate file through it."""
    from assistant_kernel.core.executors.common import workspace_snapshot
    workspace_snapshot(settings, "p1", task_id, "test-build", candidates=True)
    scopes = WorkspaceScopeService(settings)
    roots = scopes.resolve("p1", task_id, None, candidate_enabled=True, shared_agreement=True)
    scopes.write_candidate(roots, "workspace://project/docs/hello.md", content, None, "absent")
    return CandidateBundleService(settings).seal("p1", task_id)["bundle_sha256"]


async def test_build_validate_review_publish_writes_the_real_file(kernel, sim, dispatcher, kernel_settings, project_files):
    driver = ScriptsAsSim(dispatcher, sim)
    task_id = (await kernel.receive("p1", role="pm", text="写一份 hello 文档"))["task_id"]
    await to_build(kernel, driver, task_id)
    bundle = write_candidate(kernel_settings, task_id)
    dispatcher.scripts.add("pm_turn", pm("ready_for_validation",
                                         report=report("implementation_report", "i1", bundle_sha=bundle)))
    dispatcher.scripts.add("supervisor:evaluate", next_step("review", "独立 Review"))
    await settle(dispatcher)
    task = await sim.task(task_id)
    assert task["documents"]["validation"]["passed"], task["documents"]["validation"]
    await kernel.act("p1", (await sim.item("p1", task_id))["item_id"], "execute")

    dispatcher.scripts.add("pm_turn", pm("completed", report=report("review_report", "rv1", verdict="pass", bundle_sha=bundle)))
    dispatcher.scripts.add("supervisor:evaluate", next_step("release"))
    await settle(dispatcher)
    await kernel.act("p1", (await sim.item("p1", task_id))["item_id"], "approve")
    await settle(dispatcher)

    async with kernel.db.tx() as c:
        publish = await (await c.execute("SELECT status, error FROM core.runs WHERE run_type='publish'")).fetchone()
    assert publish["status"] == "succeeded", publish["error"]
    assert (await sim.task(task_id))["state"] == "completed"
    assert (project_files / "docs" / "hello.md").read_text("utf-8") == "# Hello\n"


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
    assert not validation["passed"] and "又被修改" in validation["report"]


async def test_document_only_delivery_needs_no_files(kernel, sim, dispatcher):
    driver = ScriptsAsSim(dispatcher, sim)
    task_id = (await kernel.receive("p1", role="pm", text="只要一份分析"))["task_id"]
    await to_build(kernel, driver, task_id)
    dispatcher.scripts.add("pm_turn", pm("ready_for_validation",
                                         report=report("implementation_report", "i1", bundle_sha="no-files:i1")))
    dispatcher.scripts.add("supervisor:evaluate", next_step("review"))
    await settle(dispatcher)
    assert (await sim.task(task_id))["documents"]["validation"]["passed"]
