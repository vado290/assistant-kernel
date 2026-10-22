"""Kernel's own file writes that follow from database state (docs/ROUND2_PLAN.md, R1).

The state machine only changes the database. Whatever must also land in a vault file is written here,
on the dispatcher's maintenance tick. Every step is idempotent: the file is checked for its marker
before writing, and a task event records that the step is done.

- background archive: an approved requirements document's "背景信息存档" goes into knowledge/background.md (kept
  apart from the charter since 0.45.6, so a charter holds only the project's definition and can be public), its
  "用户画像观察" into knowledge/user-profile.md, and the task's domain brief into knowledge/domain-brief.md;
  a task that ends without approval still gives its "用户画像观察" (marked as such; its facts were never confirmed);
- profile observations Supervisor noted in a consultation or a card review go into knowledge/user-profile.md;
- each such write is committed on its own, naming its source (system map memory review, 0.45.5);
- (the iteration log moved to the core.iterations table in ROUND4; changes/iterations.md is no longer written)
- shared context: an approved update is written into the kernel's knowledge/shared file; a missing shared file
  starts from the template (a new installation of the public kernel project).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from assistant_kernel.core import projectgit, rules, skills
from assistant_kernel.core.db import all_rows, event
from assistant_kernel.core.machine import notify
from assistant_kernel.storage import ProjectRuntimeManager

log = logging.getLogger(__name__)
ARCHIVE = "背景信息存档"
BACKGROUND = "background.md"
BACKGROUND_HEADER = ("# 背景信息存档\n\n本项目积累的组织、行业与项目事实。Kernel 在需求文档获批时追加其\"背景信息存档\"一节，"
                     "每条注明日期和任务；读的时候以最新条目为准。\n")
PROFILE_SECTION = "用户画像观察"
PROFILE_HEADER = ("# 用户画像\n\n每位用户一节。Kernel 追加需求文档的\"用户画像观察\"（需求获批时；任务未获批就结束时也追加并注明），"
                  "以及 Supervisor 在对话中记下的用户偏好和纠正；周度复盘可以提议整理。"
                  "标 [推断] 的内容未经用户确认；同一维度以最新一条为准，用户的纠正优先。\n")
EMPTY = {"", "无", "无。", "（无）", "暂无"}
STATE_NAMES = {"completed": "完成", "cancelled": "取消", "failed": "失败"}


def _vault(settings, project_id: str) -> Path:
    return ProjectRuntimeManager(settings.data_root).ensure(project_id).vault_root


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


async def sync_vaults(kernel, settings) -> dict[str, int]:
    done = {}
    for name, step in (("archived", _archive_backgrounds), ("unapproved", _archive_unapproved),
                       ("profile", _apply_profile_observations), ("seed_shared", _seed_shared), ("shared", _apply_shared),
                       ("worktrees", _remove_worktrees), ("templates", _sync_templates), ("imported", _import_commits),
                       ("push", _push_repositories)):
        try:
            done[name] = await step(kernel, settings)
        except Exception:   # one broken file must not stop the dispatcher
            log.exception("vault sync step %s failed", name)
            done[name] = 0
    return done


def _commit(vault: Path, paths: list[str], message: str) -> None:
    """Commit just these memory files (best effort: a git problem is logged, the write itself stands)."""
    present = [p for p in paths if (vault / p).exists()]
    if not present:
        return
    try:
        projectgit.commit_paths(vault, present, message)
    except (projectgit.GitError, OSError) as exc:
        log.warning("commit of %s in %s failed: %s", present, vault, exc)


MEMORY_FILES = ["knowledge/background.md", "knowledge/user-profile.md", "knowledge/domain-brief.md",
                "knowledge/archive"]


# ------------------------------------------------------------------ background archive → knowledge/background.md

def append_archive(path: Path, marker: str, heading: str, body: str) -> bool:
    """Add one dated entry to knowledge/background.md (created on first use); False when it is already there."""
    text = path.read_text("utf-8") if path.is_file() else BACKGROUND_HEADER
    if marker in text:
        return False
    entry = f"### {heading} <!-- {marker} -->\n\n{body.strip()}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.rstrip() + "\n\n" + entry + "\n", "utf-8", newline="\n")
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
    """On requirements approval: background → knowledge/background.md, user-profile observations → user-profile.md,
    the task's domain brief → knowledge/domain-brief.md."""
    from assistant_kernel.core.executors.common import read_document
    async with kernel.db.tx() as c:
        rows = await all_rows(c, """
            SELECT t.task_id, t.project_id, t.goal, t.documents->'task_contract' AS contract,
                   t.documents->'domain_brief' AS brief, m.user_id
            FROM core.tasks t LEFT JOIN core.messages m ON m.message_id = t.source_message_id
            WHERE t.approvals ? 'requirements' AND t.kind <> 'bootstrap'   -- a charter goes to the new project instead
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
        heading = f"{_today()} · {row['goal'][:40]}（任务 {str(row['task_id'])[:8]}）"
        written = False
        if body.strip() not in EMPTY:
            written = await asyncio.to_thread(append_archive, knowledge / BACKGROUND, f"archive:{sha[:12]}", heading,
                                              body)
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
        if written or profiled or briefed:
            await asyncio.to_thread(_commit, _vault(settings, row["project_id"]), MEMORY_FILES,
                                    f"存档：需求获批的背景、画像与行业速写（任务 {str(row['task_id'])[:8]}）")
        async with kernel.db.tx() as c:
            await event(c, row["project_id"], "background_archived", task_id=row["task_id"], doc_sha=sha,
                        written=written, empty=body.strip() in EMPTY, profiled=profiled, briefed=briefed)
        count += written
    return count


async def _archive_unapproved(kernel, settings) -> int:
    """A delivery task that ended without its requirements approved: its "用户画像观察" still says how the user works
    with us, so it goes into the profile, marked as unapproved; its background facts were never confirmed and stay out."""
    from assistant_kernel.core.executors.common import read_document
    async with kernel.db.tx() as c:
        rows = await all_rows(c, """
            SELECT t.task_id, t.project_id, t.goal, t.documents->'task_contract' AS contract, m.user_id
            FROM core.tasks t LEFT JOIN core.messages m ON m.message_id = t.source_message_id
            WHERE t.state = ANY(%s) AND t.kind = 'delivery' AND t.documents ? 'task_contract'
              AND coalesce(t.approvals->'requirements'->>'doc_sha', '') <> t.documents->'task_contract'->>'sha256'
              AND NOT EXISTS (SELECT 1 FROM core.task_events e WHERE e.task_id = t.task_id
                              AND e.kind = 'background_archived'
                              AND e.payload->>'doc_sha' = t.documents->'task_contract'->>'sha256')""",
                              (list(rules.TERMINAL_STATES),))
    count = 0
    for row in rows:
        sha = row["contract"]["sha256"]
        markdown = await asyncio.to_thread(read_document, settings, row["project_id"], row["contract"])
        observed = skills.section_text(markdown, PROFILE_SECTION)
        profiled = False
        if observed.strip() not in EMPTY:
            vault = _vault(settings, row["project_id"])
            heading = f"{_today()} · {row['goal'][:40]}（任务 {str(row['task_id'])[:8]}，需求未获批，仅协作观察）"
            profiled = await asyncio.to_thread(append_profile, vault / "knowledge" / "user-profile.md",
                                               row["user_id"] or "默认", f"profile:{sha[:12]}", heading, observed)
            if profiled:
                await asyncio.to_thread(_commit, vault, ["knowledge/user-profile.md"],
                                        f"存档：未获批任务的协作观察（任务 {str(row['task_id'])[:8]}）")
        async with kernel.db.tx() as c:
            await event(c, row["project_id"], "background_archived", task_id=row["task_id"], doc_sha=sha,
                        unapproved=True, written=False, profiled=profiled, briefed=False)
        count += profiled
    return count


# ------------------------------------------------------------------ Supervisor's profile observations

async def _apply_profile_observations(kernel, settings) -> int:
    """Observations Supervisor noted (machine._record_profile) → the user's section of knowledge/user-profile.md."""
    async with kernel.db.tx() as c:
        rows = await all_rows(c, """
            SELECT a.project_id, a.payload FROM core.task_events a
            WHERE a.kind = 'profile_observed'
              AND NOT EXISTS (SELECT 1 FROM core.task_events b WHERE b.kind = 'profile_observation_applied'
                              AND b.payload->>'source_run' = a.payload->>'source_run')
            ORDER BY a.event_id""")
    for row in rows:
        payload, vault = row["payload"], _vault(settings, row["project_id"])
        run = str(payload["source_run"])
        body = "\n".join(f"- {item} · [Supervisor·对话]" for item in payload.get("items") or [])
        added = await asyncio.to_thread(append_profile, vault / "knowledge" / "user-profile.md",
                                        payload.get("user_id") or "默认", f"observed:{run[:12]}",
                                        f"{_today()} · Supervisor 在对话中记下", body)
        if added:
            await asyncio.to_thread(_commit, vault, ["knowledge/user-profile.md"],
                                    f"存档：Supervisor 记下的用户偏好或纠正（运行 {run[:8]}）")
        async with kernel.db.tx() as c:
            await event(c, row["project_id"], "profile_observation_applied", source_run=run)
    return len(rows)


# ------------------------------------------------------------------ shared context

def write_shared(data_root: Path, update: dict[str, Any]) -> None:
    path = skills.shared_path(data_root, update["file"])
    text = path.read_text("utf-8") if path.is_file() else f"# {update['file']}\n"
    if skills.section_text(text, update["section"]) != update["content"].strip():
        # the reason is recorded in the kernel project's change stream at approval (ROUND4), not in the file
        text = skills.replace_section(text, update["section"], update["content"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, "utf-8", newline="\n")


SHARED_TEMPLATE = Path(__file__).parent / "shared_template"


def seed_shared(data_root: Path) -> list[str]:
    """Shared files the kernel project lacks, copied from the template; existing files are never touched.
    The shared files are personal to an installation, so the public kernel project does not carry them."""
    if not (data_root / "vaults" / skills.SHARED_CONTEXT_HOME).is_dir():
        return []
    added = []
    for name in skills.SHARED_FILES:
        target, source = skills.shared_path(data_root, name), SHARED_TEMPLATE / name
        if not target.exists() and source.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(source.read_text("utf-8"), "utf-8", newline="\n")
            added.append(name)
    return added


async def _seed_shared(kernel, settings) -> int:
    return len(await asyncio.to_thread(seed_shared, settings.data_root))


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
        await asyncio.to_thread(_commit, _vault(settings, skills.SHARED_CONTEXT_HOME),
                                [f"knowledge/shared/{update['file']}"],
                                f"公共背景：{update['file']} · {update['section']}（用户批准）")
        async with kernel.db.tx() as c:
            await event(c, row["project_id"], "shared_update_applied", item_id=update["item_id"])
            await notify(c, row["project_id"], "supervisor",
                         f"✅ 公共背景已更新：{update['file']} · {update['section']}（各项目从下一轮起使用新内容）")
    return len(rows)


# ------------------------------------------------------------------ code delivery: closed tasks give their worktree back

async def _remove_worktrees(kernel, settings) -> int:
    from assistant_kernel.core import gitdelivery
    async with kernel.db.tx() as c:
        rows = await all_rows(c, """
            SELECT task_id, project_id FROM core.tasks t WHERE closed_at IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM core.task_events e WHERE e.task_id = t.task_id
                              AND e.kind = 'worktree_released')""")
    removed = 0
    for row in rows:
        error = None
        try:   # a worktree that cannot be removed is recorded once and left alone; it never blocks the others
            code = await asyncio.to_thread(gitdelivery.config, settings, row["project_id"])
            gone = bool(code) and await asyncio.to_thread(gitdelivery.remove_worktree, code, str(row["task_id"]))
        except Exception as exc:
            log.warning("removing the worktree of task %s failed: %s", row["task_id"], exc)
            gone, error = False, str(exc)[:500]
        async with kernel.db.tx() as c:
            await event(c, row["project_id"], "worktree_released", task_id=row["task_id"], removed=gone, error=error)
        removed += gone
    return removed


# ------------------------------------------------------------------ project history → GitHub (ROUND5 D13)

async def _push_repositories(kernel, settings) -> int:
    """Push committed history of each project folder and its writable external folders to their GitHub
    repositories. A failure (e.g. no `gh auth login` yet) is said once a day and retried at most hourly.
    A project with settings `git_push: false` (a private one, e.g. trading) is never pushed; it still commits."""
    import subprocess
    from assistant_kernel.core import projectgit
    from assistant_kernel.core.db import one
    pushed = 0
    for project_id in await kernel.active_projects():
        async with kernel.db.tx() as c:
            setting = await one(c, "SELECT settings->>'git_push' AS push FROM core.projects WHERE project_id=%s",
                                (project_id,))
        if (setting or {}).get("push") == "false":
            continue
        async with kernel.db.tx() as c:
            recent = await one(c, """SELECT bool_or(created_at > now() - interval '1 hour') AS waiting,
                                            bool_or((payload->>'notified')::boolean) AS told
                                     FROM core.task_events WHERE project_id=%s AND kind='git_push_failed'
                                       AND created_at > now() - interval '1 day'""", (project_id,))
        if recent["waiting"]:
            continue
        grants = await kernel.grants(project_id)
        for path, name in projectgit.repositories(_vault(settings, project_id), project_id, grants):
            if not await asyncio.to_thread(projectgit.pending_push, path):
                continue
            try:
                await asyncio.to_thread(projectgit.push, path, name)
                pushed += 1
            except (projectgit.GitError, OSError, subprocess.TimeoutExpired) as exc:
                tell = not recent["told"]
                async with kernel.db.tx() as c:
                    await event(c, project_id, "git_push_failed", path=str(path), error=str(exc)[:500], notified=tell)
                    if tell:
                        await notify(c, project_id, "supervisor", f"⚠ 推送到 GitHub 失败（本地提交不受影响，每小时重试）："
                                     f"{str(exc)[:400]}")
                break
    return pushed


# ------------------------------------------------------------------ commits made outside tasks → change stream

IMPORT_DAYS, IMPORT_LIMIT, IMPORT_EVERY = 30, 100, 600   # look-back, commits per repository and tick, seconds
_last_import = 0.0


def _commits(path: Path, days: int, limit: int) -> list[dict[str, Any]]:
    """The repository's commits of the last `days`, oldest first: sha, author, ISO date, subject, body, files."""
    from assistant_kernel.core import projectgit
    out = projectgit.git("log", f"--since={days} days ago", f"-n{limit}", "--reverse", "--name-only",
                         "--format=%x01%H%x02%an%x02%cI%x02%s%x02%b%x03", cwd=path)
    found = []
    for block in out.split("\x01")[1:]:   # separators that str.strip() (in projectgit.git) leaves alone
        head, _, names = block.partition("\x03")
        sha, author, date, subject, body = (head.split("\x02") + [""] * 5)[:5]
        found.append({"sha": sha, "author": author, "date": date, "subject": subject.strip(), "body": body.strip(),
                      "files": [f for f in names.splitlines() if f.strip()]})
    return found


def _repositories(settings, project_id: str, grants: list[dict]) -> list[tuple[Path, str, str]]:
    """(folder, label, file scheme) of every repository whose history belongs to the project's change stream."""
    from assistant_kernel.core import gitdelivery, projectgit
    vault = _vault(settings, project_id)
    found = [(vault, "project", "workspace://project/")]
    found += [(path, name[len(project_id) + 1:], f"grant://{name[len(project_id) + 1:]}/")
              for path, name in projectgit.repositories(vault, project_id, grants)[1:]]
    code = gitdelivery.config(settings, project_id)
    if code:
        found.append((Path(code["repo"]), "code", f"git://{code['main']}/"))
    return [item for item in found if projectgit.is_repo(item[0])]


async def _import_commits(kernel, settings) -> int:
    """Changes made outside tasks (by hand, by other tools, by Kernel's own upkeep) are part of what changed in a
    project: each such commit becomes an `imported` row of the change stream, dated by the commit (system map M1).
    A task's release is recorded by the task itself, so release commits (and, in a code repository, every commit
    Kernel made for a task) are left out. At most every IMPORT_EVERY seconds; a commit is imported once."""
    global _last_import
    from assistant_kernel.core import gitdelivery, machine, projectgit
    now = asyncio.get_running_loop().time()
    if _last_import and now - _last_import < IMPORT_EVERY:
        return 0
    _last_import = now
    kernel_names = {identity[1].split("=", 1)[1] for identity in (projectgit.IDENTITY, gitdelivery.IDENTITY)}
    imported = 0
    for project_id in await kernel.active_projects():
        grants = await kernel.grants(project_id)
        for path, label, scheme in await asyncio.to_thread(_repositories, settings, project_id, grants):
            try:
                commits = await asyncio.to_thread(_commits, path, IMPORT_DAYS, IMPORT_LIMIT)
            except Exception as exc:   # one unreadable repository must not stop the others
                log.warning("reading the history of %s failed: %s", path, exc)
                continue
            by_kernel = lambda c: c["author"] in kernel_names  # noqa: E731
            wanted = [c for c in commits if not (by_kernel(c) and (label == "code" or c["subject"].startswith("发布：")))]
            if not wanted:
                continue
            refs = {f"git:{label}:{c['sha']}": c for c in wanted}
            async with kernel.db.tx() as c:
                rows = await all_rows(c, "SELECT source_ref FROM core.iterations WHERE project_id=%s AND source_ref = ANY(%s)",
                                      (project_id, list(refs)))
                for ref in [r for r in refs if r not in {row["source_ref"] for row in rows}]:
                    commit = refs[ref]
                    files = [scheme + f for f in commit["files"]]
                    row = await machine.insert_iteration(
                        c, project_id, None, kind="imported", source="system" if by_kernel(commit) else "manual",
                        source_ref=ref, title=commit["subject"] or f"提交 {commit['sha'][:8]}",
                        reason=f"任务之外的提交（{label} {commit['sha'][:8]}，{commit['author']}）："
                               + (commit["body"] or commit["subject"] or "（提交说明为空）")[:1200],
                        changes=[{"type": "file", "target": f, "action": "modify", "note": ""} for f in commit["files"][:30]],
                        outcome="已提交（导入）", facts={"files": files[:200], "commit": commit["sha"], "repo": label,
                                                    "author": commit["author"]}, created_at=commit["date"])
                    imported += bool(row)
    return imported


# ------------------------------------------------------------------ template skills → each project (ROUND5 R5.2 fix)

async def _sync_templates(kernel, settings) -> int:
    """Once per Kernel version (ROUND5 D24): give every project the template skills new to it and refresh the copies
    it never edited (skills.sync_templates), committing just the agents' folders. Edited copies stay for the weekly
    review. Then the operator-approved adoption batches (skill_updates; nothing happens without a policy)."""
    from assistant_kernel import __version__
    from assistant_kernel.core import projectgit
    from assistant_kernel.core.skill_updates import WRITING, list_updates, maintain
    changed = 0
    for project_id in await kernel.active_projects():
        vault = _vault(settings, project_id)
        if (skills.offered(vault) or {}).get("kernel_version") == __version__:
            continue
        if any(row["status"] in WRITING for row in await list_updates(kernel, project_id, 100)):
            continue   # an adoption batch is writing this project's skills: next tick
        result = await asyncio.to_thread(skills.sync_templates, vault, skills.project_config(vault)["capabilities"],
                                         __version__)
        if result["installed"] or result["refreshed"]:
            changed += 1
            try:
                await asyncio.to_thread(projectgit.commit_paths, vault, ["agents"],
                                        f"同步技能模板（Kernel {__version__}）")
            except (projectgit.GitError, OSError) as exc:
                log.warning("committing synced skills of %s failed: %s", project_id, exc)
        async with kernel.db.tx() as c:
            await event(c, project_id, "skills_synced", kernel_version=__version__, **result)
    return changed + await maintain(kernel)

