"""Pure rendering of card items into channel messages (text, buttons, answer-modal pages)."""
from __future__ import annotations

from typing import Any

MESSAGE_LIMIT = 1900          # Discord allows 2000 characters per message
MODAL_FIELDS = 5               # Discord allows 5 inputs per modal
STAGE_NAMES = {"requirements": "需求分析", "design": "方案设计", "build": "实施与自测",
               "review": "独立 Review", "release": "发布", "operate": "运营"}
APPROVAL_NAMES = {"requirements": "需求文档", "design": "设计文档"}
ACTION_LABELS = {"execute": "执行", "revise": "修改…", "stop": "停止任务", "approve": "批准", "reject": "不同意",
                 "retry": "重试", "continue": "继续", "published": "确认已发布成功", "failed": "确认发布失败"}
ACTION_STYLES = {"execute": "success", "approve": "success", "retry": "primary", "continue": "primary",
                 "published": "success", "stop": "danger", "reject": "danger", "failed": "danger"}
DECISION_TITLES = {"release": "申请发布", "complete": "建议结束任务", "cancel": "建议取消任务",
                   "new_task": "建议新建任务", "run_failed": "执行失败", "turn_budget": "回合数已达上限",
                   "reconcile": "发布结果需要核对", "create_project": "创建新项目",
                   "followups": "建议排队的新任务"}
DONE_REPLIES = {"execute": "已确认，PM 将开始执行。", "approve": "已批准。", "reject": "已记录：不同意。",
                "stop": "任务已停止。", "retry": "已安排重试。", "continue": "已继续。",
                "published": "已记录：发布成功。", "failed": "已记录：发布失败。", "submit": "答案已提交。",
                "revise": "你的想法已交给 Supervisor，它会据此重新安排。"}


def button(item_id: str, action: str, label: str | None = None) -> dict[str, str]:
    return {"custom_id": f"core:{item_id}:{action}", "label": label or ACTION_LABELS.get(action, action),
            "style": ACTION_STYLES.get(action, "secondary")}


def _fit(text: str) -> tuple[str, str | None]:
    """Text for the message, plus the full text as an attachment when it does not fit."""
    if len(text) <= MESSAGE_LIMIT:
        return text, None
    return text[:MESSAGE_LIMIT - 40].rstrip() + "\n…（内容较长，全文见附件）", text


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


def render_item(item: dict[str, Any]) -> dict[str, Any]:
    """{'text', 'buttons', 'attachment'} for one card item."""
    content, item_id = item["content"], item["item_id"]
    message = content.get("message") or ""
    if item["kind"] == "questions":
        lines = [f"**❓ 需要你回答**\n{message}".rstrip()]
        for number, q in enumerate(content["questions"], 1):
            line = f"{number}. {q['text']}" + ("" if q.get("required", True) else "（选填）")
            if q.get("options"):
                line += "\n   选项：" + " / ".join(str(o) for o in q["options"])
            hint = suggested_answer(item, q)
            if hint:
                line += f"\n   推荐：{hint}"
            lines.append(line)
        pages = question_pages(item)
        buttons = [button(item_id, f"answer:{i}", "回答" if len(pages) == 1 else
                          f"回答 {i * MODAL_FIELDS + 1}-{i * MODAL_FIELDS + len(page)}")
                   for i, page in enumerate(pages)]
        buttons += [button(item_id, "revise", "其他想法…"), button(item_id, "stop")]
        text, attachment = _fit("\n".join(lines))
    elif item["kind"] == "next_step":
        stage = STAGE_NAMES.get(content["target_stage"], content["target_stage"])
        lines = [f"**▶ 下一步：{stage}**", message]
        if content.get("approve"):
            names = "、".join(APPROVAL_NAMES.get(a, a) for a in content["approve"])
            lines.append(f"⚠ 点「执行」即同时批准：{names}")
        lines.append(f"\n**给 PM 的指令**\n{content['instruction']}")
        buttons = [button(item_id, a) for a in ("execute", "revise", "stop")]
        text, attachment = _fit("\n".join(line for line in lines if line))
    else:
        title = DECISION_TITLES.get(content["decision"], content["decision"])
        buttons = [button(item_id, a) for a in content.get("actions", [])]
        body = f"**⚑ {title}**\n{message}"
        followups = (content.get("data") or {}).get("tasks")
        if followups:
            body += "\n\n" + "\n".join(f"{n}. {goal}" for n, goal in enumerate(followups, 1))
            body += "\n\n批准后这些任务依次排队执行，本任务结束。"
        spec = (content.get("data") or {}).get("spec")
        if spec:
            profile = spec["business_profile"]
            body += (f"\n\n项目 ID：`{spec['project_id']}`\n名称：{spec['display_name']}\n用途：{spec['purpose']}\n"
                     f"行业：{profile['industry']}\n组织：{profile['organization']}\n使用者：{profile['users']}\n"
                     f"说明：{profile['notes']}\n\n批准后将创建项目目录、PM/Supervisor 配置和 Discord 频道。")
        text, attachment = _fit(body)
    return {"text": text, "buttons": buttons, "attachment": attachment}


def closed_text(item: dict[str, Any]) -> str:
    kind = {"questions": "问题", "next_step": "下一步", "decision": "决定"}.get(item["kind"], "卡片")
    return f"~~{kind}卡片~~ 已处理或已被新卡片取代。"


def pointer_text(link: str) -> str:
    return f"📌 有一项需要你处理的事项，请到 Supervisor 频道查看：{link}"
