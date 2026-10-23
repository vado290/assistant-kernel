"""Supervisor executor (plan 2.7–2.9): a fresh, read-only evaluation that proposes the next card item."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from assistant_kernel.core import grants, iterlog, rules
from assistant_kernel.core.runtime import DEFAULT_TZ
from assistant_kernel.core import skills as skill_rules
from assistant_kernel.core.executors.common import (
    DOMAIN_BRIEF, PROFILE_HEADING, READ_ONLY_TOOLS, USER_PROFILE, call_for_json, capabilities, charter, knowledge_text, load_agent,
    project_catalog, prompt_text, read_document, render_documents,
)
from assistant_kernel.core.executors.digest import SHARED_CANDIDATES_HEADING, shared_candidates

STAGE_ORDER = ["requirements", "design", "build", "review", "release", "operate"]
PURPOSE_PROMPT = {"evaluate": "supervisor-evaluate", "review_input": "supervisor-review-input",
                  "consult": "supervisor-consult"}
PURPOSE_NAMES = {"evaluate": "evaluate and arrange the next step", "review_input": "a new user message while a card is open",
                 "consult": "answer the user's question"}
REASONS = {
    "start": "a new task has just started; arrange the first step",
    "pm_questions": "the PM asked questions that need to be put to the user",
    "pm_completed": "the PM finished this turn's work",
    "pm_failed": "the PM reported that this turn failed",
    "pm_return": "the PM suggests going back to an earlier stage", "answers": "the user answered your last questions",
    "revise": "the user wants your last proposed next step changed", "validation_passed": "Kernel validation passed",
    "validation_failed": "Kernel validation failed", "publish_failed": "the release failed",
    "release_rejected": "the user did not approve the release", "complete_rejected": "the user does not want the task finished",
    "cancel_rejected": "the user does not want the task cancelled",
    "create_project_rejected": "the user did not approve creating the project from the current charter",
    "bootstrap_failed": "creating the project failed",
    "followups_rejected": "the user did not approve the follow-up tasks you proposed",
    "project_task_rejected": "the user does not want to start a project-creation task",
    "grant_approved": "the user approved the directory access you proposed (effective from the next turn)",
    "grant_rejected": "the user did not approve the directory access you proposed",
}
KIND_NAMES = {"delivery": "delivery", "evolution": "weekly review (self-improvement)", "bootstrap": "project creation"}
TRACK_NAMES = {"full": "full track (requirements → design → build → review → release)",
               "quick": "quick track (requirements with a brief solution → build → release)"}


def allowed_moves(task: dict[str, Any]) -> str:
    lines = [f"Current track: {TRACK_NAMES.get(task.get('track', 'full'))}"]
    for target in sorted(rules.stage_moves(task), key=STAGE_ORDER.index):
        approve = rules.approvals_needed(task, target)
        error = rules.move_error(task, target, approving=tuple(approve))
        if error:
            lines.append(f"- {target}: not possible now ({error})")
        else:
            lines.append(f"- {target}: possible" + (f" (executing it also approves {', '.join(approve)})" if approve else ""))
    return "\n".join(lines)


def render_reason(run_input: dict[str, Any]) -> str:
    reason = run_input.get("reason", "")
    lines = [f"- Reason for this evaluation: {reason} ({REASONS.get(reason, '')})"]
    for key, label in (("summary", "PM's summary of this turn"), ("text", "the user's change request / note"),
                       ("target", "stage the PM suggests going back to"), ("error", "error"),
                       ("instruction", "the instruction you proposed last time")):
        if run_input.get(key):
            lines.append(f"- {label}: {run_input[key]}")
    if run_input.get("inferred_intent"):
        lines.append("- Your last inference (the user saw it and asked for a change):\n" + "\n".join(
            f"  - {i['text']}" for i in run_input["inferred_intent"]))
    if run_input.get("pm_questions"):
        lines.append("- The PM's questions:\n" + "\n".join(
            f"  - [{q['id']}]{' (required)' if q.get('required', True) else ''} {q['text']}" for q in run_input["pm_questions"]))
    if run_input.get("unanswered"):
        lines.append("- The user did not answer these questions and gave the thoughts above instead:\n"
                     + "\n".join(f"  - {q}" for q in run_input["unanswered"]))
    if run_input.get("answers"):
        asked = {q["id"]: q["text"] for q in run_input.get("questions") or []}
        lines.append("- The user's answers:\n" + "\n".join(f"  - {asked.get(k, k)}: {v}" for k, v in run_input["answers"].items()))
    return "\n".join(lines)


def render_messages(messages: list[dict[str, Any]]) -> str:
    who = {("inbound", "pm"): "user → PM channel", ("inbound", "supervisor"): "user → Supervisor channel",
           ("outbound", "pm"): "PM", ("outbound", "supervisor"): "Supervisor", ("outbound", "kernel"): "system"}
    lines = [f"- [{m['created_at']:%m-%d %H:%M}] {who.get((m['direction'], m['role']), m['role'])}: {m['body'].get('text')}"
             for m in messages if m["body"].get("text")]
    return "\n".join(lines) or "(none)"


def render_card(card: dict[str, Any] | None) -> str:
    if not card or not card["items"]:
        return "(no open card)"
    return "\n".join(f"- [{i['kind']}] " + json.dumps(i["content"], ensure_ascii=False)[:1500] for i in card["items"])


TASK_STATES = {"queued": "queued", "completed": "completed", "cancelled": "cancelled"}
BRIEF_LIMIT = 2000
CATALOG_HEADING = "## Project catalog (Kernel summary: each project's purpose, scope and routing from its charter)\n\n"


async def situation_line(kernel, run_input: dict[str, Any], task: dict[str, Any] | None) -> str:
    """When it is and who is speaking: Supervisor starts every run without memory (system map, 0.45.7)."""
    message_id = run_input.get("message_id") or (task or {}).get("source_message_id")
    message = await kernel.get_message(message_id) if message_id else None
    user = (message or {}).get("user_id")
    who = (f"Current user: {user} (the \"user {user}\" section of the user profile)" if user
           else "Current user: unknown (use the most relevant section of the profile)")
    return f"Current time: {datetime.now(ZoneInfo(DEFAULT_TZ)):%Y-%m-%d %H:%M} ({DEFAULT_TZ}) · {who}"


async def project_situation(kernel, settings, project_id: str, task: dict[str, Any] | None) -> str:
    """What else is going on in the project: other tasks (queued, recently ended, with their ledgers), the latest
    changes, and the industry brief's opening — material for reading what the user meant but has not said."""
    lines = ["## Project situation (Kernel summary)"]
    evolution = (task or {}).get("kind") == "evolution"
    others = await kernel.recent_tasks(project_id, exclude=(task or {}).get("task_id"),
                                      **({"include_basis": True} if evolution else {}))
    lines.append("### Other tasks (queued tasks may have no ledger yet; the basis and proposal references below come "
                 "from the current database)" if evolution else
                 "### Other tasks (queued and recently ended; read a task's ledger in the read-only sandbox for details)")
    lines += [f"- [{TASK_STATES.get(t['state'], t['state'])}] {t['goal']} ({t['kind']}"
              + (f", closed because {t['close_reason']}" if t.get("close_reason") else "")
              + f", {t['updated_at']:%m-%d}) · "
              + (f"task {t['task_id']} (queued; does not rely on state.md)" if evolution and t["state"] == "queued" else
                 f"`runs/{t['task_id']}/state.md`")
              + ("\n  basis: " + json.dumps({"reason": t.get("reason"), "parent_task_id": t.get("parent_task_id"),
                    "source_ref": t.get("source_ref"), "source_proposal_ref": t.get("source_proposal_ref")},
                    ensure_ascii=False, default=str) if evolution else "") for t in others] or ["(none)"]
    if evolution:
        lines.append("A similar goal is not a duplicate: compare the reason, the original proposal and the scope; never "
                     "merge automatically or rewrite an existing approval. Mark missing references or basis as unknown "
                     "instead of guessing ids; when an original report is missing or cut off, look it up with the read "
                     "tools you have, and without them have the PM gather the evidence in operate — never conclude "
                     "\"no duplicate\" from missing evidence.")
        seen = set()
        for t in others:
            ref = t.get("source_proposal_ref")
            if not ref:
                continue
            key = json.dumps(ref, sort_keys=True)
            if key in seen:
                continue
            if len(seen) >= 5:
                lines.append("More than 5 original reports are referenced; not all are expanded here. The rest keep "
                             "their references above — gather evidence for the relevant ones.")
                break
            seen.add(key)
            body = await asyncio.to_thread(read_document, settings, project_id, {"ref": ref})
            lines.append(f"### Original review report (parent task {t.get('parent_task_id')}; read and verified by "
                         "reference)\n"
                         + (body[:8000] if body else "(missing, failed verification or empty: not verified yet)")
                         + ("\n…(longer than 8000 characters, cut off; do not claim a complete check from this)"
                            if len(body) > 8000 else ""))
    changes = await kernel.iterations(project_id, days=180, limit=5)
    lines.append("### Latest changes (details with list_iterations)\n" + (iterlog.render(changes, brief=True) or "(none)"))
    brief = await asyncio.to_thread(knowledge_text, settings, project_id, DOMAIN_BRIEF)
    if brief:
        cut = brief if len(brief) <= BRIEF_LIMIT else brief[:BRIEF_LIMIT] + "\n…(full text in knowledge/domain-brief.md)"
        lines.append("### Industry brief (opening; mind its dates and validity)\n" + cut)
    return "\n".join(lines)


async def execute_supervisor(run: dict[str, Any], ctx) -> dict[str, Any]:
    kernel, settings = ctx.kernel, ctx.services["settings"]
    purpose, run_input = run["purpose"], run["input"]
    task_id = run["task_id"] or run_input.get("context_task_id")
    task = await kernel.get_task(task_id) if task_id else None
    project_id = run["project_id"]
    caps = await asyncio.to_thread(capabilities, settings, project_id)
    config = await asyncio.to_thread(load_agent, settings, project_id, "supervisor", purpose=purpose,
                                     task_kind=task["kind"] if task else None)
    card = await kernel.card(project_id)
    sections = [f"# Project {project_id} · Supervisor ({PURPOSE_NAMES[purpose]})",
                await situation_line(kernel, run_input, task)]
    project_charter = await asyncio.to_thread(charter, settings, project_id)
    if project_charter:
        sections.append("## Project charter\n\n" + project_charter)
    profile = await asyncio.to_thread(knowledge_text, settings, project_id, USER_PROFILE)
    if profile:
        sections.append(PROFILE_HEADING + profile)
    sections.append(await project_situation(kernel, settings, project_id, task))
    sections.append("## Your role in this project\n\n" + config.role_text)
    if config.skills_text:
        sections.append(config.skills_text)
    if task:
        sections.append(f"## Current task\n\n- Goal: {task['goal']}\n- Kind: {KIND_NAMES.get(task['kind'], task['kind'])}\n"
                        + (f"- Why it was started: {task['reason']}\n" if task.get("reason") else "")
                        + f"- Stage: {task['stage']}\n- State: {task['state']}\n"
                        f"- PM turns so far: {task['turn_count']}\n\n### Stages it can move to (computed by Kernel)\n\n"
                        f"{allowed_moves(task)}")
        sections.append("## Documents in effect\n\n" + render_documents(settings, task))
        from assistant_kernel.core.executors.pm import STAGE_NAMES
        from assistant_kernel.core.executors.state import render_state
        sections.append("## Task ledger (facts recorded by Kernel: each turn's instruction, the user's answers and "
                        "additions, card actions; never ask again what the user already answered)\n\n"
                        + render_state(task, await kernel.task_ledger(task["task_id"]), STAGE_NAMES))
    else:
        sections.append("## Current task\n\n(the project has no active task)")
    sections.append("## Recent conversation\n\n" + render_messages(await kernel.recent_messages(run["project_id"])))
    sections.append("## Current card\n\n" + render_card(card))
    if purpose == "evaluate":
        sections.append("## This evaluation\n\n" + render_reason(run_input))
    elif purpose == "review_input":
        message = await kernel.get_message(run_input["message_id"])
        sections.append(f"## The user's new message\n\n{message['body'].get('text', '')}")
    else:
        message = await kernel.get_message(run_input["message_id"])
        source = message["body"].get("handoff_from")
        title = f"Request handed over from project {source} (handoff note)" if source else \
            "What the user said in the Supervisor channel"
        sections.append(f"## {title}\n\n{message['body'].get('text', '')}")
        if "routing" in caps:
            catalog = await asyncio.to_thread(project_catalog, settings, await kernel.active_projects(),
                                              exclude=project_id)
            sections.append(CATALOG_HEADING + catalog)
    sections.append(prompt_text(PURPOSE_PROMPT[purpose]))
    if "project_management" in caps and purpose == "evaluate":
        if task and task["kind"] == "bootstrap":   # a new project must not duplicate an existing one
            catalog = await asyncio.to_thread(project_catalog, settings, await kernel.active_projects(),
                                              exclude=project_id)
            sections.append(CATALOG_HEADING + catalog)
        sections.append(prompt_text("supervisor-kernel"))
    if "shared_context" in caps and purpose == "consult":
        sections.append(prompt_text("supervisor-shared"))
        candidates = await asyncio.to_thread(shared_candidates, settings, await kernel.active_projects(),
                                             exclude=project_id)
        if candidates:
            sections.append(SHARED_CANDIDATES_HEADING + candidates)
    if "routing" in caps and purpose == "consult":
        sections.append(prompt_text("routing"))
    parsed, model_result = await call_for_json(
        ctx, run, config, "\n\n".join(sections), task_id=str(task_id) if task_id else None, stage=None,
        session_id=None, tools=READ_ONLY_TOOLS, sandbox="read-only")
    if parsed is None:
        return {"decision": "invalid_output", "raw": model_result.final_text[:2000]}
    update = parsed.get("propose_shared_update")
    if isinstance(update, dict) and "shared_context" in caps:
        # Show the user what the section says now next to the proposal.
        update["before"] = await asyncio.to_thread(skill_rules.shared_section, settings.data_root,
                                                   str(update.get("file", "")), str(update.get("section", "")))
    requested = parsed.get("directory") if parsed.get("decision") == "propose_grant" else parsed.get("propose_grant")
    checked: dict[str, Any] = {"_grant": None, "_grant_error": None}
    if isinstance(requested, dict):   # a directory grant: checked here (filesystem), written by the state machine
        try:
            checked["_grant"] = await asyncio.to_thread(grants.check_directory, requested, project_id, settings.data_root)
        except ValueError as exc:
            checked["_grant_error"] = str(exc)
    # Kernel facts go last so a model reply can never set them.
    return {**parsed, "config_sha256": config.config_sha256, "skills": list(config.skill_labels),
            "usage": model_result.usage.model_dump(), "_capabilities": sorted(caps), **checked}
