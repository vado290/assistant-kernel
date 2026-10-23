"""Render a project digest (Kernel.project_digest) as Markdown evidence for an evolution review (model-facing, English)."""
from __future__ import annotations

import json
from typing import Any

from assistant_kernel.core import i18n

RECENT_LIMIT, MAJOR_LIMIT = 30, 20   # hard caps on the change stream in a review (ROUND4)
STAGE_LABELS = {"requirements": "requirements", "design": "design", "build": "build", "review": "review",
                "operate": "operation"}
SHARED_CANDIDATE_LIMIT = 20
SHARED_CANDIDATES_HEADING = ("### Facts other projects archived as \"shared\" that the shared background does not hold yet "
                             "(candidates; the user decides which are worth adopting — propose them one section at a "
                             "time with propose_shared_update)\n\n")


def shared_candidates(settings, project_ids: list[str], *, exclude: str) -> str:
    """Facts other projects archived as shared ("Shared:", "公共：", … — true beyond that project) that the shared
    files do not hold yet. A plain text check against the shared files: good enough for a person to decide on."""
    from assistant_kernel.core import skills
    from assistant_kernel.core.executors.common import charter
    from assistant_kernel.core.vault_sync import ARCHIVE
    shared = "\n".join(s.body for s in skills.shared_context(settings.data_root))
    found = []
    for project_id in project_ids:
        if project_id == exclude:
            continue
        for line in skills.section_text(charter(settings, project_id), ARCHIVE).splitlines():
            fact = i18n.shared_fact(line.strip().lstrip("-*").strip())
            if fact and fact[:20] not in shared:
                found.append(f"- {project_id}: {fact}")
    return "\n".join(found[:SHARED_CANDIDATE_LIMIT])


async def evolution_material(kernel, settings, project_id: str) -> str:
    """Everything a weekly review reads: the project's own digest, template updates its skill copies have not taken,
    and — for the kernel (kernel_maintenance) — what the other projects share with it (ROUND2 R4b)."""
    from assistant_kernel.core import skills
    from assistant_kernel.storage import ProjectRuntimeManager
    runtime = ProjectRuntimeManager(settings.data_root)
    vault = runtime.ensure(project_id).vault_root
    from assistant_kernel.core import iterlog
    parts = [render_digest(await kernel.project_digest(project_id))]
    context = await kernel.evolution_context(project_id)
    parts.append("### Operations and follow-up entry points (bounded and read-only; a registered state is not live "
                 "health, and a cut-off list does not mean there is nothing else)\n"
                 + json.dumps(context, ensure_ascii=False, default=str)
                 + "\nTimes: observed_at is when this database read happened, not a health check; for kind=job last_at "
                   "is the time of the last run result, for kind=service the time of the last state change. A service "
                   "whose state does not change keeps its last_at, so a long stable run may show an old or empty value; "
                   "that alone does not mean a check is overdue or the service is broken. Without live health evidence "
                   "say it is not verified, judge from the logs and receipts you have, and never invent a check time."
                 + "\nRun tasks can be read in their generated runs/<task_id>/state.md; for queued tasks use the state and "
                   "reason above; read earlier reviews by proposal_ref. First check for duplicate proposals, overlaps "
                   "and continuation points. If the list is cut off, verify specific items from the map or configuration "
                   "and name the states you could not get; never claim full coverage.")
    parts.append("Run versions list at most the latest 12 successful runs with skills/config_sha256: skill labels include "
                 "skills discoverable on demand and do not prove a skill was read in full or followed; config "
                 "fingerprints do not keep old role texts — mark a missing reliable baseline as unknown.")
    from assistant_kernel.core.skill_updates import list_updates
    adoption = await list_updates(kernel, project_id)
    if adoption:
        parts.append("### Skill adoption records (files applied does not mean loaded or effective; blocked items go to "
                     "follow-up tasks, citing update_id)\n" + json.dumps(adoption, ensure_ascii=False, default=str))
    baseline = await kernel.operational_baseline(project_id)
    if baseline:
        released = await kernel.get_task(baseline["task_id"])
        refs = {k: v.get("ref") for k, v in (released or {}).get("documents", {}).items()
                if k in ("task_contract", "solution_design", "implementation_report", "review_report")}
        parts.append("### Released change entry point (the latest increment, not all operating requirements; verify by "
                     "ref with read_project_document)\n"
                     + json.dumps({**baseline, "document_refs": refs}, ensure_ascii=False, default=str))
    else:
        parts.append("### Release basis\nNo Kernel release baseline yet; verify the actual products and any manual or "
                     "external release evidence — never invent an approval or conclude there is nothing in operation.")
    recent = await kernel.iterations(project_id, days=28, limit=RECENT_LIMIT + 1)
    parts.append("### Change stream of the last 4 weeks (bounded; record ids are used for the effect review)\n"
                 + iterlog.render(recent[:RECENT_LIMIT])
                 + ("\nMore records are not shown; query them with list_iterations." if len(recent) > RECENT_LIMIT else ""))
    recent = recent[:RECENT_LIMIT]
    for label, filters in (("Waiting for an effect review (including records older than the default window; pending "
                            "does not count as closed)", {"pending_only": True}),
                           ("Expected effect without a way to measure it (gather evidence; never invent historical "
                            "metrics)", {"needs_measure": True})):
        page = await kernel.iteration_page(project_id, **filters, oldest_first=True, limit=RECENT_LIMIT)
        parts.append("### " + label + "\n" + iterlog.render(page["items"])
                     + "\nPaging: " + json.dumps({"filters": {**filters, "oldest_first": True},
                         "as_of": page["as_of"], "truncated": page["truncated"], "next_cursor": page["next_cursor"]})
                     + "\nGive a verdict or a deferral for each item included this round; write a continuation point for "
                       "the items not covered — [] does not prove there is nothing to do. States can change; re-check "
                       "next round.")
    shown = {r["iteration_id"] for r in recent}
    majors = [r for r in await kernel.iterations(project_id, days=180, major_only=True, limit=MAJOR_LIMIT + len(shown))
              if r["iteration_id"] not in shown][:MAJOR_LIMIT]
    if majors:
        parts.append("### Earlier major changes within 6 months (one line each; details with list_iterations)\n"
                     + iterlog.render(majors, brief=True))
    gaps = skills.collaboration_gaps(vault)
    if gaps:
        parts.append("### Broken skill hand-offs (checked by each skill's io metadata; hand-offs often fail here)\n"
                     + "\n".join(f"- {g}" for g in gaps))
    updates = skills.template_updates(vault)
    if updates:
        parts.append("### Shared templates were updated (this project's copies have not followed; whether and what to "
                     "adopt is for you to propose)\n" + "\n".join(
            f"- {u['name']}: project {u['copy']}, template {u['template']}. Latest template changes: {u['changes']}"
            for u in updates))
    if "kernel_maintenance" in skills.project_config(vault)["capabilities"]:
        lines = []
        for other in await kernel.active_projects():
            if other == project_id:
                continue
            share = skills.project_config(runtime.ensure(other).vault_root)["share_with_kernel"]
            lines.append(summarize_project(other, share, await kernel.project_digest(other)))
        if lines:
            parts.append("### How the other projects are doing (by each project's sharing level; use it to find "
                         "problems several projects share and improve the shared templates)\n" + "\n".join(lines))
        candidates = shared_candidates(settings, await kernel.active_projects(), exclude=project_id)
        if candidates:
            parts.append(SHARED_CANDIDATES_HEADING + candidates)
    return "\n\n".join(parts)


def summarize_project(project_id: str, share: str, digest: dict[str, Any]) -> str:
    tasks = digest["tasks"]
    done = sum(1 for t in tasks if t["state"] == "completed")
    stopped = sum(1 for t in tasks if t["state"] == "cancelled")
    actions = digest["card_actions"]
    revised = sum(1 for a in actions if a.get("action") == "revise")
    rejected = sum(1 for a in actions if a.get("action") in ("reject", "stop"))
    failed = sum(f["n"] for f in digest["failures"])
    jobs_failed = sum(j["failed"] for j in digest.get("jobs") or [])
    line = (f"- {project_id} ({share}): {len(tasks)} tasks ({done} completed, {stopped} stopped), "
            f"{digest['user_messages']} user messages, {revised} card changes, {rejected} declined or stopped, "
            f"{failed} failed or retried runs, {jobs_failed} job failures")
    if share in ("improvements", "full"):
        goals = [t["goal"][:60] for t in tasks if t.get("kind") == "delivery"][:5]
        if goals:
            line += "; latest tasks: " + "; ".join(goals)
    if share == "full":
        texts = [str(a.get("text"))[:80] for a in actions if a.get("text")][:3]
        if texts:
            line += "; the user's change requests: " + "; ".join(texts)
    return line


def render_digest(digest: dict[str, Any]) -> str:
    lines = [f"### Run record of the last {digest['days']} days (Kernel statistics; these are the facts)",
             f"- {digest['user_messages']} user messages; {len(digest['tasks'])} tasks"]
    for t in digest["tasks"]:
        lines.append(f"  - [{t['state']}/{t['stage']}] {t['goal']} ({t['turn_count']} PM turns"
                     + (f", closed because {t['close_reason']}" if t["close_reason"] else "") + ")")
        if t.get("task_id"):
            lines.append(f"    ledger: runs/{t['task_id']}/state.md")
    actions = digest["card_actions"]
    revises = [a for a in actions if a.get("action") == "revise" and a.get("text")]
    negative = [a for a in actions if a.get("action") in ("stop", "reject")]
    unsure = [a for a in actions if i18n.sounds_unsure(str(a.get("answers") or ""))]
    lines.append(f"- {len(actions)} card actions: {len(revises)} changes, {len(negative)} declined or stopped, "
                 f"{len(unsure)} answers saying the user is not sure")
    for a in revises[:10]:
        lines.append(f"  - The user's change request: \"{a['text'][:200]}\" (on: {a.get('message', '')[:100]})")
    for a in negative[:10]:
        lines.append(f"  - The user {('stopped' if a['action'] == 'stop' else 'declined')}: {a.get('message', '')[:150]}")
    for a in unsure[:5]:
        lines.append(f"  - A question the user found hard to answer: {a.get('message', '')[:150]} → {str(a.get('answers'))[:150]}")
    if len(revises) > 10 or len(negative) > 10 or len(unsure) > 5:
        lines.append("- The card examples are cut off and do not represent all feedback; read the relevant task "
                     "ledgers, and page through messages with list_recent_history.")
    counts = digest["event_counts"]
    lines.append(f"- Cards rewritten because of new input: {counts.get('card_upgraded', 0)}; Supervisor re-evaluations "
                 f"after new messages: {counts.get('supervisor_reevaluate', 0)}; discarded stale results: "
                 f"{counts.get('stale_result', 0)}")
    per_task: dict[str, list[str]] = {}
    for row in digest.get("stage_turns") or []:
        per_task.setdefault(row["goal"], []).append(f"{STAGE_LABELS.get(row['stage'], row['stage'])} {row['n']}")
    if per_task:
        lines.append("- PM turns per stage of each task:")
        lines += [f"  - {goal}: {', '.join(parts)}" for goal, parts in per_task.items()]
    for job in digest.get("jobs") or []:
        lines.append(f"- Scheduled job {job['job_id']}: {job['ok']} succeeded, {job['failed']} failed"
                     + (f" (e.g. {str(job['example'])[:150]})" if job["failed"] and job["example"] else ""))
    delegations = digest.get("delegations") or []
    lines.append("- Subagents the PM reported using: " + (", ".join(f"{name} × {n}" for name, n in delegations)
                                                          if delegations else
                                                          "none (when the requirements stage sent no research "
                                                          "subagent, check whether it should have)"))
    if digest["failures"]:
        lines.append("- Failed or retried runs:")
        for f in digest["failures"]:
            lines.append(f"  - {f['run_type']}:{f['purpose']} × {f['n']}, e.g.: {f['example'] or '(no error message)'}")
    else:
        lines.append("- No failed or retried runs")
    return "\n".join(lines)
