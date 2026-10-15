"""Task state machine (plan section 3): the only code that changes task state.

Every function here runs inside the caller's transaction, after the project row
has been locked. Each transition updates the task and creates exactly one new
cursor: a queued run or a card item (principle P7).
"""
from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb

from assistant_kernel.core import cards, rules
from assistant_kernel.core.db import all_rows, event, one

MUTEX = {"pm_turn": "pm", "supervisor": "supervisor", "validation": "validation", "publish": "publish"}
JSON_FIELDS = ("documents", "approvals", "pm_session")
EXTRA_TURNS = 12

DECISION_ACTIONS = {
    "release": ("approve", "reject"),
    "complete": ("approve", "reject"),
    "cancel": ("approve", "reject"),
    "new_task": ("approve", "reject"),
    "run_failed": ("retry", "stop"),
    "turn_budget": ("continue", "stop"),
    "reconcile": ("published", "failed"),
}


class InvalidResult(ValueError):
    """A model result breaks the contract; the run is failed (and retried) instead of applied."""


# ---------------------------------------------------------------- basic writes

async def load_task(c, task_id) -> dict[str, Any] | None:
    return await one(c, "SELECT * FROM core.tasks WHERE task_id=%s FOR UPDATE", (task_id,))


async def _update(c, task: dict[str, Any], **fields: Any) -> dict[str, Any]:
    names = list(fields)
    values = [Jsonb(fields[n]) if n in JSON_FIELDS else fields[n] for n in names]
    sets = ", ".join(f"{n}=%s" for n in names)
    row = await one(c, f"""UPDATE core.tasks SET {sets}, state_revision=state_revision+1, updated_at=now()
                           WHERE task_id=%s RETURNING *""", (*values, task["task_id"]))
    if "state" in fields and fields["state"] != task["state"]:
        await event(c, task["project_id"], "task_state", task_id=task["task_id"],
                    old=task["state"], new=fields["state"], stage=row["stage"])
    return row


async def enqueue(c, project_id: str, run_type: str, purpose: str = "default", run_input: dict | None = None,
                  *, task: dict[str, Any] | None = None, base: dict | None = None) -> dict[str, Any]:
    base = base if base is not None else ({"state_revision": task["state_revision"]} if task else {})
    return await one(
        c, """INSERT INTO core.runs(project_id, task_id, run_type, purpose, input, base, mutex_key)
              VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *""",
        (project_id, task["task_id"] if task else None, run_type, purpose, Jsonb(run_input or {}),
         Jsonb(base), f"{MUTEX[run_type]}:{project_id}" if run_type in MUTEX else None))


async def notify(c, project_id: str, role: str, text: str, *, task_id=None, chat: dict | None = None) -> None:
    """Queue an outbound message (delivered by the gateway in phase 3)."""
    chat = chat or {}
    await c.execute(
        """INSERT INTO core.messages(project_id, channel, chat_id, user_id, direction, role, body, task_id,
                                     delivery_status)
           VALUES (%s, %s, %s, %s, 'outbound', %s, %s, %s, 'pending')""",
        (project_id, chat.get("channel", "kernel"), chat.get("chat_id"), chat.get("user_id"), role,
         Jsonb({"text": text}), task_id))


# ---------------------------------------------------------------- transitions (each creates one cursor)

async def to_supervising(c, task, reason: str, **data: Any) -> dict[str, Any]:
    task = await _update(c, task, state="supervising")
    await enqueue(c, task["project_id"], "supervisor", "evaluate", {"reason": reason, **data}, task=task)
    return task


async def to_pm(c, task, turn_input: dict[str, Any], *, stage: str | None = None) -> dict[str, Any]:
    stage = stage or task["stage"]
    fresh = task["pm_session"].get("stage") != stage
    task = await _update(c, task, state="pm", stage=stage)
    await enqueue(c, task["project_id"], "pm_turn", "default",
                  {**turn_input, "stage": stage, "fresh_session": fresh}, task=task)
    return task


async def to_awaiting(c, task, item: dict[str, Any]) -> dict[str, Any]:
    task = await _update(c, task, state="awaiting_user")
    await cards.replace_task_item(c, task["project_id"], task["task_id"], item)
    return task


async def to_validating(c, task) -> dict[str, Any]:
    task = await _update(c, task, state="validating")
    bundle = (task["documents"].get("implementation_report") or {}).get("bundle_sha")
    await enqueue(c, task["project_id"], "validation", "default", {"bundle_sha": bundle}, task=task)
    return task


async def to_publishing(c, task) -> dict[str, Any]:
    task = await _update(c, task, state="publishing", stage="release")
    await enqueue(c, task["project_id"], "publish", "default",
                  {"bundle_sha": task["approvals"]["release"]["bundle_sha"]}, task=task)
    return task


async def close(c, task, state: str, reason: str) -> dict[str, Any]:
    """Finish a task, withdraw its runs and card item, then start the next queued task."""
    task = await _update(c, task, state=state, close_reason=reason, closed_at=_now())
    await c.execute("UPDATE core.runs SET status='cancelled', ended_at=now() WHERE task_id=%s AND status='queued'",
                    (task["task_id"],))
    await c.execute("UPDATE core.runs SET cancel_requested=true WHERE task_id=%s AND status IN ('running','reconciling')",
                    (task["task_id"],))
    await cards.remove_items(c, task["project_id"], lambda i: i["target"] != {"kind": "task", "id": str(task["task_id"])})
    await cards.clear_task_extras(c, task["project_id"], task["task_id"])
    if state == "completed" and rules.approval_valid(task, "release"):
        docs = task["documents"]
        await c.execute(
            "UPDATE core.projects SET operational_baseline=%s, updated_at=now() WHERE project_id=%s",
            (Jsonb({"task_id": str(task["task_id"]), "task_contract": docs.get("task_contract"),
                    "solution_design": docs.get("solution_design"),
                    "bundle_sha": task["approvals"]["release"]["bundle_sha"]}), task["project_id"]))
    await activate_next(c, task["project_id"])
    return task


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


async def activate_next(c, project_id: str) -> dict[str, Any] | None:
    active = await one(c, "SELECT task_id FROM core.tasks WHERE project_id=%s AND state = ANY(%s)",
                       (project_id, list(rules.ACTIVE_STATES)))
    if active:
        return None
    task = await one(c, """SELECT * FROM core.tasks WHERE project_id=%s AND state='queued'
                           ORDER BY (priority='urgent') DESC, created_at LIMIT 1 FOR UPDATE""", (project_id,))
    if task is None:
        return None
    task = await _update(c, task, stage=rules.INITIAL_STAGE[task["kind"]])
    return await to_supervising(c, task, "start")


async def create_task(c, project_id: str, *, goal: str, origin: str, kind: str = "delivery",
                      source_message_id=None, priority: str = "normal", parent_task_id=None,
                      idempotency_key: str | None = None) -> dict[str, Any]:
    task = await one(
        c, """INSERT INTO core.tasks(project_id, kind, origin, priority, goal, source_message_id,
                                     parent_task_id, idempotency_key)
              VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
              ON CONFLICT (idempotency_key) DO NOTHING RETURNING *""",
        (project_id, kind, origin, priority, goal, source_message_id, parent_task_id, idempotency_key))
    if task is None:
        return await one(c, "SELECT * FROM core.tasks WHERE idempotency_key=%s", (idempotency_key,))
    await event(c, project_id, "task_created", task_id=task["task_id"], origin=origin, kind=kind)
    await activate_next(c, project_id)
    return await one(c, "SELECT * FROM core.tasks WHERE task_id=%s", (task["task_id"],))


# ---------------------------------------------------------------- staleness

def is_cursor(task: dict[str, Any] | None, run: dict[str, Any]) -> bool:
    """True when `run` is the cursor the task is currently waiting on."""
    return bool(task and task["state"] in rules.CURSOR_RUN
                and rules.CURSOR_RUN[task["state"]] == (run["run_type"], run["purpose"])
                and run["base"].get("state_revision") == task["state_revision"])


# ---------------------------------------------------------------- run results

async def on_result(c, run: dict[str, Any], result: dict[str, Any]) -> None:
    if run["run_type"] == "supervisor" and run["purpose"] == "consult":
        return await _consult_result(c, run, result)
    task = await load_task(c, run["task_id"])
    if run["purpose"] == "review_input":
        return await _review_input_result(c, task, run, result)
    if not is_cursor(task, run):
        await event(c, run["project_id"], "stale_result", task_id=run["task_id"], run_id=run["run_id"])
        return
    handler = {"pm_turn": _pm_result, "supervisor": _supervisor_result,
               "validation": _validation_result, "publish": _publish_result}[run["run_type"]]
    await handler(c, task, run, result)


async def _pm_result(c, task, run, result) -> None:
    outcome = result.get("outcome")
    docs = dict(task["documents"])
    report = result.get("report")
    if report:
        if report.get("kind") not in ("task_contract", "solution_design", "implementation_report", "review_report") \
                or not report.get("sha256"):
            raise InvalidResult("report needs a known kind and sha256")
        docs[report["kind"]] = report
    session = {"stage": run["input"]["stage"], "native_session_id": result.get("session_id")}
    task = await _update(c, task, documents=docs, pm_session=session, turn_count=task["turn_count"] + 1)
    summary = result.get("summary", "")
    if task["turn_count"] > task["max_turns"]:
        await to_awaiting(c, task, _decision(task, "turn_budget", f"已执行 {task['turn_count']} 轮，是否继续？",
                                             pm_result=result))
    elif outcome == "needs_input":
        questions = _checked_questions(result.get("questions"))
        await to_supervising(c, task, "pm_questions", pm_questions=questions, summary=summary)
    elif outcome == "ready_for_validation":
        if task["stage"] != "build" or not (docs.get("implementation_report") or {}).get("bundle_sha"):
            raise InvalidResult("validation needs the build stage and an implementation_report with bundle_sha")
        await to_validating(c, task)
    elif outcome in ("completed", "needs_approval", "continue", "failed"):
        await to_supervising(c, task, "pm_" + outcome, summary=summary)
    elif outcome in ("return_to_requirements", "return_to_design"):
        await to_supervising(c, task, "pm_return", target=outcome.removeprefix("return_to_"), summary=summary)
    else:
        raise InvalidResult(f"unknown PM outcome {outcome!r}")


async def _supervisor_result(c, task, run, result) -> None:
    newer = await one(c, """SELECT 1 FROM core.messages WHERE task_id=%s AND direction='inbound'
                            AND created_at > %s LIMIT 1""", (task["task_id"], run["started_at"]))
    if newer:
        # The user said something while Supervisor was thinking: evaluate again with it.
        await enqueue(c, task["project_id"], "supervisor", "evaluate", run["input"], task=task)
        await event(c, task["project_id"], "supervisor_reevaluate", task_id=task["task_id"], run_id=run["run_id"])
        return
    await to_awaiting(c, task, _supervisor_item(task, run["input"], result))


def _supervisor_item(task, run_input: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """Turn a Supervisor decision into the task's card item, enforcing Kernel rules."""
    decision = result.get("decision")
    message = result.get("message", "")
    if run_input.get("reason") == "pm_questions":
        pm_questions = run_input["pm_questions"]
        if decision != "relay_pm_questions" or not _covers(pm_questions, result):
            return _raw_pm_questions(task, pm_questions)
        return cards.new_item(target_kind="task", target_id=str(task["task_id"]), kind="questions",
                              origin="pm", deliver_to="pm",
                              content={"message": message, "questions": _checked_questions(result["questions"]),
                                       "pm_questions": pm_questions, "suggested": result.get("suggested", {})})
    if decision == "ask_user":
        return cards.new_item(target_kind="task", target_id=str(task["task_id"]), kind="questions",
                              origin="supervisor", deliver_to="supervisor",
                              content={"message": message, "questions": _checked_questions(result.get("questions"))})
    if decision == "propose_next":
        target = result.get("target_stage") or task["stage"]
        if target == "release":
            error = rules.move_error(task, "release")
            if error:
                raise InvalidResult(error)
            bundle = rules.validated_bundle(task)
            return _decision(task, "release", message or "是否批准发布？",
                             bundle_sha=bundle, review_sha=rules.doc_sha(task, "review_report"))
        if not result.get("instruction"):
            raise InvalidResult("propose_next needs an instruction")
        approve = rules.approvals_needed(task, target)
        error = rules.move_error(task, target, approving=tuple(approve))
        if error:
            raise InvalidResult(error)
        shas = {kind: rules.doc_sha(task, rules.DOCUMENT_FOR_APPROVAL[kind]) for kind in approve}
        return cards.new_item(target_kind="task", target_id=str(task["task_id"]), kind="next_step",
                              origin="supervisor", deliver_to="pm",
                              content={"message": message, "instruction": result["instruction"],
                                       "target_stage": target, "approve": approve, "doc_shas": shas})
    if decision in ("propose_complete", "propose_cancel"):
        return _decision(task, decision.removeprefix("propose_"), message)
    raise InvalidResult(f"unsupported Supervisor decision {decision!r}")


def _covers(pm_questions: list[dict], result: dict[str, Any]) -> bool:
    required = {q["id"] for q in pm_questions if q.get("required", True)}
    covered = {pm_id for q in result.get("questions") or [] for pm_id in q.get("covers", [])}
    return required <= covered | set(result.get("suggested") or {})


def _raw_pm_questions(task, pm_questions) -> dict[str, Any]:
    return cards.new_item(target_kind="task", target_id=str(task["task_id"]), kind="questions",
                          origin="kernel", deliver_to="pm",
                          content={"message": "PM 的原始问题", "questions": pm_questions, "pm_questions": pm_questions})


def _checked_questions(questions) -> list[dict[str, Any]]:
    if not questions or not all(isinstance(q, dict) and q.get("id") and q.get("text") for q in questions):
        raise InvalidResult("questions need an id and text")
    if len({q["id"] for q in questions}) != len(questions):
        raise InvalidResult("question ids must be unique")
    return questions


def _decision(task, decision: str, text: str, **data: Any) -> dict[str, Any]:
    return cards.new_item(target_kind="task", target_id=str(task["task_id"]), kind="decision", origin="kernel",
                          content={"decision": decision, "message": text,
                                   "actions": list(DECISION_ACTIONS[decision]), "data": data})


async def _validation_result(c, task, run, result) -> None:
    docs = dict(task["documents"])
    docs["validation"] = {"bundle_sha": run["input"]["bundle_sha"], "passed": bool(result.get("passed")),
                          "report": result.get("report")}
    task = await _update(c, task, documents=docs)
    await to_supervising(c, task, "validation_passed" if result.get("passed") else "validation_failed")


async def _publish_result(c, task, run, result) -> None:
    outcome = result.get("outcome")
    if outcome == "published":
        await close(c, task, "completed", "published")
    elif outcome == "failed":
        await to_supervising(c, task, "publish_failed", error=result.get("error"))
    else:
        await to_awaiting(c, task, _decision(task, "reconcile", "发布结果不确定，请核对后选择实际结果。"))


async def _review_input_result(c, task, run, result) -> None:
    item = cards.task_item(await cards.open_card(c, run["project_id"]), run["task_id"]) if task else None
    if not task or task["state"] != "awaiting_user" or not item or item["item_id"] != run["base"].get("item_id"):
        await event(c, run["project_id"], "stale_result", task_id=run["task_id"], run_id=run["run_id"])
        return
    if result.get("decision") == "keep":
        return
    await _upgrade_item(c, task, item, result.get("item") or {})


async def _upgrade_item(c, task, old_item, proposal: dict[str, Any]) -> None:
    run_input = {}
    if old_item["deliver_to"] == "pm" and old_item["content"].get("pm_questions"):
        run_input = {"reason": "pm_questions", "pm_questions": old_item["content"]["pm_questions"]}
    new = _supervisor_item(task, run_input, proposal)
    await cards.replace_task_item(c, task["project_id"], task["task_id"], new)
    await event(c, task["project_id"], "card_upgraded", task_id=task["task_id"],
                old_item=old_item["item_id"], new_item=new["item_id"])


async def _consult_result(c, run, result) -> None:
    if not result.get("reply"):
        raise InvalidResult("a consultation must reply to the user")
    message = await one(c, "SELECT * FROM core.messages WHERE message_id=%s", (run["input"]["message_id"],))
    chat = {"channel": message["channel"], "chat_id": message["chat_id"], "user_id": message["user_id"]}
    if result.get("reply"):
        await notify(c, run["project_id"], "supervisor", result["reply"], chat=chat)
    new_task = result.get("propose_new_task")
    if new_task:
        await cards.add_item(c, run["project_id"], cards.new_item(
            target_kind="consultation", target_id=str(run["run_id"]), kind="decision", origin="supervisor",
            content={"decision": "new_task", "message": new_task.get("message") or "是否创建新任务？",
                     "actions": list(DECISION_ACTIONS["new_task"]),
                     "data": {"goal": new_task["goal"], "source_message_id": str(message["message_id"])}}))
    upgrade = result.get("upgrade")
    context_task = run["input"].get("context_task_id")
    if upgrade and context_task:
        task = await load_task(c, context_task)
        item = cards.task_item(await cards.open_card(c, run["project_id"]), context_task)
        if task and task["state"] == "awaiting_user" and item:
            await _upgrade_item(c, task, item, upgrade)


async def on_exhausted(c, run: dict[str, Any]) -> None:
    """A run failed after all retries: give the user a way forward instead of getting stuck."""
    if run["purpose"] in ("review_input", "consult"):
        await event(c, run["project_id"], "side_run_failed", task_id=run["task_id"], run_id=run["run_id"])
        return
    task = await load_task(c, run["task_id"])
    if not is_cursor(task, run):
        return
    if run["run_type"] == "supervisor" and run["input"].get("reason") == "pm_questions":
        await to_awaiting(c, task, _raw_pm_questions(task, run["input"]["pm_questions"]))
    elif run["run_type"] == "publish":
        await to_awaiting(c, task, _decision(task, "reconcile", "发布执行失败或中断，请核对实际结果。"))
    else:
        await to_awaiting(c, task, _decision(
            task, "run_failed", f"{run['run_type']} 多次失败：{(run.get('error') or {}).get('message', '')}",
            run_type=run["run_type"], run_input=run["input"]))


# ---------------------------------------------------------------- card actions

async def on_action(c, project_id: str, item_id: str, action: str, values: dict[str, Any],
                    source_message_id=None) -> dict[str, Any]:
    card, item = await cards.take_item(c, project_id, item_id)
    await event(c, project_id, "card_action", interaction_id=card["interaction_id"],
                task_id=item["target"]["id"] if item["target"]["kind"] == "task" else None,
                item_id=item_id, action=action)
    if item["target"]["kind"] == "consultation":
        return await _consultation_action(c, project_id, item, action, source_message_id)
    task = await load_task(c, item["target"]["id"])
    if not task or task["state"] != "awaiting_user":
        raise cards.StaleItem("the task is no longer waiting for this item")
    await cards.clear_task_extras(c, project_id, task["task_id"])
    handler = {"questions": _questions_action, "next_step": _next_step_action, "decision": _decision_action}
    return await handler[item["kind"]](c, task, item, action, values, source_message_id)


async def _questions_action(c, task, item, action, values, source_message_id):
    if action != "submit":
        raise ValueError("questions accept only submit")
    answers = values.get("answers") or {}
    missing = [q["id"] for q in item["content"]["questions"] if q.get("required", True) and not answers.get(q["id"])]
    if missing:
        raise ValueError(f"required questions unanswered: {', '.join(missing)}")
    payload = {"questions": item["content"]["questions"], "answers": answers,
               "note": values.get("text", ""), "source_message_id": _str(source_message_id)}
    if item["deliver_to"] == "pm":
        payload.update(pm_questions=item["content"].get("pm_questions"), suggested=item["content"].get("suggested"))
        return await to_pm(c, task, {"kind": "answers", **payload})
    return await to_supervising(c, task, "answers", **payload)


async def _next_step_action(c, task, item, action, values, source_message_id):
    content = item["content"]
    if action == "revise":
        if not values.get("text"):
            raise ValueError("revise needs the requested change")
        return await to_supervising(c, task, "revise", text=values["text"], instruction=content["instruction"])
    if action == "stop":
        return await close(c, task, "cancelled", "stopped_by_user")
    if action != "execute":
        raise ValueError("next_step accepts execute, revise or stop")
    approvals = dict(task["approvals"])
    for kind in content["approve"]:
        sha = rules.doc_sha(task, rules.DOCUMENT_FOR_APPROVAL[kind])
        if sha != content["doc_shas"][kind]:
            raise cards.StaleItem(f"the {kind} document changed after this card was shown")
        approvals[kind] = {"doc_sha": sha, "message_id": _str(source_message_id)}
        if kind == "design":
            approvals[kind]["based_on_requirements_sha"] = rules.doc_sha(task, "task_contract")
    error = rules.move_error({**task, "approvals": approvals}, content["target_stage"])
    if error:
        raise cards.StaleItem(error)
    task = await _update(c, task, approvals=approvals)
    return await to_pm(c, task, {"kind": "instruction", "instruction": content["instruction"],
                                 "note": values.get("text", "")}, stage=content["target_stage"])


async def _decision_action(c, task, item, action, values, source_message_id):
    decision, data = item["content"]["decision"], item["content"]["data"]
    if action not in DECISION_ACTIONS[decision]:
        raise ValueError(f"{decision} accepts {DECISION_ACTIONS[decision]}")
    note = values.get("text", "")
    if decision == "release":
        if action == "reject":
            return await to_supervising(c, task, "release_rejected", text=note)
        if rules.validated_bundle(task) != data["bundle_sha"] or rules.doc_sha(task, "review_report") != data["review_sha"]:
            raise cards.StaleItem("the reviewed bundle changed after this card was shown")
        approvals = {**task["approvals"], "release": {"bundle_sha": data["bundle_sha"], "review_sha": data["review_sha"],
                                                      "message_id": _str(source_message_id)}}
        return await to_publishing(c, await _update(c, task, approvals=approvals))
    if decision in ("complete", "cancel"):
        if action == "reject":
            return await to_supervising(c, task, f"{decision}_rejected", text=note)
        return await close(c, task, "completed" if decision == "complete" else "cancelled", f"user_{decision}")
    if decision == "turn_budget":
        if action == "stop":
            return await close(c, task, "cancelled", "turn_budget_stopped")
        task = await _update(c, task, max_turns=task["turn_count"] + EXTRA_TURNS)
        return await to_supervising(c, task, "pm_continue", summary=data["pm_result"].get("summary", ""))
    if decision == "reconcile":
        if action == "published":
            return await close(c, task, "completed", "published_after_reconcile")
        return await to_supervising(c, task, "publish_failed", error="user reported failure")
    if decision == "run_failed":
        if action == "stop":
            return await close(c, task, "cancelled", "stopped_after_failure")
        run_input = data["run_input"]
        if data["run_type"] == "pm_turn":
            return await to_pm(c, task, run_input, stage=run_input["stage"])
        if data["run_type"] == "validation":
            return await to_validating(c, task)
        return await to_supervising(c, task, **run_input)
    raise ValueError(f"unknown decision {decision}")


async def _consultation_action(c, project_id, item, action, source_message_id):
    data = item["content"]["data"]
    if item["content"]["decision"] != "new_task" or action not in ("approve", "reject"):
        raise ValueError("consultation items accept approve or reject")
    if action == "approve":
        return await create_task(c, project_id, goal=data["goal"], origin="supervisor",
                                 source_message_id=data["source_message_id"],
                                 idempotency_key=f"consult-task:{item['item_id']}")
    return {}


def _str(value) -> str | None:
    return str(value) if value is not None else None


# ---------------------------------------------------------------- user input

async def user_input(c, project_id: str, message: dict[str, Any]) -> dict[str, Any]:
    """PM-channel text: supplements the active task, or starts a new task when the project is idle."""
    active = await one(c, "SELECT * FROM core.tasks WHERE project_id=%s AND state = ANY(%s) FOR UPDATE",
                       (project_id, list(rules.ACTIVE_STATES)))
    if active is None:
        task = await create_task(c, project_id, goal=message["body"].get("text", ""), origin="user",
                                 source_message_id=message["message_id"],
                                 idempotency_key=f"message-task:{message['message_id']}")
        await c.execute("UPDATE core.messages SET task_id=%s, consumed_by_run_id=NULL WHERE message_id=%s",
                        (task["task_id"], message["message_id"]))
        return {"action": "created", "task_id": str(task["task_id"])}
    await c.execute("UPDATE core.messages SET task_id=%s WHERE message_id=%s",
                    (active["task_id"], message["message_id"]))
    if active["state"] == "awaiting_user":
        await cards.add_supplement(c, project_id, active["task_id"], message["message_id"])
        item = cards.task_item(await cards.open_card(c, project_id), active["task_id"])
        pending = await one(c, """SELECT 1 FROM core.runs WHERE task_id=%s AND purpose='review_input'
                                  AND status IN ('queued','running')""", (active["task_id"],))
        if item and not pending:
            await enqueue(c, project_id, "supervisor", "review_input", {"message_id": str(message["message_id"])},
                          task=active, base={"item_id": item["item_id"]})
    return {"action": "attached", "task_id": str(active["task_id"])}


async def consult(c, project_id: str, message: dict[str, Any]) -> dict[str, Any]:
    """Supervisor-channel text: a read-only consultation that never becomes PM input."""
    active = await one(c, "SELECT task_id FROM core.tasks WHERE project_id=%s AND state = ANY(%s)",
                       (project_id, list(rules.ACTIVE_STATES)))
    run = await enqueue(c, project_id, "supervisor", "consult",
                        {"message_id": str(message["message_id"]),
                         "context_task_id": str(active["task_id"]) if active else None})
    return {"action": "consult", "run_id": str(run["run_id"])}


async def pending_inputs(c, task_id) -> list[dict[str, Any]]:
    return await all_rows(c, """SELECT * FROM core.messages WHERE task_id=%s AND direction='inbound'
                                AND consumed_by_run_id IS NULL ORDER BY created_at""", (task_id,))
