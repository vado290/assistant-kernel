"""Pure rendering of card items into channel messages (text, buttons, answer-modal pages)."""
from __future__ import annotations

from typing import Any

from assistant_kernel.core.i18n import stage_name as i18n_stage_name, t

MESSAGE_LIMIT = 1900          # Discord allows 2000 characters per message
MODAL_FIELDS = 5               # Discord allows 5 inputs per modal
ACTION_STYLES = {"execute": "success", "approve": "success", "retry": "primary", "continue": "primary",
                 "published": "success", "stop": "danger", "reject": "danger", "failed": "danger"}
DONE_ACTIONS = ("execute", "approve", "reject", "stop", "retry", "continue", "published", "failed", "submit", "revise")


def stage_name(stage: str) -> str:
    return i18n_stage_name(stage)


def done_reply(action: str) -> str:
    return t(f"done.{action}" if action in DONE_ACTIONS else "done.default")


def access_name(access: str) -> str:
    return t("card.access_write" if access == "write" else "card.access_read")


def button(item_id: str, action: str, label: str | None = None) -> dict[str, str]:
    known = t(f"action.{action}")
    return {"custom_id": f"core:{item_id}:{action}", "label": label or (known if known != f"action.{action}" else action),
            "style": ACTION_STYLES.get(action, "secondary")}


def _fit(text: str) -> tuple[str, str | None]:
    """Text for the message, plus the full text as an attachment when it does not fit."""
    if len(text) <= MESSAGE_LIMIT:
        return text, None
    return text[:MESSAGE_LIMIT - 40].rstrip() + "\n" + t("card.long"), text


def question_pages(item: dict[str, Any]) -> list[list[dict[str, Any]]]:
    questions = item["content"]["questions"]
    return [questions[i:i + MODAL_FIELDS] for i in range(0, len(questions), MODAL_FIELDS)] or [[]]


def suggested_answer(item: dict[str, Any], question: dict[str, Any]) -> str:
    """Pre-fill: Supervisor's suggestion for a covered PM question, else the recommended option."""
    suggested = item["content"].get("suggested") or {}
    for pm_id in question.get("covers") or []:
        if pm_id in suggested:
            return str(suggested[pm_id].get("answer") or "")
    return str(question.get("recommended") or "")


def answer_label(page: int, size: int, pages: int) -> str:
    return t("card.answer") if pages == 1 else \
        t("card.answer_range", start=page * MODAL_FIELDS + 1, end=page * MODAL_FIELDS + size)


def render_item(item: dict[str, Any]) -> dict[str, Any]:
    """{'text', 'buttons', 'attachment'} for one card item."""
    content, item_id = item["content"], item["item_id"]
    message = content.get("message") or ""
    if item["kind"] == "questions":
        lines = [f"**{t('card.questions_title')}**\n{message}".rstrip()]
        for number, q in enumerate(content["questions"], 1):
            line = f"{number}. {q['text']}" + ("" if q.get("required", True) else t("card.optional"))
            if q.get("options"):
                line += "\n   " + t("card.options") + " / ".join(str(o) for o in q["options"])
            hint = suggested_answer(item, q)
            if hint:
                line += "\n   " + t("card.recommended") + hint
            lines.append(line)
        pages = question_pages(item)
        buttons = [button(item_id, f"answer:{i}", answer_label(i, len(page), len(pages)))
                   for i, page in enumerate(pages)]
        buttons += [button(item_id, "revise", t("card.other_thoughts")), button(item_id, "stop")]
        text, attachment = _fit("\n".join(lines))
    elif item["kind"] == "next_step":
        lines = [f"**{t('card.next_step', stage=stage_name(content['target_stage']))}**", message]
        if content.get("track_changed") and content.get("track") in ("quick", "full"):
            lines.append(t(f"track.{content['track']}"))
        if content.get("approve"):
            names = t("list.separator").join(t(f"approval.{a}") if a in ("requirements", "design") else a
                                                    for a in content["approve"])
            lines.append(t("card.approve_with_execute", names=names))
        if content.get("inferred_intent"):
            lines.append(f"\n**{t('card.inferred')}**\n" + "\n".join(
                f"- {i['text']}" + (t("card.basis", basis=i["basis"]) if i.get("basis") else "")
                for i in content["inferred_intent"]))
        lines.append(f"\n**{t('card.instruction')}**\n{content['instruction']}")
        buttons = [button(item_id, a) for a in ("execute", "revise", "stop")]
        text, attachment = _fit("\n".join(line for line in lines if line))
    else:
        decision = content["decision"]
        title = t(f"decision.{decision}")
        title = decision if title == f"decision.{decision}" else title
        buttons = [button(item_id, a) for a in content.get("actions", [])]
        body = f"**⚑ {title}**\n{message}"
        data = content.get("data") or {}
        followups = data.get("tasks")
        if followups:
            body += "\n\n" + "\n".join(_followup_line(n, f) for n, f in enumerate(followups, 1))
            body += "\n\n" + t("card.followups_note")
        if decision == "shared_update":
            body += ("\n\n" + t("card.shared_file", file=data["file"], section=data["section"],
                                reason=data["reason"] or t("card.unspecified"))
                     + f"\n\n**{t('card.shared_now')}**\n{data.get('before') or t('card.shared_new_section')}"
                     + f"\n\n**{t('card.shared_after')}**\n{data['content']}\n\n" + t("card.shared_note"))
        spec = data.get("spec")
        if spec:
            body += "\n\n" + t("card.project_spec", project_id=spec["project_id"], name=spec["display_name"],
                               purpose=spec["purpose"],
                               capabilities=t("list.separator").join(spec.get("capabilities") or [])
                               or t("card.none"),
                               delivery=spec.get("delivery"), share=spec.get("share_with_kernel"))
            for d in spec.get("directories") or []:
                body += "\n" + t("card.spec_directory", path=d["path"], access=access_name(d["access"]),
                                 purpose=d["purpose"])
        if decision == "release":   # approving the release also approves its iteration record
            body += ("\n\n" + t("card.release_record", record=data["record"])
                     + (t("card.major") if data.get("major") else "")
                     if data.get("record") else "\n\n" + t("card.no_record"))
        directory = data.get("directory")
        if decision == "grant" and directory:
            body += "\n\n" + t("card.grant", path=directory["path"], access=access_name(directory["access"]),
                               purpose=directory["purpose"], key=directory["key"])
        goal = data.get("goal")
        if decision == "project_task" and goal:
            body += "\n\n" + t("card.project_task", goal=goal)
        text, attachment = _fit(body)
    return {"text": text, "buttons": buttons, "attachment": attachment}


def _followup_line(number: int, followup) -> str:
    if isinstance(followup, dict):
        reason = t("card.followup_reason", reason=followup["reason"]) if followup.get("reason") else ""
        return f"{number}. {followup['goal']}{reason}"
    return f"{number}. {followup}"


def closed_text(item: dict[str, Any]) -> str:
    kind = item["kind"] if item["kind"] in ("questions", "next_step") else "decision"
    return t(f"card.closed_{kind}")


def pointer_text(link: str) -> str:
    return t("card.pointer", link=link)
