"""Kernel's own file writes that follow from database state (docs/ROUND2_PLAN.md, R1).

The state machine only changes the database. Whatever must also land in a vault file is written here,
on the dispatcher's maintenance tick. Every step is idempotent: the file is checked for its marker
before writing, and a task event records that the step is done.

- background archive: an approved requirements document's "背景信息存档" goes into the charter, its
  "用户画像观察" into knowledge/user-profile.md, and the task's domain brief into knowledge/domain-brief.md;
- iteration log: every finished task gets one line in the project's changes/iterations.md;
- shared context: an approved update is written into the kernel's knowledge/shared file.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from assistant_kernel.core import skills
from assistant_kernel.core.db import all_rows, event
from assistant_kernel.core.machine import notify
from assistant_kernel.storage import ProjectRuntimeManager

log = logging.getLogger(__name__)
ARCHIVE = "背景信息存档"
PROFILE_SECTION = "用户画像观察"
PROFILE_HEADER = ("# 用户画像\n\n每位用户一节。Kernel 在需求文档获批时追加其中的\"用户画像观察\"；周度复盘可以提议整理。"
                  "标 [推断] 的内容未经用户确认，用户的纠正优先。\n")
EMPTY = {"", "无", "无。", "（无）", "暂无"}
STATE_NAMES = {"completed": "完成", "cancelled": "取消", "failed": "失败"}


def _vault(settings, project_id: str) -> Path:
    return ProjectRuntimeManager(settings.data_root).ensure(project_id).vault_root


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


async def sync_vaults(kernel, settings) -> dict[str, int]:
    done = {}
    for name, step in (("archived", _archive_backgrounds), ("logged", _log_iterations), ("shared", _apply_shared)):
        try:
            done[name] = await step(kernel, settings)
        except Exception:   # one broken file must not stop the dispatcher
            log.exception("vault sync step %s failed", name)
            done[name] = 0
    return done


# ------------------------------------------------------------------ background archive → charter

def append_archive(charter: Path, marker: str, heading: str, body: str) -> bool:
    """Add one dated entry to the charter's archive section; False when it is already there."""
    text = charter.read_text("utf-8")
    if marker in text:
        return False
    current = skills.section_text(text, ARCHIVE)
    kept = "\n".join(line for line in current.splitlines() if not line.startswith("（需求文档获批时"))
    entry = f"### {heading} <!-- {marker} -->\n\n{body.strip()}"
    charter.write_text(skills.replace_section(text, "9 " + ARCHIVE, (kept + "\n\n" + entry).strip()), "utf-8",
                       newline="\n")
    return True


def append_profile(path: Path, user: str, marker: str, heading: str, body: str) -> bool:
    """Add one dated observation to the user's section of knowledge/user-profile.md; False when already there."""
    text = path.read_text("utf-8") if path.is_file() else PROFILE_HEADER
    if marker in text:
        return False
    section = f"用户 {user}"
    entry = f"### {heading} <!-- {marker} -->\n\n{body.strip()}"
    text = skills.replace_section(text, section, (skills.section_text(text, section) + "\n\n" + entry).strip())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, "utf-8", newline="\n")
    return True


def write_brief(path: Path, marker: str, heading: str, body: str) -> bool:
    """Replace knowledge/domain-brief.md with the newly approved research; the previous one moves to knowledge/archive/."""
    if path.is_file():
        old = path.read_text("utf-8")
        if marker in old:
            return False
        archive = path.parent / "archive" / f"domain-brief-{hashlib.sha256(old.encode()).hexdigest()[:12]}.md"
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_text(old, "utf-8", newline="\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# 行业速写\n\n> {heading} <!-- {marker} -->\n\n{body.strip()}\n", "utf-8", newline="\n")
    return True


async def _archive_backgrounds(kernel, settings) -> int:
    """On requirements approval: background → charter, user-profile observations → user-profile.md,
    the task's domain brief → knowledge/domain-brief.md."""
    from assistant_kernel.core.executors.common import read_document
    async with kernel.db.tx() as c:
        rows = await all_rows(c, """
            SELECT t.task_id, t.project_id, t.goal, t.documents->'task_contract' AS contract,
                   t.documents->'domain_brief' AS brief, m.user_id
            FROM core.tasks t LEFT JOIN core.messages m ON m.message_id = t.source_message_id
            WHERE t.approvals ? 'requirements'
              AND t.documents->'task_contract'->>'sha256' = t.approvals->'requirements'->>'doc_sha'
              AND NOT EXISTS (SELECT 1 FROM core.task_events e WHERE e.task_id = t.task_id
                              AND e.kind = 'background_archived'
                              AND e.payload->>'doc_sha' = t.documents->'task_contract'->>'sha256')""")
    count = 0
    for row in rows:
        sha = row["contract"]["sha256"]
        markdown = await asyncio.to_thread(read_document, settings, row["project_id"], row["contract"])
        body = skills.section_text(markdown, ARCHIVE)
        knowledge = _vault(settings, row["project_id"]) / "knowledge"
        charter = knowledge / "charter.md"
        heading = f"{_today()} · {row['goal'][:40]}（任务 {str(row['task_id'])[:8]}）"
        written = False
        if body.strip() not in EMPTY and charter.is_file():
            written = await asyncio.to_thread(append_archive, charter, f"archive:{sha[:12]}", heading, body)
        observed = skills.section_text(markdown, PROFILE_SECTION)
        profiled = False
        if observed.strip() not in EMPTY:
            profiled = await asyncio.to_thread(append_profile, knowledge / "user-profile.md", row["user_id"] or "默认",
                                               f"profile:{sha[:12]}", heading, observed)
        briefed = False
        if row["brief"]:
            research = await asyncio.to_thread(read_document, settings, row["project_id"], row["brief"])
            if research.strip():
                briefed = await asyncio.to_thread(write_brief, knowledge / "domain-brief.md",
                                                  f"brief:{row['brief']['sha256'][:12]}", f"{heading} 调研，随需求批准存档",
                                                  research)
        async with kernel.db.tx() as c:
            await event(c, row["project_id"], "background_archived", task_id=row["task_id"], doc_sha=sha,
                        written=written, empty=body.strip() in EMPTY, profiled=profiled, briefed=briefed)
        count += written
    return count


# ------------------------------------------------------------------ iteration log

def append_iteration(path: Path, marker: str, line: str) -> bool:
    if path.is_file() and marker in path.read_text("utf-8"):
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.is_file():
        path.write_text("# 迭代记录\n\n每个结束的任务一行（Kernel 自动写入）：日期 · 结果 · 来源 · 目标 · 原因。\n\n", "utf-8",
                        newline="\n")
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(f"{line} <!-- {marker} -->\n")
    return True


async def _log_iterations(kernel, settings) -> int:
    async with kernel.db.tx() as c:
        rows = await all_rows(c, """
            SELECT task_id, project_id, kind, origin, state, close_reason, goal, reason, closed_at
            FROM core.tasks t WHERE closed_at IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM core.task_events e WHERE e.task_id = t.task_id
                              AND e.kind = 'iteration_logged')
            ORDER BY closed_at""")
    for row in rows:
        line = (f"- {row['closed_at']:%Y-%m-%d} · {STATE_NAMES.get(row['state'], row['state'])}（{row['close_reason']}）"
                f" · {row['origin']}/{row['kind']} · {row['goal'][:100]} · 原因：{row['reason'] or '—'}")
        path = _vault(settings, row["project_id"]) / "changes" / "iterations.md"
        await asyncio.to_thread(append_iteration, path, f"task:{row['task_id']}", line)
        async with kernel.db.tx() as c:
            await event(c, row["project_id"], "iteration_logged", task_id=row["task_id"])
    return len(rows)


# ------------------------------------------------------------------ shared context

def write_shared(data_root: Path, update: dict[str, Any]) -> None:
    path = skills.shared_path(data_root, update["file"])
    text = path.read_text("utf-8") if path.is_file() else f"# {update['file']}\n"
    if skills.section_text(text, update["section"]) != update["content"].strip():
        text = skills.replace_section(text, update["section"], update["content"])
        changes = skills.section_text(text, "变更记录")
        record = f"- {_today()} {update['section']}：{update['reason'] or '（未说明原因）'}（用户批准）"
        text = skills.replace_section(text, "变更记录", (changes + "\n" + record).strip())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, "utf-8", newline="\n")


async def _apply_shared(kernel, settings) -> int:
    async with kernel.db.tx() as c:
        rows = await all_rows(c, """
            SELECT a.project_id, a.payload FROM core.task_events a
            WHERE a.kind = 'shared_update_approved'
              AND NOT EXISTS (SELECT 1 FROM core.task_events b WHERE b.kind = 'shared_update_applied'
                              AND b.payload->>'item_id' = a.payload->>'item_id')
            ORDER BY a.event_id""")
    for row in rows:
        update = row["payload"]
        await asyncio.to_thread(write_shared, settings.data_root, update)
        async with kernel.db.tx() as c:
            await event(c, row["project_id"], "shared_update_applied", item_id=update["item_id"])
            await notify(c, row["project_id"], "supervisor",
                         f"✅ 公共背景已更新：{update['file']} · {update['section']}（各项目从下一轮起使用新内容）")
    return len(rows)
