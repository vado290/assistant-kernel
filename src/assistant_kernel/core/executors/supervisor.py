"""Supervisor executor (plan 2.7–2.9): a fresh, read-only evaluation that proposes the next card item."""
from __future__ import annotations

import asyncio
import json
from typing import Any

from assistant_kernel.core import rules
from assistant_kernel.core.executors.common import (
    READ_ONLY_TOOLS, call_for_json, load_agent, prompt_text, render_documents,
)

STAGE_ORDER = ["requirements", "design", "build", "review", "release", "operate"]
PURPOSE_PROMPT = {"evaluate": "supervisor-evaluate", "review_input": "supervisor-review-input",
                  "consult": "supervisor-consult"}
REASONS = {
    "start": "新任务刚开始，安排第一步",
    "pm_questions": "PM 提出了问题，需要整理给用户",
    "pm_completed": "PM 完成了本轮工作", "pm_continue": "PM 本轮未完成，需要继续",
    "pm_needs_approval": "PM 提交了需要批准的文档", "pm_failed": "PM 报告本轮失败",
    "pm_return": "PM 建议回到更早的阶段", "answers": "用户回答了你上次的问题",
    "revise": "用户要求修改你上次提出的下一步", "validation_passed": "Kernel 验证通过",
    "validation_failed": "Kernel 验证未通过", "publish_failed": "发布失败",
    "release_rejected": "用户没有批准发布", "complete_rejected": "用户不同意结束任务",
    "cancel_rejected": "用户不同意取消任务",
}


def allowed_moves(task: dict[str, Any]) -> str:
    lines = []
    for target in sorted(rules.STAGE_MOVES.get(task["stage"], ()), key=STAGE_ORDER.index):
        approve = rules.approvals_needed(task, target)
        error = rules.move_error(task, target, approving=tuple(approve))
        if error:
            lines.append(f"- {target}：现在不可以（{error}）")
        else:
            lines.append(f"- {target}：可以" + (f"（用户执行时将同时批准 {', '.join(approve)}）" if approve else ""))
    return "\n".join(lines)


def render_reason(run_input: dict[str, Any]) -> str:
    reason = run_input.get("reason", "")
    lines = [f"- 评估原因：{reason}（{REASONS.get(reason, '')}）"]
    for key, label in (("summary", "PM 本轮总结"), ("text", "用户的修改意见/附言"), ("target", "PM 建议回到的阶段"),
                       ("error", "错误"), ("instruction", "你上次提出的指令")):
        if run_input.get(key):
            lines.append(f"- {label}：{run_input[key]}")
    if run_input.get("pm_questions"):
        lines.append("- PM 的问题：\n" + "\n".join(
            f"  - [{q['id']}]{'（必答）' if q.get('required', True) else ''} {q['text']}" for q in run_input["pm_questions"]))
    if run_input.get("answers"):
        asked = {q["id"]: q["text"] for q in run_input.get("questions") or []}
        lines.append("- 用户的回答：\n" + "\n".join(f"  - {asked.get(k, k)}：{v}" for k, v in run_input["answers"].items()))
    return "\n".join(lines)


def render_messages(messages: list[dict[str, Any]]) -> str:
    who = {("inbound", "pm"): "用户→PM频道", ("inbound", "supervisor"): "用户→Supervisor频道",
           ("outbound", "pm"): "PM", ("outbound", "supervisor"): "Supervisor", ("outbound", "kernel"): "系统"}
    lines = [f"- [{m['created_at']:%m-%d %H:%M}] {who.get((m['direction'], m['role']), m['role'])}：{m['body'].get('text')}"
             for m in messages if m["body"].get("text")]
    return "\n".join(lines) or "（无）"


def render_card(card: dict[str, Any] | None) -> str:
    if not card or not card["items"]:
        return "（没有待处理的卡片）"
    return "\n".join(f"- [{i['kind']}] " + json.dumps(i["content"], ensure_ascii=False)[:1500] for i in card["items"])


async def execute_supervisor(run: dict[str, Any], ctx) -> dict[str, Any]:
    kernel, settings = ctx.kernel, ctx.services["settings"]
    purpose, run_input = run["purpose"], run["input"]
    task_id = run["task_id"] or run_input.get("context_task_id")
    task = await kernel.get_task(task_id) if task_id else None
    config = await asyncio.to_thread(load_agent, settings, run["project_id"], "supervisor")
    card = await kernel.card(run["project_id"])
    sections = [prompt_text("supervisor"), "## 项目说明\n\n" + config.role_text]
    if config.skill_texts:
        sections.append("## 已加载技能\n\n" + "\n\n".join(config.skill_texts))
    if task:
        sections.append(f"## 当前任务\n\n- 目标：{task['goal']}\n- 阶段：{task['stage']}\n- 状态：{task['state']}\n"
                        f"- 已执行 PM 回合：{task['turn_count']}\n\n### 可去的阶段（Kernel 计算）\n\n{allowed_moves(task)}")
        sections.append("## 已生效文档\n\n" + render_documents(settings, task))
    else:
        sections.append("## 当前任务\n\n（项目当前没有进行中的任务）")
    sections.append("## 最近的对话\n\n" + render_messages(await kernel.recent_messages(run["project_id"])))
    sections.append("## 当前卡片\n\n" + render_card(card))
    if purpose == "evaluate":
        sections.append("## 本次评估\n\n" + render_reason(run_input))
    elif purpose == "review_input":
        message = await kernel.get_message(run_input["message_id"])
        sections.append(f"## 用户刚发的新消息\n\n{message['body'].get('text', '')}")
    else:
        message = await kernel.get_message(run_input["message_id"])
        sections.append(f"## 用户在 Supervisor 频道的发言\n\n{message['body'].get('text', '')}")
    sections.append(prompt_text(PURPOSE_PROMPT[purpose]))
    parsed, model_result = await call_for_json(
        ctx, run, config, "\n\n".join(sections), task_id=str(task_id) if task_id else None, stage=None,
        session_id=None, tools=READ_ONLY_TOOLS, sandbox="read-only")
    if parsed is None:
        return {"decision": "invalid_output", "raw": model_result.final_text[:2000]}
    return {**parsed, "config_sha256": config.config_sha256, "usage": model_result.usage.model_dump()}
