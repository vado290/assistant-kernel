"""Job store and runtime sync (docs/ROUND3_PLAN.md section 6.2).

- sync: a project's published jobs.yaml → core.jobs (new: enabled, changed: updated, gone: retired),
  and its services → supervisord programs (only the changed ones are updated);
- schedule: due jobs become `job` runs (job id + planned time is the idempotency key; missed times are skipped,
  only the latest runs; a job whose previous run is still going is skipped this time);
- operate: list / pause / resume / run now, and service health checks.
Only the production dispatcher runs these (Dispatcher(run_jobs=True)); a smoke test with a temporary database never
touches supervisord.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from psycopg.types.json import Jsonb

from assistant_kernel.core import gitdelivery, runtime
from assistant_kernel.core.db import all_rows, event, lock_project, one
from assistant_kernel.core.machine import enqueue_job, notify
from assistant_kernel.services import schedule_time
from assistant_kernel.storage import ProjectRuntimeManager

log = logging.getLogger(__name__)
STATUS_NAMES = {"enabled": "启用", "paused": "已暂停", "retired": "已退役"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def next_at(spec: dict[str, Any], after: datetime) -> datetime | None:
    return schedule_time.next_time(spec["schedule"], spec["timezone"], after)


def _vault(settings, project_id: str):
    return ProjectRuntimeManager(settings.data_root).ensure(project_id).vault_root


# ------------------------------------------------------------------ manifest → core.jobs

async def apply_manifest(kernel, project_id: str, sha: str | None, parsed: dict[str, list]) -> dict[str, list[str]]:
    """Make core.jobs match a parsed manifest (sha None = no manifest). Returns what changed."""
    wanted = {j["id"]: ("job", j) for j in parsed["jobs"]} | {s["id"]: ("service", s) for s in parsed["services"]}
    changes: dict[str, list[str]] = {"added": [], "updated": [], "retired": [], "services": [], "services_removed": []}
    now = _now()
    async with kernel.db.tx() as c:
        await lock_project(c, project_id)
        rows = {r["job_id"]: r for r in await all_rows(c, "SELECT * FROM core.jobs WHERE project_id=%s", (project_id,))}
        for job_id, (kind, spec) in wanted.items():
            row = rows.get(job_id)
            due = next_at(spec, now) if kind == "job" else None
            if row is None or row["status"] == "retired":
                await c.execute("""INSERT INTO core.jobs(project_id, job_id, kind, spec, status, next_at, manifest_sha)
                                   VALUES (%s, %s, %s, %s, 'enabled', %s, %s)
                                   ON CONFLICT (project_id, job_id) DO UPDATE SET kind=EXCLUDED.kind, spec=EXCLUDED.spec,
                                     status='enabled', next_at=EXCLUDED.next_at, failures=0,
                                     manifest_sha=EXCLUDED.manifest_sha, updated_at=now()""",
                                (project_id, job_id, kind, Jsonb(spec), due, "" if kind == "service" else sha))
                changes["added"].append(job_id)
                if kind == "service":
                    changes["services"].append(job_id)
            elif row["spec"] != spec or row["kind"] != kind or (kind == "service" and not row["manifest_sha"]):
                keep = row["next_at"] if kind == "job" and row["spec"].get("schedule") == spec.get("schedule") else due
                await c.execute("""UPDATE core.jobs SET kind=%s, spec=%s, next_at=%s, manifest_sha=%s, updated_at=now()
                                   WHERE project_id=%s AND job_id=%s""", (kind, Jsonb(spec), keep, "" if kind == "service" else sha, project_id, job_id))
                changes["updated"].append(job_id)
                if kind == "service":
                    changes["services"].append(job_id)
            elif row["manifest_sha"] != sha:
                await c.execute("UPDATE core.jobs SET manifest_sha=%s WHERE project_id=%s AND job_id=%s",
                                (sha, project_id, job_id))
        for job_id, row in rows.items():
            if job_id not in wanted and row["status"] != "retired":
                await c.execute("""UPDATE core.jobs SET status='retired', next_at=NULL,
                                   manifest_sha=CASE WHEN kind='service' THEN '' ELSE manifest_sha END, updated_at=now()
                                   WHERE project_id=%s AND job_id=%s""", (project_id, job_id))
                changes["retired"].append(job_id)
                if row["kind"] == "service":
                    changes["services_removed"].append(job_id)
            elif job_id not in wanted and row["kind"] == "service" and row["manifest_sha"] != runtime.manifest_sha(""):
                changes["services_removed"].append(job_id)   # includes legacy retirement without an apply acknowledgment
        if any(changes.values()):
            await event(c, project_id, "runtime_synced", manifest_sha=sha, **changes)
    return changes


async def sync_runtime(kernel, settings) -> int:
    """Bring every active project's jobs and services in line with its published manifest."""
    synced = 0
    for project_id in await kernel.active_projects():
        try:
            synced += await _sync_project(kernel, settings, project_id)
        except Exception:   # one broken project must not stop the others
            log.exception("runtime sync failed for %s", project_id)
    return synced


def released_manifest(settings, project_id: str) -> tuple[str | None, Callable[[str], bool], Path | None]:
    """(jobs.yaml text, path check, home) of what the project released. File delivery: the vault's jobs.yaml (gated on
    the release record by the caller). Code delivery: jobs.yaml at the repository root on the main branch, which only
    moves by approved fast-forwards; jobs and services then run in the repository (home), agents stay in the vault."""
    vault = _vault(settings, project_id)
    code = gitdelivery.config(settings, project_id)
    if code is None:
        path = vault / runtime.MANIFEST
        return (path.read_text("utf-8") if path.is_file() else None), lambda p: (vault / p).exists(), None

    def on_main(p: str) -> bool:
        if p.startswith("agents/"):
            return (vault / p).exists()
        try:
            gitdelivery.git("cat-file", "-e", f"{code['main']}:{p}", cwd=code["repo"])
            return True
        except gitdelivery.GitError:
            return False

    # A read failure is not an approved deletion. Resolve the ref and distinguish an absent tree entry first.
    gitdelivery.git("rev-parse", "--verify", code["main"], cwd=code["repo"])
    present = gitdelivery.git("ls-tree", "--name-only", code["main"], "--", runtime.MANIFEST, cwd=code["repo"])
    text = gitdelivery.git("show", f"{code['main']}:{runtime.MANIFEST}", cwd=code["repo"]) if present.strip() else None
    return text, on_main, code["repo"]


async def _sync_project(kernel, settings, project_id: str) -> int:
    vault = _vault(settings, project_id)
    text, exists, home = await asyncio.to_thread(released_manifest, settings, project_id)
    sha = runtime.manifest_sha(text) if text is not None else None
    async with kernel.db.tx() as c:
        stored = await one(c, """SELECT count(*) AS n, count(*) FILTER (WHERE manifest_sha IS DISTINCT FROM %s) AS stale,
                                 count(*) FILTER (WHERE kind='service') AS services FROM core.jobs WHERE project_id=%s
                                 AND (status <> 'retired' OR (kind='service' AND manifest_sha<>%s))""",
                           (sha, project_id, runtime.manifest_sha("")))
        publication = await one(c, """SELECT payload->>'manifest_sha' AS sha FROM core.task_events
                                      WHERE project_id=%s AND kind='runtime_manifest_published' ORDER BY event_id DESC LIMIT 1""",
                                (project_id,))
        seen = {r["kind"] for r in await all_rows(c, """SELECT DISTINCT kind FROM core.task_events WHERE project_id=%s
                    AND kind LIKE 'runtime_manifest_%%' AND payload->>'manifest_sha' IS NOT DISTINCT FROM %s""", (project_id, sha))}
    released = home is not None or bool(publication and publication["sha"] == sha)
    if sha is None and not stored["n"]:
        return 0
    if not released:   # written outside a release (e.g. straight from a build turn's shell): never registered
        if "runtime_manifest_unreleased" in seen:   # already said
            return 0
        async with kernel.db.tx() as c:
            await event(c, project_id, "runtime_manifest_unreleased", manifest_sha=sha)
            await notify(c, project_id, "supervisor", f"⚠ {runtime.MANIFEST} 被直接修改过，与发布的版本不同，Kernel 没有登记"
                         "这些变化。作业和服务的改动请通过任务发布（批准发布即登记）。")
        return 0
    if "runtime_manifest_invalid" in seen or (sha is not None and stored["n"] and not stored["stale"] and not stored["services"]):
        return 0
    try:
        parsed = runtime.parse_manifest(text, exists) if text is not None else {"jobs": [], "services": []}
    except runtime.ManifestError as exc:   # published but broken (validation should have caught it): say so once
        async with kernel.db.tx() as c:
            await event(c, project_id, "runtime_manifest_invalid", manifest_sha=sha, error=str(exc)[:1000])
            await notify(c, project_id, "supervisor", f"⚠ {runtime.MANIFEST} 有误，未登记任何变化：{str(exc)[:500]}")
        return 0
    if home is not None and (parsed["services"] or any(j["type"] == "script" for j in parsed["jobs"])):
        try:
            await asyncio.to_thread(gitdelivery.runtime_home, gitdelivery.config(settings, project_id))
        except gitdelivery.GitError as exc:
            async with kernel.db.tx() as c:
                await _apply_notice(c, project_id, sha, "runtime_apply_failed", str(exc))
            return 0
    changes = await apply_manifest(kernel, project_id, sha, parsed)
    # A code fix to program generation must also repair existing configuration without restarting unchanged services.
    needs_update = getattr(runtime.CONTROL, "needs_update", None)
    for service in parsed["services"] if needs_update else []:
        if service["id"] not in changes["services"] and await asyncio.to_thread(needs_update, project_id, vault, service, home):
            changes["services"].append(service["id"])
            changes["updated"].append(service["id"])
    if changes["services"] or changes["services_removed"]:
        services = [s for s in parsed["services"] if s["id"] in changes["services"]]
        pending = changes["services"] + changes["services_removed"]
        async with kernel.db.tx() as c:
            await c.execute("UPDATE core.jobs SET manifest_sha='' WHERE project_id=%s AND job_id = ANY(%s)", (project_id, pending))
        try:
            await asyncio.to_thread(runtime.CONTROL.apply, project_id, vault, services, changes["services_removed"], home)
        except Exception as exc:   # pending marker survives for both active and retired services
            async with kernel.db.tx() as c:
                await _apply_notice(c, project_id, sha, "runtime_apply_failed", "服务配置尚未应用，保留待重试：" + str(exc)[:300])
            raise
        else:
            async with kernel.db.tx() as c:
                # manifest_sha is NOT NULL; retired rows use the empty-content digest to acknowledge deletion.
                await c.execute("""UPDATE core.jobs SET manifest_sha=CASE WHEN status='retired' THEN %s ELSE %s END
                                   WHERE project_id=%s AND job_id = ANY(%s)""",
                                (runtime.manifest_sha(""), sha, project_id, pending))
    async with kernel.db.tx() as c:
        await _apply_notice(c, project_id, sha, "runtime_apply_recovered", "运行配置及工作区检查已恢复；业务效果仍需核对。")
    shown = [f"{label}：{'、'.join(changes[key])}" for key, label in (("added", "新增"), ("updated", "修改"),
                                                                    ("retired", "退役")) if changes[key]]
    if shown:
        async with kernel.db.tx() as c:
            await notify(c, project_id, "supervisor", "⚙ 运行时已按发布的 jobs.yaml 更新（" + "；".join(shown)
                         + "）。发送 /jobs 查看。")
    return int(any(changes.values()))


async def _apply_notice(c, project_id, sha, kind, text):
    previous = await one(c, """SELECT kind FROM core.task_events WHERE project_id=%s
                              AND kind IN ('runtime_apply_failed','runtime_apply_recovered')
                              AND payload->>'manifest_sha' IS NOT DISTINCT FROM %s ORDER BY event_id DESC LIMIT 1""", (project_id, sha))
    if (previous or {}).get("kind") == kind or (kind == "runtime_apply_recovered" and not previous):
        return
    await event(c, project_id, kind, manifest_sha=sha)
    await notify(c, project_id, "supervisor", text)


# ------------------------------------------------------------------ scheduling

async def schedule_jobs(kernel, now: datetime | None = None) -> list[str]:
    """Queue a `job` run for every due job. Missed times are skipped (only the latest runs); a job still running
    is skipped this time. Returns the job ids queued."""
    now = now or _now()
    queued = []
    async with kernel.db.tx() as c:
        due = await all_rows(c, """SELECT * FROM core.jobs WHERE status='enabled' AND kind='job' AND next_at <= %s
                                   ORDER BY next_at FOR UPDATE SKIP LOCKED""", (now,))
        for job in due:
            planned = job["next_at"]
            busy = await one(c, """SELECT 1 FROM core.runs WHERE run_type='job' AND mutex_key=%s
                                   AND status IN ('queued','running','reconciling')""", (_mutex(job),))
            if busy:
                await event(c, job["project_id"], "job_skipped", job_id=job["job_id"], planned_at=planned.isoformat(),
                            reason="previous run still going")
            else:
                await _enqueue(c, job, planned, manual=False)
                queued.append(job["job_id"])
            await c.execute("UPDATE core.jobs SET next_at=%s WHERE project_id=%s AND job_id=%s",
                            (next_at(job["spec"], now), job["project_id"], job["job_id"]))
    return queued


def _mutex(job) -> str:
    return f"job:{job['project_id']}:{job['job_id']}"


async def _enqueue(c, job, planned: datetime, *, manual: bool) -> dict[str, Any]:
    return await enqueue_job(c, job, planned, manual=manual, mutex_key=_mutex(job))


# ------------------------------------------------------------------ operating (the /jobs command)

async def list_jobs(kernel, project_id: str) -> list[dict[str, Any]]:
    async with kernel.db.tx() as c:
        return await all_rows(c, """SELECT * FROM core.jobs WHERE project_id=%s AND status <> 'retired'
                                    ORDER BY kind, job_id""", (project_id,))


async def set_status(kernel, project_id: str, job_id: str, status: str) -> dict[str, Any] | None:
    async with kernel.db.tx() as c:
        job = await one(c, "SELECT * FROM core.jobs WHERE project_id=%s AND job_id=%s AND status <> 'retired' FOR UPDATE",
                        (project_id, job_id))
        if job is None or job["kind"] != "job":
            return None
        due = next_at(job["spec"], _now()) if status == "enabled" else job["next_at"]
        await c.execute("UPDATE core.jobs SET status=%s, next_at=%s, updated_at=now() WHERE project_id=%s AND job_id=%s",
                        (status, due, project_id, job_id))
        await event(c, project_id, "job_status", job_id=job_id, status=status)
        return {**job, "status": status, "next_at": due}


async def run_now(kernel, project_id: str, job_id: str) -> dict[str, Any] | None:
    """Queue one run immediately; the schedule (next time) is not changed."""
    async with kernel.db.tx() as c:
        job = await one(c, "SELECT * FROM core.jobs WHERE project_id=%s AND job_id=%s AND status <> 'retired'",
                        (project_id, job_id))
        if job is None or job["kind"] != "job":
            return None
        return await _enqueue(c, job, _now(), manual=True)


async def check_services(kernel) -> int:
    """Alert once when a registered service leaves RUNNING, and once when it is back."""
    async with kernel.db.tx() as c:
        services = await all_rows(c, "SELECT * FROM core.jobs WHERE kind='service' AND status <> 'retired'")
    if not services:
        return 0
    states = await asyncio.to_thread(runtime.CONTROL.status)
    changed = 0
    for service in services:
        state = states.get(runtime.program_name(service["project_id"], service["job_id"]), "MISSING")
        if state in ("STARTING", "BACKOFF") or state == (service["last_status"] or "RUNNING"):
            continue
        async with kernel.db.tx() as c:
            await c.execute("UPDATE core.jobs SET last_status=%s, last_at=now() WHERE project_id=%s AND job_id=%s",
                            (state, service["project_id"], service["job_id"]))
            text = (f"✅ 服务 {service['job_id']} 已恢复运行。" if state == "RUNNING"
                    else f"⚠ 服务 {service['job_id']} 当前状态 {state}（supervisord 会自动重启崩溃的进程；持续异常请检查日志 "
                         f"runs/services/{service['job_id']}.err.log）。")
            await notify(c, service["project_id"], "supervisor", text)
        changed += 1
    return changed


def render_jobs(rows: list[dict[str, Any]], zone: str = runtime.DEFAULT_TZ) -> str:
    if not rows:
        return "本项目没有登记的作业或服务（由已发布的 jobs.yaml 决定）。"
    from zoneinfo import ZoneInfo
    lines = ["本项目的作业和服务："]
    for r in rows:
        if r["kind"] == "service":
            lines.append(f"- 服务 `{r['job_id']}`：{r['last_status'] or '已登记'}")
            continue
        when = r["next_at"].astimezone(ZoneInfo(zone)).strftime("%m-%d %H:%M") if r["next_at"] else "—"
        last = f"，上次 {r['last_status']}" if r["last_status"] else ""
        fails = f"，连续失败 {r['failures']} 次" if r["failures"] else ""
        lines.append(f"- 作业 `{r['job_id']}`（{r['spec']['type']}）：{STATUS_NAMES[r['status']]}，下次 {when}{last}{fails}")
    lines.append("命令：/jobs pause <id> · /jobs resume <id> · /jobs run <id>")
    return "\n".join(lines)
