"""ROUND3 S3: the shared job and service runtime (manifest → core.jobs → job runs → alerts, /jobs, health)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from assistant_kernel.core import jobs, runtime
from assistant_kernel.core.db import event
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

    def apply(self, project_id, vault, services, removed, home=None):
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


@pytest.mark.parametrize("known", [False, True])
async def test_service_last_at_is_change_time_not_a_health_heartbeat(
        kernel, kernel_settings, project_files, control, known):
    from assistant_kernel.core.executors.digest import evolution_material
    old = datetime(2020, 1, 1, tzinfo=timezone.utc) if known else None
    async with kernel.db.tx() as c:
        await c.execute("""INSERT INTO core.jobs(project_id,job_id,kind,spec,manifest_sha,last_status,last_at)
            VALUES ('p1','stable','service','{}','x',%s,%s)""", ("RUNNING" if known else None, old))
    control.states[runtime.program_name("p1", "stable")] = "RUNNING"
    assert await jobs.check_services(kernel) == 0
    assert await jobs.check_services(kernel) == 0
    context = await kernel.evolution_context("p1")
    assert context["runtime"][0]["last_at"] == old
    assert context["runtime"][0]["last_status"] == ("RUNNING" if known else None)
    text = await evolution_material(kernel, kernel_settings, "p1")
    assert "observed_at is when this database read happened, not a health check" in text
    assert "for kind=job last_at is the time of the last run result" in text
    assert "for kind=service the time of the last state change" in text
    assert "that alone does not mean a check is overdue or the service is broken" in text


@pytest.fixture
def vault(kernel_settings):
    root = kernel_settings.data_root / "vaults" / "p1"
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "scripts" / "report.py").write_text("print('report ok')\n", "utf-8")
    (root / "scripts" / "listen.py").write_text("", "utf-8")
    (root / "runtime").mkdir(exist_ok=True)
    return root


async def release(kernel, vault, text: str, project_id: str = "p1") -> None:
    """Publish a jobs.yaml as a release would: the file plus the release record Kernel registers from."""
    (vault / runtime.MANIFEST).write_text(text, "utf-8")
    async with kernel.db.tx() as c:
        await event(c, project_id, "runtime_manifest_published", manifest_sha=runtime.manifest_sha(text))


def exists_in(root):
    return lambda path: (root / path).exists()


def test_service_home_and_control_failure_semantics(tmp_path, monkeypatch):
    from types import SimpleNamespace
    service = {"id": "listener", "command": ["python3", "scripts/listen.py"], "timezone": "Asia/Shanghai"}
    conf = runtime.program_conf("p1", service, tmp_path / "vault", tmp_path / "repo")
    assert f"directory={tmp_path / 'repo'}" in conf and f"{tmp_path / 'vault'}/runs/services" in conf
    monkeypatch.setattr(runtime.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=3, stdout="proj-p1-listener FATAL", stderr=""))
    ctl = runtime.SupervisorControl(tmp_path)
    assert "FATAL" in ctl._ctl("status")
    with pytest.raises(RuntimeError):
        ctl._ctl("update", "proj-p1-listener")


def test_retirement_retry_accepts_only_the_exact_missing_group(tmp_path, monkeypatch):
    from types import SimpleNamespace
    ctl = runtime.SupervisorControl(tmp_path)
    monkeypatch.setattr(runtime.subprocess, "run", lambda args, **kw: SimpleNamespace(
        returncode=1 if args[-2] == "update" else 0,
        stdout="ERROR: no such group: proj-p1-listener" if args[-2] == "update" else "", stderr=""))
    assert ctl.apply("p1", tmp_path, [], ["listener"])
    with pytest.raises(RuntimeError):
        ctl._ctl("update", "proj-p1-listener")
    with pytest.raises(RuntimeError):
        ctl._ctl("update", "proj-p1-other", missing_ok=True)


async def test_legacy_retired_service_gets_one_confirmed_cleanup(kernel, kernel_settings, vault, control):
    await release(kernel, vault, MANIFEST)
    await jobs.sync_runtime(kernel, kernel_settings)
    async with kernel.db.tx() as c:
        await c.execute("UPDATE core.jobs SET status='retired' WHERE project_id='p1'")  # old release retained the old hash
    (vault / "jobs.yaml").unlink()
    assert await jobs.sync_runtime(kernel, kernel_settings) == 0  # missing file alone is not deletion approval
    assert len(control.applied) == 1
    async with kernel.db.tx() as c:
        await event(c, "p1", "runtime_manifest_published", manifest_sha=None)
    assert await jobs.sync_runtime(kernel, kernel_settings) == 1
    assert control.applied[-1][2] == ["listener"]
    assert await jobs.sync_runtime(kernel, kernel_settings) == 0
    assert len(control.applied) == 2


async def test_git_manifest_read_failure_never_retires_services(kernel, kernel_settings, vault, control, monkeypatch):
    from assistant_kernel.core import gitdelivery
    await release(kernel, vault, MANIFEST)
    await jobs.sync_runtime(kernel, kernel_settings)
    monkeypatch.setattr(gitdelivery, "config", lambda *a: {"main": "main", "repo": vault})
    def git(*args, **kw):
        if args[0] == "show": raise gitdelivery.GitError("cannot read object")
        return "jobs.yaml" if args[0] == "ls-tree" else "abc"
    monkeypatch.setattr(gitdelivery, "git", git)
    with pytest.raises(gitdelivery.GitError):
        await jobs._sync_project(kernel, kernel_settings, "p1")
    assert len(await jobs.list_jobs(kernel, "p1")) == 3 and len(control.applied) == 1
    monkeypatch.setattr(gitdelivery, "git", lambda *a, **kw: "")
    assert jobs.released_manifest(kernel_settings, "p1")[0] is None  # genuinely absent tree entry


async def test_missing_manifest_requires_an_explicit_published_deletion(kernel, kernel_settings, vault, control):
    await release(kernel, vault, MANIFEST)
    await jobs.sync_runtime(kernel, kernel_settings)
    (vault / "jobs.yaml").unlink()
    assert await jobs.sync_runtime(kernel, kernel_settings) == 0
    assert len(await jobs.list_jobs(kernel, "p1")) == 3
    assert len(control.applied) == 1
    async with kernel.db.tx() as c:
        await event(c, "p1", "runtime_manifest_published", manifest_sha=None)
    assert await jobs.sync_runtime(kernel, kernel_settings) == 1
    assert not await jobs.list_jobs(kernel, "p1")
    assert control.applied[-1][2] == ["listener"]


async def test_service_retirement_failure_is_retried_and_reported_once(kernel, sim, kernel_settings, vault, control, monkeypatch):
    await release(kernel, vault, MANIFEST)
    await jobs.sync_runtime(kernel, kernel_settings)
    await jobs.set_status(kernel, "p1", "daily-report", "paused")
    await release(kernel, vault, MANIFEST.split("services:")[0])
    original = control.apply
    def failure(*a, **kw): raise OSError("removal failed")
    monkeypatch.setattr(control, "apply", failure)
    for _ in range(2):
        with pytest.raises(OSError):
            await jobs._sync_project(kernel, kernel_settings, "p1")
    async with kernel.db.tx() as c:
        row = await (await c.execute("SELECT status,manifest_sha FROM core.jobs WHERE project_id='p1' AND job_id='listener'")).fetchone()
    assert row == {"status": "retired", "manifest_sha": ""}
    alerts = [m for m in await sim.outbound("p1") if "stays pending for a retry" in m["body"]["text"]]
    assert len(alerts) == 1
    monkeypatch.setattr(control, "apply", original)
    assert await jobs.sync_runtime(kernel, kernel_settings) == 1
    assert control.applied[-1][2] == ["listener"]
    assert await jobs.sync_runtime(kernel, kernel_settings) == 0
    assert len([m for m in await sim.outbound("p1") if "workspace check recovered" in m["body"]["text"]]) == 1
    assert next(r for r in await jobs.list_jobs(kernel, "p1") if r["job_id"] == "daily-report")["status"] == "paused"


async def test_changed_service_config_is_repaired_without_restarting_unchanged_ones(kernel, kernel_settings, vault, control, monkeypatch):
    await release(kernel, vault, MANIFEST)
    await jobs.sync_runtime(kernel, kernel_settings)
    drift = [True]
    monkeypatch.setattr(control, "needs_update", lambda *a: drift[0], raising=False)
    original = control.apply
    def apply(*args):
        drift[0] = False
        return original(*args)
    monkeypatch.setattr(control, "apply", apply)
    assert await jobs.sync_runtime(kernel, kernel_settings) == 1
    assert len(control.applied) == 2
    assert await jobs.sync_runtime(kernel, kernel_settings) == 0
    assert len(control.applied) == 2


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
    await release(kernel, vault, MANIFEST)
    assert await jobs.sync_runtime(kernel, kernel_settings) == 1
    assert await jobs.sync_runtime(kernel, kernel_settings) == 0          # unchanged manifest: nothing to do
    rows = {r["job_id"]: r for r in await jobs.list_jobs(kernel, "p1")}
    assert set(rows) == {"daily-report", "listener", "listener-restart"} and rows["daily-report"]["next_at"]
    assert control.applied == [("p1", ["listener"], [])]
    assert any("The runtime follows the released jobs.yaml" in m["body"]["text"] for m in await sim.outbound("p1"))

    await release(kernel, vault, MANIFEST.split("services:")[0])   # the service is removed
    await jobs.sync_runtime(kernel, kernel_settings)
    assert {r["job_id"] for r in await jobs.list_jobs(kernel, "p1")} == {"daily-report"}
    assert control.applied[-1] == ("p1", [], ["listener"])

    await release(kernel, vault, "schema_version: runtime-manifest.v1\njobs:\n  - id: x\n")
    await jobs.sync_runtime(kernel, kernel_settings)
    await jobs.sync_runtime(kernel, kernel_settings)
    alerts = [m for m in await sim.outbound("p1") if "has errors" in m["body"]["text"]]
    assert len(alerts) == 1                                                # a broken manifest is reported once
    assert {r["job_id"] for r in await jobs.list_jobs(kernel, "p1")} == {"daily-report"}   # and changes nothing


async def test_due_jobs_run_once_and_failures_alert(kernel, sim, kernel_settings, vault, control):
    await release(kernel, vault, MANIFEST)
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
    alert = [m["body"]["text"] for m in await sim.outbound("p1") if "failed (after" in m["body"]["text"]]
    assert alert and "exit code 3" in alert[0] and "/jobs run daily-report" in alert[0]

    (vault / "scripts" / "report.py").write_text("print('fixed')\n", "utf-8")
    await jobs.run_now(kernel, "p1", "daily-report")
    await settle(d)
    assert any("is back to normal" in m["body"]["text"] for m in await sim.outbound("p1"))

    # the service restart job restarts only the project's own program
    await jobs.run_now(kernel, "p1", "listener-restart")
    await settle(d)
    assert control.restarted == ["listener"]


async def test_a_job_still_running_is_skipped(kernel, kernel_settings, vault, control):
    await release(kernel, vault, MANIFEST)
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
    with pytest.raises(JobFailed, match="timeout"):
        await run_script(vault, job, {}, vault / "slow.log")


async def test_jobs_command_and_service_health(kernel, sim, kernel_settings, vault, control, gateway):  # noqa: F811
    await release(kernel, vault, MANIFEST)
    await jobs.sync_runtime(kernel, kernel_settings)
    listing = await gateway.jobs_command("p1", [])
    assert "daily-report" in listing and "service `listener`" in listing
    assert "paused" in await gateway.jobs_command("p1", ["pause", "daily-report"])
    assert "paused" in await gateway.jobs_command("p1", [])
    assert await jobs.schedule_jobs(kernel, datetime.now(timezone.utc) + timedelta(days=2)) == []   # paused
    assert "resumed" in await gateway.jobs_command("p1", ["resume", "daily-report"])
    assert "a service cannot be paused" in await gateway.jobs_command("p1", ["pause", "listener"])
    assert "will run now" in await gateway.jobs_command("p1", ["run", "daily-report"])

    control.states = {"proj-p1-listener": "FATAL"}
    assert await jobs.check_services(kernel) == 1 and await jobs.check_services(kernel) == 0
    control.states = {"proj-p1-listener": "RUNNING"}
    assert await jobs.check_services(kernel) == 1
    texts = [m["body"]["text"] for m in await sim.outbound("p1")]
    assert any("FATAL" in t for t in texts) and any("is running again" in t for t in texts)


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
    assert "does not exist" in check_runtime_manifest(kernel_settings, "p1", "t1")


async def test_digest_counts_job_runs(kernel, kernel_settings, vault, control):
    await release(kernel, vault, MANIFEST)
    await jobs.sync_runtime(kernel, kernel_settings)
    await jobs.run_now(kernel, "p1", "daily-report")
    await settle(Dispatcher(kernel, {"job": execute_job}, services={"settings": kernel_settings}))
    digest = await kernel.project_digest("p1")
    assert "Scheduled job daily-report: 1 succeeded, 0 failed" in render_digest(digest)

