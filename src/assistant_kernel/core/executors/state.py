"""Task state ledger (ROUND3_PLAN 4.1): the facts Kernel recorded about one task, rendered as Markdown.

A new PM session reads it instead of relying on an earlier conversation; it is also written to the vault
(`runs/<task>/state.md`) so subagents can read it. Built only from stored rows, never by a model. Written in English
(model-facing); what the user said stays in the user's words.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from assistant_kernel.core import rules
from assistant_kernel.storage import ProjectRuntimeManager

FULL_TURNS = 10           # the latest turns in full; earlier ones as one line each
ACTION_LIMIT = 20
APPROVAL_NAMES = {"requirements": "requirements", "design": "design", "release": "release"}
ACTION_NAMES = {"execute": "confirmed execution", "submit": "submitted answers", "revise": "asked for a change",
                "stop": "stopped", "approve": "approved", "reject": "declined", "retry": "retried", "continue": "continued"}


def _cut(text: Any, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


def render_state(task: dict[str, Any], ledger: dict[str, Any], stage_names: dict[str, str]) -> str:
    lines = ["### Task", f"- Goal: {task['goal']}"]
    if task.get("reason"):
        lines.append(f"- Why it was started: {task['reason']}")
    if task.get("parent_task_id") or task.get("source_ref"):
        lines.append(f"- Source task: {task.get('parent_task_id') or 'not recorded'}; proposal reference: "
                     f"{task.get('source_ref') or 'not recorded'}")
    if ledger.get("source_proposal"):
        lines.append("- Source review artifact (read it with read_project_document; it does not mean the requirements "
                     "are approved): " + json.dumps(ledger["source_proposal"], ensure_ascii=False))
    lines.append(f"- Current stage: {stage_names.get(task['stage'], task['stage'])}; track: "
                 f"{'quick' if task.get('track') == 'quick' else 'full'}; PM turns completed: {len(ledger['turns'])}")
    approvals = [f"{APPROVAL_NAMES.get(k, k)} ({'valid' if rules.approval_valid(task, k) else 'void: the document changed after approval'})"
                 for k in task["approvals"] if k in APPROVAL_NAMES]
    lines.append("- Approvals: " + (", ".join(approvals) if approvals else "none yet"))
    docs = [f"{kind} (sha {doc['sha256'][:12]})" for kind, doc in task["documents"].items()
            if isinstance(doc, dict) and doc.get("sha256")]
    lines.append("- Documents: " + (", ".join(docs) if docs else "none yet") + " (read them with read_task_document)")

    lines.append("\n### Discussion record (by turn, facts recorded by Kernel)")
    cards = ledger.get("cards") or []   # card items recorded since 0.45.12; older tasks render as before
    turns = ledger["turns"]
    if not turns:
        lines.append("(no completed PM turn yet)")
    early = len(turns) - FULL_TURNS
    for number, turn in enumerate(turns, 1):
        given, result = turn["input"] or {}, turn["result"] or {}
        stage = stage_names.get(given.get("stage"), given.get("stage"))
        head = f"- Turn {number} ({stage}, {turn['created_at']:%m-%d %H:%M})"
        if number <= early:
            lines.append(f"{head}: {_cut(result.get('summary'), 150)}")
            continue
        lines.append(head)
        if given.get("kind") == "instruction":
            lines.append(f"  - Instruction (confirmed by the user): {_cut(given.get('instruction'), 1200)}")
            for guess in given.get("inferred_intent") or []:
                lines.append(f"  - Supervisor's inference (assumption): {_cut(guess.get('text'), 300)}"
                             + (f" (basis: {_cut(guess.get('basis'), 150)})" if guess.get("basis") else ""))
        elif given.get("kind") == "answers" and cards:
            lines.append("  - Based on the user's answers to card questions (questions and answers under \"Card questions\")")
        elif given.get("kind") == "answers":   # a task from before card records: the answers as the turn got them
            asked = {q["id"]: q["text"] for q in given.get("questions") or []}
            for qid, value in (given.get("answers") or {}).items():
                lines.append(f"  - User answered: {_cut(asked.get(qid, qid), 120)} → {_cut(value, 300)}")
        if given.get("note") and not (cards and given.get("kind") == "answers"):
            lines.append(f"  - User's note: {_cut(given['note'], 400)}")
        for text in turn["supplements"]:
            lines.append(f"  - User added: {_cut(text, 400)}")
        lines.append(f"  - PM result ({result.get('outcome', '')}): {_cut(result.get('summary'), 500)}")
        if cards and result.get("questions"):
            lines.append(f"  - The PM asked {len(result['questions'])} questions (see \"Card questions\")")
        elif not cards:
            for q in result.get("questions") or []:
                lines.append(f"  - PM asked: {_cut(q.get('text'), 200)}")
        for d in result.get("delegations") or []:
            lines.append(f"  - Subagent \"{_cut(d.get('name'), 40)}\": {_cut(d.get('goal'), 150)} → {_cut(d.get('result'), 200)}")

    if cards:
        lines += render_questions(task, cards, ledger["actions"])
    # Question cards are under "Card questions" when recorded; the rest (execute, approve, stop…) stay listed here.
    others = [a for a in ledger["actions"] if a.get("action") != "submit"
              and not (cards and a.get("item_kind") == "questions")][-ACTION_LIMIT:]
    if others:
        lines.append("\n### Card actions (latest)")
        for a in others:
            text = f"; the user wrote: {_cut(a['text'], 300)}" if a.get("text") else ""
            lines.append(f"- {a['at']:%m-%d %H:%M} {ACTION_NAMES.get(a.get('action'), a.get('action'))}"
                         f" \"{_cut(a.get('message'), 120)}\"{text}")
    return "\n".join(lines)


QA_FULL = 15   # the latest question cards in full; earlier ones as one line each


def render_questions(task: dict[str, Any], cards: list[dict[str, Any]], actions: list[dict[str, Any]]) -> list[str]:
    """Every question card of the task in time order, whoever asked, with the user's answers or why there are none.
    The status follows from the records: an action on the item, a later item replacing it, or the task's state."""
    acted = {a.get("item_id"): a for a in actions}
    replaced = {c.get("replaces") for c in cards if c.get("replaces")}
    latest = cards[-1].get("item_id") if cards else None
    asked = [c for c in cards if c.get("item_kind") == "questions"]
    lines = ["\n### Card questions (recorded by Kernel; questions from both the PM and the Supervisor)"]
    if not asked:
        return lines + ["(no card questions in this task yet)"]
    early = len(asked) - QA_FULL
    for number, card in enumerate(asked, 1):
        content = card.get("content") or {}
        who = "asked by the PM, relayed by the Supervisor" if card.get("deliver_to") == "pm" else "asked by the Supervisor"
        action = acted.get(card.get("item_id"))
        if action and action.get("action") == "submit":
            status = "answered"
        elif action and action.get("action") == "revise":
            status = f"not answered: the user asked for a change instead \"{_cut(action.get('text'), 300)}\""
        elif action and action.get("action") == "stop":
            status = "not answered: the user stopped the task"
        elif card.get("item_id") in replaced:
            status = "replaced by a newer card"
        elif task.get("state") in rules.TERMINAL_STATES:
            status = "not answered: the task has ended"
        elif card.get("item_id") == latest and task.get("state") == "awaiting_user":
            status = "waiting for an answer"
        else:
            status = "not answered"
        questions = [q for q in content.get("questions") or [] if isinstance(q, dict)]
        head = f"- {card['at']:%m-%d %H:%M} {who} ({status})"
        if number <= early:
            lines.append(f"{head}: {_cut('; '.join(str(q.get('text')) for q in questions), 150)}")
            continue
        lines.append(head)
        answers = {qa.get("id"): qa.get("answer") for qa in (action or {}).get("qa") or []} \
            if action and action.get("action") == "submit" else {}
        if action and action.get("action") == "submit" and not answers:   # recorded before answers had texts
            answers = (action.get("answers") or {})
        for q in questions:
            answer = answers.get(q.get("id"))
            lines.append(f"  - Q: {_cut(q.get('text'), 600)}" + (f" → A: {_cut(answer, 600)}" if answer else ""))
        if action and action.get("action") == "submit" and action.get("text"):
            lines.append(f"  - User's note: {_cut(action['text'], 600)}")
    return lines


def state_path(settings, project_id: str, task_id: str) -> Path:
    vault = ProjectRuntimeManager(settings.data_root).ensure(project_id).vault_root
    return vault / "runs" / str(task_id) / "state.md"


def write_state(settings, project_id: str, task_id: str, text: str) -> str:
    """Write the ledger where subagents can read it; returns its vault-relative path."""
    target = state_path(settings, project_id, task_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, "utf-8", newline="\n")
    return f"runs/{task_id}/state.md"
