"""Shared job and service runtime (docs/ROUND3_PLAN.md section 6).

A project declares what should run in its vault's `jobs.yaml` (format runtime-manifest.v1). The file
is published like any other project file, so approving the publish approves the registration. Kernel syncs the
published manifest into `core.jobs`, runs due jobs as `job` runs, and registers services as single supervisord
programs. Kernel only ever touches its own `proj-*` programs; it never reloads or restarts supervisord itself.
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any, Callable

import yaml

from assistant_kernel.services import schedule_time

MANIFEST = "jobs.yaml"   # vault top level (the vault's runtime/ folder holds Kernel internals: codex home, credentials)
SCHEMA = "runtime-manifest.v1"
DEFAULT_TZ = "Asia/Shanghai"
JOB_TYPES = ("script", "agent", "service_restart")
NOTIFY = ("supervisor", "pm", "none")
ID = re.compile(r"[a-z][a-z0-9-]{1,39}")
MAX_TIMEOUT = 6 * 3600
PROGRAM_PREFIX = "proj-"


class ManifestError(ValueError):
    pass


def _relative(path: Any, what: str) -> str:
    text = str(path or "").strip()
    pure = PurePosixPath(text)
    if not text or pure.is_absolute() or ".." in pure.parts:
        raise ManifestError(f"{what} 必须是项目内的相对路径：{text!r}")
    return pure.as_posix()


def _schedule(rule: Any, zone: str, what: str) -> dict[str, Any]:
    if not isinstance(rule, dict):
        raise ManifestError(f"{what} 的 schedule 必须是对象，例如 {{kind: cron, expression: \"0 8 * * *\"}}")
    try:
        schedule_time.validate_rule(rule, zone)
    except (ValueError, KeyError) as exc:
        raise ManifestError(f"{what} 的时间规则无效：{exc}") from exc
    return dict(rule)


def parse_manifest(text: str, exists: Callable[[str], bool]) -> dict[str, list[dict[str, Any]]]:
    """Validate a manifest; returns normalised {"jobs": [...], "services": [...]}. Raises ManifestError with every
    problem found. `exists(relative_path)` tells whether a file will exist in the project after publishing."""
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ManifestError(f"{MANIFEST} 不是合法的 YAML：{exc}") from exc
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA:
        raise ManifestError(f"{MANIFEST} 必须是对象，且 schema_version: {SCHEMA}")
    problems: list[str] = []
    services, jobs, seen = [], [], set()

    def unique(item_id: Any, what: str) -> str:
        item_id = str(item_id or "")
        if not ID.fullmatch(item_id):
            raise ManifestError(f"{what} 的 id 必须是 2–40 个小写字母、数字或连字符，以字母开头：{item_id!r}")
        if item_id in seen:
            raise ManifestError(f"id 重复：{item_id}")
        seen.add(item_id)
        return item_id

    for raw in data.get("services") or []:
        try:
            if not isinstance(raw, dict):
                raise ManifestError("services 中的每一项必须是对象")
            sid = unique(raw.get("id"), "服务")
            command = raw.get("command")
            argv = shlex.split(command) if isinstance(command, str) else [str(a) for a in command or []]
            if not argv:
                raise ManifestError(f"服务 {sid} 缺少 command")
            if argv[0].endswith((".py", ".sh")) or "/" in argv[0]:
                _relative(argv[0], f"服务 {sid} 的程序")
            zone = str(raw.get("timezone") or DEFAULT_TZ)
            service = {"id": sid, "command": argv, "timezone": zone}
            services.append(service)
            if raw.get("restart_schedule"):
                jobs.append({"id": f"{sid}-restart", "type": "service_restart", "entry": sid, "args": [],
                             "schedule": _schedule(raw["restart_schedule"], zone, f"服务 {sid}"), "timezone": zone,
                             "timeout_seconds": 120, "retries": 1, "notify": "supervisor"})
                seen.add(f"{sid}-restart")
        except ManifestError as exc:
            problems.append(str(exc))
    service_ids = {s["id"] for s in services}
    for raw in data.get("jobs") or []:
        try:
            if not isinstance(raw, dict):
                raise ManifestError("jobs 中的每一项必须是对象")
            jid = unique(raw.get("id"), "作业")
            kind = str(raw.get("type") or "")
            if kind not in JOB_TYPES:
                raise ManifestError(f"作业 {jid} 的 type 只能是 {', '.join(JOB_TYPES)}")
            zone = str(raw.get("timezone") or DEFAULT_TZ)
            job = {"id": jid, "type": kind, "args": [str(a) for a in raw.get("args") or []], "timezone": zone,
                   "schedule": _schedule(raw.get("schedule"), zone, f"作业 {jid}"),
                   "timeout_seconds": int(raw.get("timeout_seconds") or 600), "retries": int(raw.get("retries", 1)),
                   "notify": str(raw.get("notify") or "supervisor")}
            if not 10 <= job["timeout_seconds"] <= MAX_TIMEOUT or not 0 <= job["retries"] <= 5:
                raise ManifestError(f"作业 {jid}：timeout_seconds 须在 10–{MAX_TIMEOUT} 之间，retries 须在 0–5 之间")
            if job["notify"] not in NOTIFY:
                raise ManifestError(f"作业 {jid} 的 notify 只能是 {', '.join(NOTIFY)}")
            if kind == "script":
                job["entry"] = _relative(raw.get("entry"), f"作业 {jid} 的 entry")
                if not exists(job["entry"]):
                    raise ManifestError(f"作业 {jid} 的脚本不存在：{job['entry']}")
            elif kind == "agent":
                job["entry"] = _relative(raw.get("entry"), f"作业 {jid} 的 entry")
                if not re.fullmatch(r"agents/[a-z][a-z0-9_-]*", job["entry"]):
                    raise ManifestError(f"作业 {jid} 的 entry 必须是 agents/<名称>")
                if not exists(f"{job['entry']}/config.yaml") or not exists(f"{job['entry']}/role.md"):
                    raise ManifestError(f"作业 {jid} 的 agent 缺少 {job['entry']}/config.yaml 或 role.md")
                job["prompt"] = str(raw.get("prompt") or "").strip()
                if not job["prompt"]:
                    raise ManifestError(f"作业 {jid}（agent）需要 prompt：每次运行交给 agent 的任务")
                job["post_result"] = str(raw.get("post_result") or "pm")
                if job["post_result"] not in NOTIFY:
                    raise ManifestError(f"作业 {jid} 的 post_result 只能是 {', '.join(NOTIFY)}")
            else:
                job["entry"] = str(raw.get("entry") or "")
                if job["entry"] not in service_ids:
                    raise ManifestError(f"作业 {jid} 要重启的服务 {job['entry']!r} 不在 services 中")
            jobs.append(job)
        except (ManifestError, TypeError, ValueError) as exc:
            problems.append(str(exc))
    if problems:
        raise ManifestError("；".join(problems))
    return {"jobs": jobs, "services": services}


def script_argv(job: dict[str, Any]) -> list[str]:
    entry = job["entry"]
    if entry.endswith(".py"):
        return ["python3", entry, *job["args"]]
    if entry.endswith(".sh"):
        return ["bash", entry, *job["args"]]
    return [f"./{entry}", *job["args"]]


# ------------------------------------------------------------------ supervisord programs for services

def program_name(project_id: str, service_id: str) -> str:
    return f"{PROGRAM_PREFIX}{project_id}-{service_id}"


def program_conf(project_id: str, service: dict[str, Any], vault: Path) -> str:
    logs = vault / "runs" / "services"
    command = " ".join(shlex.quote(part) for part in service["command"])
    return (f"[program:{program_name(project_id, service['id'])}]\n"
            f"command={command}\ndirectory={vault}\nautostart=true\nautorestart=true\nstartsecs=5\n"
            "stopsignal=TERM\nstopwaitsecs=30\nstopasgroup=true\nkillasgroup=true\n"
            f"stdout_logfile={logs / (service['id'] + '.log')}\nstdout_logfile_maxbytes=5MB\nstdout_logfile_backups=2\n"
            f"stderr_logfile={logs / (service['id'] + '.err.log')}\nstderr_logfile_maxbytes=5MB\nstderr_logfile_backups=2\n"
            f'environment=PROJECT_ID="{project_id}",SERVICE_ID="{service["id"]}",TZ="{service["timezone"]}"\n')


class SupervisorControl:
    """supervisorctl for Kernel's own proj-* programs only: write/remove a program file, then update that group."""

    def __init__(self, programs_dir: Path | None = None, conf: str | None = None):
        self.programs_dir = Path(programs_dir or os.environ.get("CORE_PROGRAMS_DIR")
                                 or "/home/v/AI/assistant-data/runtime/kernel-services/programs")
        self.conf = conf or os.environ.get("CORE_SUPERVISOR_CONF") or "/etc/supervisor/conf.d/assistant.conf"

    def _ctl(self, *args: str) -> str:
        if args and args[0] in ("reload", "shutdown") or any(a in ("all",) for a in args):
            raise PermissionError("Kernel only manages its own proj-* programs")
        done = subprocess.run(["supervisorctl", "-c", self.conf, *args], capture_output=True, text=True, timeout=120)
        return (done.stdout + done.stderr).strip()

    def apply(self, project_id: str, vault: Path, services: list[dict[str, Any]], removed: list[str]) -> list[str]:
        """Write the program files for `services`, delete those for `removed`, and update just those groups."""
        self.programs_dir.mkdir(parents=True, exist_ok=True)
        (vault / "runs" / "services").mkdir(parents=True, exist_ok=True)
        names = []
        for service in services:
            name = program_name(project_id, service["id"])
            (self.programs_dir / f"{name}.conf").write_text(program_conf(project_id, service, vault), "utf-8")
            names.append(name)
        for service_id in removed:
            name = program_name(project_id, service_id)
            (self.programs_dir / f"{name}.conf").unlink(missing_ok=True)
            names.append(name)
        if not names:
            return []
        self._ctl("reread")
        return [self._ctl("update", name) for name in names]

    def restart(self, project_id: str, service_id: str) -> str:
        return self._ctl("restart", program_name(project_id, service_id))

    def status(self) -> dict[str, str]:
        """State of every proj-* program, e.g. {"proj-a-listener": "RUNNING"}."""
        states = {}
        for line in self._ctl("status").splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0].startswith(PROGRAM_PREFIX):
                states[parts[0]] = parts[1]
        return states


CONTROL: Any = SupervisorControl()   # tests replace it with a fake
