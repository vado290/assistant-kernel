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
    project_catalog, prompt_text, render_documents,
)
from assistant_kernel.core.executors.digest import SHARED_CANDIDATES_HEADING, shared_candidates

STAGE_ORDER = ["requirements", "design", "build", "review", "release", "operate"]
PURPOSE_PROMPT = {"evaluate": "supervisor-evaluate", "review_input": "supervisor-review-input",
                  "consult": "supervisor-consult"}
PURPOSE_NAMES = {"evaluate": "评估并安排下一步", "review_input": "卡片待处理时用户的新消息", "consult": "回答用户的咨询"}
REASONS = {
    "start": "新任务刚开始，安排第一步",
    "pm_questions": "PM 提出了问题，需要整理给用户",
    "pm_completed": "PM 完成了本轮工作",
    "pm_failed": "PM 报告本轮失败",
    "pm_return": "PM 建议回到更早的阶段", "answers": "用户回答了你上次的问题",
    "revise": "用户要求修改你上次提出的下一步", "validation_passed": "Kernel 验证通过",
    "validation_failed": "Kernel 验证未通过", "publish_failed": "发布失败",
    "release_rejected": "用户没有批准发布", "complete_rejected": "用户不同意结束任务",
    "cancel_rejected": "用户不同意取消任务",
    "create_project_rejected": "用户没有批准按当前章程创建项目", "bootstrap_failed": "创建项目失败",
    "followups_rejected": "用户没有批准你提出的后续任务",
    "project_task_rejected": "用户不同意开始创建项目的任务",
    "grant_approved": "用户批准了你提议的目录授权（下一轮起生效）", "grant_rejected": "用户没有批准你提议的目录授权",
}
KIND_NAMES = {"delivery": "交付", "evolution": "周度复盘（自我进化）", "bootstrap": "创建项目"}
TRACK_NAMES = {"full": "完整流程（需求→设计→实施→Review→发布）", "quick": "轻量流程（需求含简要方案→实施→发布）"}


def allowed_moves(task: dict[str, Any]) -> str:
    lines = [f"当前流程：{TRACK_NAMES.get(task.get('track', 'full'))}"]
    for target in sorted(rules.stage_moves(task), key=STAGE_ORDER.index):
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
    if run_input.get("inferred_intent"):
        lines.append("- 你上次的推测（用户看过后提出了修改）：\n" + "\n".join(
            f"  - {i['text']}" for i in run_input["inferred_intent"]))
    if run_input.get("pm_questions"):
        lines.append("- PM 的问题：\n" + "\n".join(
            f"  - [{q['id']}]{'（必答）' if q.get('required', True) else ''} {q['text']}" for q in run_input["pm_questions"]))
    if run_input.get("unanswered"):
        lines.append("- 用户没有回答这些问题，而是给出了上面的想法：\n" + "\n".join(f"  - {q}" for q in run_input["unanswered"]))
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


TASK_STATES = {"queued": "排队中", "completed": "完成", "cancelled": "取消"}
BRIEF_LIMIT = 2000


async def situation_line(kernel, run_input: dict[str, Any], task: dict[str, Any] | None) -> str:
    """When it is and who is speaking: Supervisor starts every run without memory (system map, 0.45.7)."""
    message_id = run_input.get("message_id") or (task or {}).get("source_message_id")
    message = await kernel.get_message(message_id) if message_id else None
    user = (message or {}).get("user_id")
    who = f"当前用户：{user}（用户画像中\"用户 {user}\"一节）" if user else "当前用户：未知（看画像中最相关的一节）"
    return f"当前时间：{datetime.now(ZoneInfo(DEFAULT_TZ)):%Y-%m-%d %H:%M}（{DEFAULT_TZ}）· {who}"


async def project_situation(kernel, settings, project_id: str, task: dict[str, Any] | None) -> str:
    """What else is going on in the project: other tasks (queued, recently ended, with their ledgers), the latest
    changes, and the industry brief's opening — material for reading what the user meant but has not said."""
    lines = ["## 项目近况（Kernel 汇总）"]
    others = await kernel.recent_tasks(project_id, exclude=(task or {}).get("task_id"))
    lines.append("### 其他任务（排队中的和最近结束的；需要细节时在只读沙箱里读它的状态簿）")
    lines += [f"- [{TASK_STATES.get(t['state'], t['state'])}] {t['goal']}（{t['kind']}"
              + (f"，结束原因 {t['close_reason']}" if t.get("close_reason") else "")
              + f"，{t['updated_at']:%m-%d}）· `runs/{t['task_id']}/state.md`" for t in others] or ["（无）"]
    changes = await kernel.iterations(project_id, days=180, limit=5)
    lines.append("### 最近的变更流水（细节用 list_iterations 查）\n" + (iterlog.render(changes, brief=True) or "（无）"))
    brief = await asyncio.to_thread(knowledge_text, settings, project_id, DOMAIN_BRIEF)
    if brief:
        cut = brief if len(brief) <= BRIEF_LIMIT else brief[:BRIEF_LIMIT] + "\n…（全文在 knowledge/domain-brief.md）"
        lines.append("### 行业速写（开头部分；注意日期和有效期）\n" + cut)
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
    sections = [f"# 项目 {project_id} · Supervisor（{PURPOSE_NAMES[purpose]}）",
                await situation_line(kernel, run_input, task)]
    project_charter = await asyncio.to_thread(charter, settings, project_id)
    if project_charter:
        sections.append("## 项目章程\n\n" + project_charter)
    profile = await asyncio.to_thread(knowledge_text, settings, project_id, USER_PROFILE)
    if profile:
        sections.append(PROFILE_HEADING + profile)
    sections.append(await project_situation(kernel, settings, project_id, task))
    sections.append("## 本项目对你的角色要求\n\n" + config.role_text)
    if config.skills_text:
        sections.append(config.skills_text)
    if task:
        sections.append(f"## 当前任务\n\n- 目标：{task['goal']}\n- 类型：{KIND_NAMES.get(task['kind'], task['kind'])}\n"
                        + (f"- 发起原因：{task['reason']}\n" if task.get("reason") else "")
                        + f"- 阶段：{task['stage']}\n- 状态：{task['state']}\n"
                        f"- 已执行 PM 回合：{task['turn_count']}\n\n### 可去的阶段（Kernel 计算）\n\n{allowed_moves(task)}")
        sections.append("## 已生效文档\n\n" + render_documents(settings, task))
        from assistant_kernel.core.executors.pm import STAGE_NAMES
        from assistant_kernel.core.executors.state import render_state
        sections.append("## 任务状态簿（Kernel 记录的事实：每轮的指令、用户的回答和补充、卡片操作；用户答过的不要再问）\n\n"
                        + render_state(task, await kernel.task_ledger(task["task_id"]), STAGE_NAMES))
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
        source = message["body"].get("handoff_from")
        title = f"从项目 {source} 转交来的请求（交接说明）" if source else "用户在 Supervisor 频道的发言"
        sections.append(f"## {title}\n\n{message['body'].get('text', '')}")
        if "routing" in caps:
            catalog = await asyncio.to_thread(project_catalog, settings, await kernel.active_projects(),
                                              exclude=project_id)
            sections.append("## 项目名录（Kernel 汇总，各项目章程的用途、范围和路由信息）\n\n" + catalog)
    sections.append(prompt_text(PURPOSE_PROMPT[purpose]))
    if "project_management" in caps and purpose == "evaluate":
        if task and task["kind"] == "bootstrap":   # a new project must not duplicate an existing one
            catalog = await asyncio.to_thread(project_catalog, settings, await kernel.active_projects(),
                                              exclude=project_id)
            sections.append("## 项目名录（Kernel 汇总，各项目章程的用途、范围和路由信息）\n\n" + catalog)
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
