"""ROUND3 S3: the shared job and service runtime (manifest → core.jobs → job runs → alerts, /jobs, health)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from assistant_kernel.core import jobs, runtime
from assistant_kernel.core.dispatch import Dispatcher
from assistant_kernel.core.executors.delivery import check_runtime_manifest
from assistant_kernel.core.executors.digest import render_digest
from assistant_kernel.core.executors.job import JobFailed, execute_job

from .test_dispatch import settle
from .test_gateway import gateway  # noqa: F401  (a fixture)

MANIFEST = """schema_version: runtime-manifest.v1
jobs:
  - id: daily-report
    type: script
    entry: scripts/report.py
    schedule: {kind: cron, expression: "0 8 * * *"}
    retries: 0
services:
  - id: listener
    command: python3 scripts/listen.py
    restart_schedule: {kind: cron, expression: "0 4 1 * *"}
"""


class FakeControl:
    def __init__(self):
        self.applied, self.restarted, self.states = [], [], {}

    def apply(self, project_id, vault, services, removed):
        self.applied.append((project_id, [s["id"] for s in services], list(removed)))
        return []

    def restart(self, project_id, service_id):
        self.restarted.append(service_id)
        return f"{service_id}: started"

    def status(self):
        return dict(self.states)


@pytest.fixture
def control(monkeypatch):
    fake = FakeControl()
    monkeypatch.setattr(runtime, "CONTROL", fake)
    return fake


@pytest.fixture
def vault(kernel_settings):
    root = kernel_settings.data_root / "vaults" / "p1"
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "scripts" / "report.py").write_text("print('report ok')\n", "utf-8")
    (root / "scripts" / "listen.py").write_text("", "utf-8")
    (root / "runtime").mkdir(exist_ok=True)
    return root


def exists_in(root):
    return lambda path: (root / path).exists()


def test_manifest_validation(vault):
    parsed = runtime.parse_manifest(MANIFEST, exists_in(vault))
    assert [j["id"] for j in parsed["jobs"]] == ["listener-restart", "daily-report"]
    assert parsed["services"][0]["command"] == ["python3", "scripts/listen.py"]
    bad = {
        "missing script": MANIFEST.replace("scripts/report.py", "scripts/nope.py"),
        "bad cron": MANIFEST.replace('"0 8 * * *"', '"every day"'),
        "duplicate id": MANIFEST.replace("id: listener", "id: daily-report"),
        "escaping path": MANIFEST.replace("scripts/report.py", "../other/x.py"),
        "unknown type": MANIFEST.replace("type: script", "type: cron"),
        "agent without files": MANIFEST.replace("type: script\n    entry: scripts/report.py",
                                                "type: agent\n    entry: agents/analyst\n    prompt: 写分析"),
    }
    for name, text in bad.items():
        with pytest.raises(runtime.ManifestError):
            runtime.parse_manifest(text, exists_in(vault))
        assert name


async def test_sync_registers_updates_and_retires(kernel, sim, kernel_settings, vault, control):
    (vault / runtime.MANIFEST).write_text(MANIFEST, "utf-8")
    assert await jobs.sync_runtime(kernel, kernel_settings) == 1
    assert await jobs.sync_runtime(kernel, kernel_settings) == 0          # unchanged manifest: nothing to do
    rows = {r["job_id"]: r for r in await jobs.list_jobs(kernel, "p1")}
    assert set(rows) == {"daily-report", "listener", "listener-restart"} and rows["daily-report"]["next_at"]
    assert control.applied == [("p1", ["listener"], [])]
    assert any("运行时已按发布" in m["body"]["text"] for m in await sim.outbound("p1"))

    (vault / runtime.MANIFEST).write_text(MANIFEST.split("services:")[0], "utf-8")   # the service is removed
    await jobs.sync_runtime(kernel, kernel_settings)
    assert {r["job_id"] for r in await jobs.list_jobs(kernel, "p1")} == {"daily-report"}
    assert control.applied[-1] == ("p1", [], ["listener"])

    (vault / runtime.MANIFEST).write_text("schema_version: runtime-manifest.v1\njobs:\n  - id: x\n", "utf-8")
    await jobs.sync_runtime(kernel, kernel_settings)
    await jobs.sync_runtime(kernel, kernel_settings)
    alerts = [m for m in await sim.outbound("p1") if "有误" in m["body"]["text"]]
    assert len(alerts) == 1                                                # a broken manifest is reported once
    assert {r["job_id"] for r in await jobs.list_jobs(kernel, "p1")} == {"daily-report"}   # and changes nothing


async def test_due_jobs_run_once_and_failures_alert(kernel, sim, kernel_settings, vault, control):
    (vault / runtime.MANIFEST).write_text(MANIFEST, "utf-8")
    await jobs.sync_runtime(kernel, kernel_settings)
    async with kernel.db.tx() as c:
        await c.execute("UPDATE core.jobs SET next_at = now() - interval '3 days' WHERE job_id='daily-report'")
    assert await jobs.schedule_jobs(kernel) == ["daily-report"]           # three missed days: one run
    assert await jobs.schedule_jobs(kernel) == []
    d = Dispatcher(kernel, {"job": execute_job}, services={"settings": kernel_settings})
    await settle(d)
    row = next(r for r in await jobs.list_jobs(kernel, "p1") if r["job_id"] == "daily-report")
    assert row["last_status"] == "succeeded" and row["next_at"] > datetime.now(timezone.utc)
    assert "report ok" in next((vault / "runs" / "jobs" / "daily-report").glob("*.log")).read_text("utf-8")

    (vault / "scripts" / "report.py").write_text("import sys\nprint('boom')\nsys.exit(3)\n", "utf-8")
    await jobs.run_now(kernel, "p1", "daily-report")
    await settle(d)
    row = next(r for r in await jobs.list_jobs(kernel, "p1") if r["job_id"] == "daily-report")
    assert row["last_status"] == "failed" and row["failures"] == 1
    alert = [m["body"]["text"] for m in await sim.outbound("p1") if "运行失败" in m["body"]["text"]]
    assert alert and "退出码 3" in alert[0] and "/jobs run daily-report" in alert[0]

    (vault / "scripts" / "report.py").write_text("print('fixed')\n", "utf-8")
    await jobs.run_now(kernel, "p1", "daily-report")
    await settle(d)
    assert any("已恢复正常" in m["body"]["text"] for m in await sim.outbound("p1"))

    # the service restart job restarts only the project's own program
    await jobs.run_now(kernel, "p1", "listener-restart")
    await settle(d)
    assert control.restarted == ["listener"]


async def test_a_job_still_running_is_skipped(kernel, kernel_settings, vault, control):
    (vault / runtime.MANIFEST).write_text(MANIFEST, "utf-8")
    await jobs.sync_runtime(kernel, kernel_settings)
    await jobs.run_now(kernel, "p1", "daily-report")                       # queued, not yet run
    async with kernel.db.tx() as c:
        await c.execute("UPDATE core.jobs SET next_at = now() - interval '1 minute' WHERE job_id='daily-report'")
    assert await jobs.schedule_jobs(kernel) == []
    async with kernel.db.tx() as c:
        skipped = await (await c.execute("SELECT 1 FROM core.task_events WHERE kind='job_skipped'")).fetchone()
    assert skipped


async def test_script_timeout(kernel_settings, vault):
    (vault / "scripts" / "slow.py").write_text("import time\ntime.sleep(30)\n", "utf-8")
    job = {"id": "slow", "type": "script", "entry": "scripts/slow.py", "args": [], "timezone": "Asia/Shanghai",
           "timeout_seconds": 1}
    from assistant_kernel.core.executors.job import run_script
    with pytest.raises(JobFailed, match="超时"):
        await run_script(vault, job, {}, vault / "slow.log")


async def test_jobs_command_and_service_health(kernel, sim, kernel_settings, vault, control, gateway):  # noqa: F811
    (vault / runtime.MANIFEST).write_text(MANIFEST, "utf-8")
    await jobs.sync_runtime(kernel, kernel_settings)
    listing = await gateway.jobs_command("p1", [])
    assert "daily-report" in listing and "服务 `listener`" in listing
    assert "已暂停" in await gateway.jobs_command("p1", ["pause", "daily-report"])
    assert "已暂停" in await gateway.jobs_command("p1", [])
    assert await jobs.schedule_jobs(kernel, datetime.now(timezone.utc) + timedelta(days=2)) == []   # paused
    assert "已恢复" in await gateway.jobs_command("p1", ["resume", "daily-report"])
    assert "服务不能暂停" in await gateway.jobs_command("p1", ["pause", "listener"])
    assert "已安排立即运行" in await gateway.jobs_command("p1", ["run", "daily-report"])

    control.states = {"proj-p1-listener": "FATAL"}
    assert await jobs.check_services(kernel) == 1 and await jobs.check_services(kernel) == 0
    control.states = {"proj-p1-listener": "RUNNING"}
    assert await jobs.check_services(kernel) == 1
    texts = [m["body"]["text"] for m in await sim.outbound("p1")]
    assert any("FATAL" in t for t in texts) and any("已恢复运行" in t for t in texts)


def test_validation_checks_a_published_manifest(kernel_settings, vault):
    candidate = vault / "runs" / "t1" / "candidate"
    (candidate / "project" / "runtime").mkdir(parents=True)
    files = [{"operation": "create", "target_uri": "workspace://project/jobs.yaml"},
             {"operation": "create", "target_uri": "workspace://project/scripts/new.py"}]
    (candidate / "manifest.json").write_text(json.dumps({"files": files}), "utf-8")
    good = MANIFEST.replace("scripts/report.py", "scripts/new.py")          # the script comes with the same delivery
    (candidate / "project" / runtime.MANIFEST).write_text(good, "utf-8")
    assert check_runtime_manifest(kernel_settings, "p1", "t1") is None
    (candidate / "project" / runtime.MANIFEST).write_text(good.replace("scripts/new.py", "scripts/missing.py"), "utf-8")
    assert "脚本不存在" in check_runtime_manifest(kernel_settings, "p1", "t1")


async def test_digest_counts_job_runs(kernel, kernel_settings, vault, control):
    (vault / runtime.MANIFEST).write_text(MANIFEST, "utf-8")
    await jobs.sync_runtime(kernel, kernel_settings)
    await jobs.run_now(kernel, "p1", "daily-report")
    await settle(Dispatcher(kernel, {"job": execute_job}, services={"settings": kernel_settings}))
    digest = await kernel.project_digest("p1")
    assert "定时作业 daily-report：成功 1 次，失败 0 次" in render_digest(digest)

