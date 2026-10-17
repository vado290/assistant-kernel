"""Job executor (docs/ROUND3_PLAN.md section 6): runs one scheduled job of a project's runtime manifest.

- script: the project's script, in the vault, with a small environment (no Kernel secrets) and a hard timeout;
- agent: one model turn of a product agent (agents/<name>/: config.yaml, role.md, and skills whose load roles name
  it), read-only, with the job's prompt; its final message is the result (posted to a channel if the job says so);
- service_restart: restarts the project's own supervisord program.
A failure raises, so the Kernel retries up to the job's `retries` and then alerts the project's Supervisor channel.
"""
from __future__ import annotations

import asyncio
import os
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from assistant_kernel.core import gitdelivery, runtime, skills as skill_rules
from assistant_kernel.core.agents import AgentConfig, AgentLoader
from assistant_kernel.core.executors.common import READ_ONLY_TOOLS, call_model, charter
from assistant_kernel.storage import ProjectRuntimeManager

LOG_TAIL = 4000
PASSED_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "PYTHONIOENCODING", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
              "http_proxy", "https_proxy", "no_proxy")


class JobFailed(RuntimeError):
    pass


def _log_path(vault: Path, job_id: str, planned: str) -> Path:
    stamp = planned.replace(":", "").replace("-", "")[:15]
    path = vault / "runs" / "jobs" / job_id / f"{stamp}.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


async def run_script(vault: Path, job: dict[str, Any], env_extra: dict[str, str], log: Path,
                     home: Path | None = None) -> dict[str, Any]:
    """Run in `home` (the repository for code delivery, else the vault); the log stays in the vault."""
    env = {key: os.environ[key] for key in PASSED_ENV if key in os.environ}
    env.update({"TZ": job["timezone"], **env_extra})
    started = time.monotonic()
    with log.open("wb") as out:
        process = await asyncio.create_subprocess_exec(
            *runtime.script_argv(job), cwd=str(home or vault), env=env, stdout=out, stderr=asyncio.subprocess.STDOUT,
            start_new_session=True)
        try:
            code = await asyncio.wait_for(process.wait(), timeout=job["timeout_seconds"])
        except asyncio.TimeoutError:
            os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
            raise JobFailed(f"超时（{job['timeout_seconds']} 秒）被终止；日志 {log.relative_to(vault)}")
    tail = log.read_bytes()[-LOG_TAIL:].decode("utf-8", "replace").strip()
    if code != 0:
        raise JobFailed(f"退出码 {code}；日志 {log.relative_to(vault)}；末尾输出：{tail[-500:]}")
    return {"exit_code": 0, "seconds": round(time.monotonic() - started, 1), "log": str(log.relative_to(vault)),
            "summary": tail.splitlines()[-1][:300] if tail else "完成"}


def load_product_agent(settings, project_id: str, name: str) -> AgentConfig:
    loader = AgentLoader(settings)
    config, files = loader.load(project_id, f"project:{project_id}:{name}")
    vault = loader.runtime.ensure(project_id).vault_root
    capabilities = skill_rules.project_config(vault)["capabilities"]
    selection = skill_rules.select(vault, settings.data_root, role=name, purpose="job", capabilities=capabilities)
    return AgentConfig(**{**config.__dict__, "skills_text": selection.render(vault), "skill_labels": tuple(selection.labels),
                          "config_sha256": skill_rules.digest(files + selection.files)})


async def run_agent(run: dict[str, Any], ctx, vault: Path, job: dict[str, Any], log: Path) -> dict[str, Any]:
    settings = ctx.services["settings"]
    name = job["entry"].split("/", 1)[1]
    config = await asyncio.to_thread(load_product_agent, settings, run["project_id"], name)
    project_charter = await asyncio.to_thread(charter, settings, run["project_id"])
    planned = datetime.fromisoformat(run["input"]["planned_at"]).astimezone(ZoneInfo(job["timezone"]))
    prompt = "\n\n".join([
        f"# 项目 {run['project_id']} · 定时作业 {job['id']}（agent {name}）",
        *(["## 项目章程\n\n" + project_charter] if project_charter else []),
        "## 你的角色\n\n" + config.role_text,
        *([config.skills_text] if config.skills_text else []),
        f"## 本次任务（计划时间 {planned:%Y-%m-%d %H:%M}，{job['timezone']}）\n\n{job['prompt']}",
        "## 输出要求\n\n你只能读取，不能修改项目文件。完成后，最后一条消息直接写给用户看的结果（简洁、结论先行），不要写 JSON。",
    ])
    result = await call_model(ctx, run, config, prompt, task_id=None, stage=None, session_id=None,
                              tools=READ_ONLY_TOOLS, sandbox="read-only")
    text = (result.final_text or "").strip()
    log.write_text(text, "utf-8")
    if not text:
        raise JobFailed("agent 没有给出结果")
    return {"summary": text, "log": str(log.relative_to(vault)), "usage": result.usage.model_dump()}


async def execute_job(run: dict[str, Any], ctx) -> dict[str, Any]:
    settings = ctx.services["settings"]
    project_id, job = run["project_id"], run["input"]["spec"]
    vault = ProjectRuntimeManager(settings.data_root).ensure(project_id).vault_root
    planned = run["input"]["planned_at"]
    log = _log_path(vault, job["id"], planned)
    if job["type"] == "script":
        env = {"PROJECT_ID": project_id, "JOB_ID": job["id"], "PLANNED_AT": planned, "RUN_ID": str(run["run_id"]),
               "MANUAL_RUN": "1" if run["input"].get("manual") else "0",
               "STARTED_AT": datetime.now(timezone.utc).isoformat()}
        code = gitdelivery.config(settings, project_id)   # code delivery: scripts live in the repository
        return await run_script(vault, job, env, log, code["repo"] if code else None)
    if job["type"] == "agent":
        return await run_agent(run, ctx, vault, job, log)
    output = await asyncio.to_thread(runtime.CONTROL.restart, project_id, job["entry"])
    log.write_text(output, "utf-8")
    if "ERROR" in output.upper() and "STARTED" not in output.upper():
        raise JobFailed(f"重启服务 {job['entry']} 失败：{output[-300:]}")
    return {"summary": f"服务 {job['entry']} 已重启", "log": str(log.relative_to(vault))}
