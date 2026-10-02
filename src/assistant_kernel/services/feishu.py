from __future__ import annotations

import asyncio
import base64
from concurrent.futures import TimeoutError as FutureTimeout
import hashlib
import hmac
import json
import logging
import os
import re
import socket
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from assistant_kernel.config import KernelSettings
from assistant_kernel.repositories import CoreRepository
from assistant_kernel.services.agent_files import AgentFileLoader, RouteResolver
from assistant_kernel.services.audit import AUDIT_SCHEMA_VERSION, build_audit_prompt
from assistant_kernel.services.channel_health import (
    ChannelHealthReporter, channel_health_pulse, now_iso,
)
from assistant_kernel.services.project_credentials import ProjectCredentialsStore
from assistant_kernel.services.project_directory import ProjectDirectoryService
from assistant_kernel.services.current_artifacts import CurrentArtifactService
from assistant_kernel.services.document_runtime import TaskDocumentService
from assistant_kernel.services.goal_supervisor import GoalSupervisorService
from assistant_kernel.services.task_lifecycle import task_lifecycle_view


COMMAND_ALIASES = {
    "/bind-project": "/bind_project",
    "/unbind-project": "/unbind_project",
    "/kernel_status": "/kernel-status",
}

COMMANDS = {
    "/agents", "/agent", "/pm", "/butler", "/whoami",
    "/bind_project", "/unbind_project", "/binding",
    "/new", "/goal", "/resume", "/pending", "/status", "/kernel-status",
    "/audit", "/task-status", "/help",
    "/supervisor", "/prompt", "/stage", "/why",
}

TASK_STATUS_PHRASES = {
    "继续", "继续任务", "继续完成任务", "请继续", "请继续任务", "请继续完成任务",
    "任务状态", "当前任务状态",
}


def parse_slash_command(text: str) -> tuple[str, str | None] | None:
    value = re.sub(r"^(?:@_user_\d+\s*)+", "", text.strip())
    normalized = re.sub(r"[\s。！!]+", "", value)
    if normalized in TASK_STATUS_PHRASES:
        return "/task-status", None
    if not value.startswith("/"):
        return None
    parts = value.split(maxsplit=1)
    command = parts[0].split("@", 1)[0].lower()
    command = COMMAND_ALIASES.get(command, command)
    if command not in COMMANDS:
        return None
    argument = parts[1].strip() if len(parts) == 2 and parts[1].strip() else None
    return command, argument


def _extract_feishu_message_text(raw_content: Any) -> str:
    try:
        parsed = json.loads(raw_content) if isinstance(raw_content, str) else raw_content
    except json.JSONDecodeError:
        return str(raw_content)
    if not isinstance(parsed, dict):
        return str(parsed or "")
    if parsed.get("text") is not None:
        return str(parsed.get("text") or "")
    blocks = parsed.get("content") or parsed.get("content_v2") or []
    if not isinstance(blocks, list):
        return ""
    lines: list[str] = []
    for block in blocks:
        items = block if isinstance(block, list) else [block]
        parts: list[str] = []
        for item in items:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                value = item.get("text")
                if value is None and item.get("tag") == "at":
                    value = item.get("user_name")
                if value is not None:
                    parts.append(str(value))
        line = "".join(parts).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def _resolve_project_pm(
    settings: KernelSettings, repository: CoreRepository, project_id: str,
):
    cards = {
        str(item["project_id"]): item
        for item in ProjectDirectoryService(settings, repository).list()
    }
    card = cards.get(project_id)
    if card is None or str(card.get("status")) != "active":
        raise ValueError(f"Project不存在或未启用: {project_id}")
    target = AgentFileLoader(settings).resolve_agent(
        project_id,
        str(card.get("default_agent") or "pm"),
        require_feishu_slash=True,
    )
    if target.kind != "manager":
        raise ValueError("Project默认Agent不是可直接进入的PM")
    return card, target


async def _handle_slash_command(
    *, settings: KernelSettings, repository: CoreRepository,
    project_id: str, agent_key: str, channel: str, chat_id: str,
    user_id: str | None, external_event_id: str, external_message_id: str | None,
    text: str, raw: dict[str, Any], command: str, argument: str | None,
    chat_type: str = "",
    platform_label: str = "飞书",
    chat_label: str = "群",
    platform_chat_label: str | None = None,
    allow_chat_binding: bool = True,
    binding_chat_id: str | None = None,
    binding_inherited: bool = False,
    binding_block_reason: str | None = None,
) -> dict[str, Any]:
    platform_chat_label = platform_chat_label or f"{platform_label}{chat_label}聊"
    binding_chat_id = binding_chat_id or chat_id
    recorded = await repository.record_channel_command(
        project_id=project_id,
        agent_key=agent_key,
        channel=channel,
        chat_id=chat_id,
        user_id=user_id,
        external_event_id=external_event_id,
        external_message_id=external_message_id,
        content={
            "text": text,
            "command": command,
            "argument": argument,
            "raw": raw,
        },
    )
    inbound = recorded["message"]
    reply_extra: dict[str, Any] = {}
    selected_project = project_id
    selected_agent = agent_key
    loader = AgentFileLoader(settings)
    group_binding = (
        await repository.resolve_chat_project_binding(channel, binding_chat_id)
        if chat_type == "group" else None
    )
    if command == "/audit":
        mode = (argument or "full").strip().lower()
        try:
            prompt = build_audit_prompt(project_id, mode)
        except ValueError:
            reply = "用法: /audit [quick|full]"
        else:
            audit = await repository.ingest_message(
                project_id=project_id,
                agent_key=agent_key,
                channel=channel,
                chat_id=chat_id,
                user_id=user_id,
                external_event_id=f"{external_event_id}:audit:{mode}",
                external_message_id=None,
                content={
                    "schema_version": AUDIT_SCHEMA_VERSION,
                    "audit_mode": mode,
                    "audit_scope": "current_session_latest_round",
                    "command_message_id": str(inbound["message_id"]),
                    "text": prompt,
                },
                message_type="text",
                reply_to_message_id=None,
                auto_reply_to_unambiguous=False,
            )
            return {
                "message": audit["message"],
                "run": audit["run"],
                "outbound": None,
                "duplicate": bool(recorded["duplicate"] or audit["duplicate"]),
                "command": command,
                "audit_mode": mode,
            }
    elif command == "/task-status":
        task = await repository.current_project_task(project_id)
        if task is None:
            reply = "当前Project没有开放中的工程任务；可继续讨论并形成下一份Work Order。"
        else:
            stage = str(task.get("stage") or task.get("run_type") or "plan")
            reply = (
                "当前工程任务：\n"
                f"Run ID：{task['run_id']}\n"
                f"阶段：{stage}\n"
                f"状态：{task['status']}\n"
                "本消息只查询状态，不会创建、重试或切换任务。"
            )
    elif command == "/new":
        reset = await repository.reset_conversation(
            project_id=project_id,
            channel=channel,
            chat_id=chat_id,
            user_id=user_id,
            source_message_id=str(inbound["message_id"]),
            idempotency_key=f"conversation-reset:{channel}:{external_event_id}",
        )
        reply = (
            "已开始新的对话。Project绑定保持不变；旧消息和任务已经归档，"
            "不会自动进入新的上下文。可使用 /resume <run-id> 恢复旧待答任务。"
        )
        if reset["cancel_requested_run_ids"]:
            reply += " 当前前台回复已请求停止。"
    elif command == "/goal":
        if argument is None:
            current_goal = await repository.current_conversation_goal(
                project_id, channel, chat_id
            )
            reply = (
                f"当前目标：{current_goal['goal']}"
                if current_goal else "当前对话尚未设置显式目标。用法: /goal <目标>"
            )
        else:
            clear = argument.strip().lower() in {"clear", "reset", "清除", "清空"}
            await repository.record_conversation_goal(
                project_id=project_id,
                channel=channel,
                chat_id=chat_id,
                user_id=user_id,
                source_message_id=str(inbound["message_id"]),
                goal=None if clear else argument,
                idempotency_key=f"conversation-goal:{channel}:{external_event_id}",
            )
            reply = "当前目标已清除。" if clear else f"当前目标已更新：{argument}"
    elif command in {"/why", "/stage"}:
        current = await repository.current_project_task(project_id)
        if project_id == "system":
            reply = "Goal Supervisor只在具体Project中工作。请先进入或绑定Project。"
        elif current is None:
            reply = "当前Project没有进行中的Task。"
        elif command == "/stage":
            lifecycle = task_lifecycle_view(current)
            reply = (
                "当前Task生命周期：\n"
                f"阶段：{lifecycle['lifecycle']}\n"
                f"Run状态：{lifecycle['run_status']}\n"
                f"内部Stage：{lifecycle['current_stage']}\n"
                "本查询不会推进Supervisor对话或使卡片失效。"
            )
        else:
            supervisor = CurrentArtifactService(settings).read(
                project_id, str(current["run_id"]), "supervisor"
            )
            payload = dict((supervisor or {}).get("payload") or {})
            last = dict(payload.get("last_decision") or {})
            reasons = [str(item) for item in last.get("reason") or []]
            reply = (
                "Supervisor最近判定："
                + str(last.get("decision") or "尚无判定")
                + ("\n原因：" + "；".join(reasons[:3]) if reasons else "")
                + "\n本查询不会推进Supervisor对话或使卡片失效。"
            )
    elif command == "/supervisor":
        if project_id == "system":
            reply = "Goal Supervisor只在具体Project中工作。请先进入或绑定Project。"
        else:
            current = await repository.current_project_task(project_id)
            task_id = str(current["run_id"]) if current is not None else ""
            if not task_id:
                reply = "当前Project没有进行中的Task；请先在PM对话中发起任务后再咨询Supervisor。"
            else:
                source_context_epoch = await repository.advance_supervisor_context_epoch(
                    task_id,
                    reason="new_supervisor_message",
                    source_id=str(inbound["message_id"]),
                )
                source_turn_number = int(
                    (current.get("request_json") or {}).get("task_turn_count") or 0
                )
                run = await repository.create_run(
                    project_id=project_id,
                    agent_key=f"project:{project_id}:supervisor",
                    run_type="chat",
                    trigger_message_id=str(inbound["message_id"]),
                    request={
                        "interaction_kind": "supervisor_consultation",
                        "schema_version": "supervisor-consultation.v3",
                        "task_id": task_id,
                        "trigger": "user_consultation",
                        "turn_number": 0,
                        "source_context_epoch": source_context_epoch,
                        "source_turn_number": source_turn_number,
                        "source_message_id": str(inbound["message_id"]),
                        "interaction_head": {
                            "context_epoch": source_context_epoch,
                            "source_message_id": str(inbound["message_id"]),
                        },
                        "channel": channel, "chat_id": chat_id,
                        "user_id": user_id,
                        "user_request": argument or command,
                        "source_actor_type": "user",
                        "source_agent_key": None,
                        "source_run_id": None,
                        "source_interface_role": "supervisor",
                        "source_channel": channel,
                        "source_chat_id": chat_id,
                    },
                    idempotency_key=(
                        f"supervisor-consult:feishu:{external_event_id}"
                    ),
                )
                reply = (
                    "已收到，Supervisor正在基于当前Task状态评估，"
                    "结果将以状态消息返回。"
                )
                reply_extra = {
                    "origin": "supervisor",
                    "supervisor_decision": None,
                    "task_id": task_id,
                    "run_id": str(run["run_id"]),
                }
    elif command == "/prompt":
        task = await repository.current_project_task(project_id)
        if task is None:
            reply = "当前Project没有开放中的Task，因此没有Supervisor Action Plan。"
        else:
            action_ref = dict(
                ((task.get("request_json") or {}).get("accepted_documents") or {}).get(
                    "current_action_plan"
                ) or {}
            )
            action = (
                TaskDocumentService(settings).read(project_id, action_ref)
                if action_ref else None
            )
            payload = dict((action or {}).get("payload") or {})
            instruction = str(payload.get("pm_instruction") or "").strip()
            reply = (
                "当前Supervisor Action Plan：\n"
                f"- Decision：`{payload.get('decision')}`\n"
                f"- Work Mode：`{payload.get('target_work_mode')}`\n"
                f"- Confirmation：`{payload.get('confirmation_class')}`\n"
                + (("- PM instruction：\n```text\n" + instruction + "\n```") if instruction else "")
                if payload else "Supervisor尚未生成当前Action Plan。"
            )
    elif command == "/status":
        if argument:
            reply = "用法: /status"
        else:
            session = await repository.current_session(
                project_id, agent_key, channel, chat_id,
            )
            lines = [
                "当前 Session 状态：",
                f"Project：{project_id}",
                f"Agent：{agent_key}",
            ]
            if session is None:
                lines.extend((
                    "当前对话 Session：尚未建立",
                    "上下文余额：未知（首个 Run 执行后才会建立 Session）",
                ))
            else:
                latest_run = await repository.latest_session_run(
                    str(session["session_id"])
                )
                executor = dict((latest_run or {}).get("executor_json") or {})
                usage_history = list(executor.get("usage_history") or [])
                last_usage = dict(usage_history[-1]) if usage_history else {}
                lines.extend((
                    f"Kernel Session ID：{session['session_id']}",
                    f"Logical Session ID：{session['logical_session_id']}",
                    f"Codex Session ID：{session.get('native_session_id') or '尚未建立'}",
                    f"Session 状态：{session['status']}",
                    f"Parent Session ID：{session.get('parent_session_id') or '无'}",
                    f"Harness：{executor.get('harness') or 'codex_cli'}",
                    f"Provider：{executor.get('provider') or '未知'}",
                    f"Model：{executor.get('model') or '未知'}",
                    f"Reasoning：{executor.get('reasoning_effort') or '未知'}",
                    f"Context Policy：{executor.get('context_policy') or '未知'}",
                ))
                window = int(session.get("context_window_tokens") or 0)
                used = max(int(session.get("input_tokens") or 0), 0)
                if session.get("usage_known") is not False and window > 0:
                    remaining = max(window - used, 0)
                    used_ratio = min(used / window, 1.0)
                    lines.append(
                        "上下文（非账户额度）："
                        f"最近请求占用 {used:,} / {window:,} tokens "
                        f"({used_ratio:.1%})；估算余额 {remaining:,} tokens "
                        f"({1.0 - used_ratio:.1%})"
                    )
                else:
                    lines.append("上下文余额：未知（当前 Session 尚无可靠用量）")
                if last_usage:
                    lines.append(
                        "最近 Turn Usage："
                        f"input {int(last_usage.get('input_tokens') or 0):,} · "
                        f"cached {int(last_usage.get('cached_input_tokens') or 0):,} · "
                        f"output {int(last_usage.get('output_tokens') or 0):,} · "
                        f"reasoning {int(last_usage.get('reasoning_output_tokens') or 0):,}"
                    )
            reply = "\n".join(lines)
    elif command == "/kernel-status":
        if argument:
            reply = "用法: /kernel-status"
        else:
            runs = await repository.list_channel_run_status(
                project_id=project_id, channel=channel, chat_id=chat_id,
                user_id=user_id, limit=10,
            )
            lines = [
                "当前 Kernel 状态：",
                f"Project：{project_id}",
                f"Agent：{agent_key}",
            ]
            if not runs:
                lines.append("最近 Run：无")
            else:
                lines.append("最近 Run：")
                for item in runs:
                    target = str(item.get("stage") or item.get("run_type") or "run")
                    attempts = f"{int(item.get('attempt') or 0)}/{int(item.get('max_attempts') or 0)}"
                    lines.append(
                        f"- [{str(item['run_id'])[:8]}] {item['status']} · {target} · attempt {attempts}"
                    )
                if any(item["status"] in {
                    "waiting_user", "waiting_supervisor", "waiting_children", "blocked"
                } for item in runs):
                    lines.append("待处理任务可使用 /pending 查看，或使用 /resume 恢复。")
            reply = "\n".join(lines)
    elif command in {"/resume", "/pending"}:
        pending = await repository.list_pending_channel_runs(
            project_id=project_id, channel=channel, chat_id=chat_id,
            user_id=user_id, limit=20,
        )
        if command == "/pending" or not argument:
            if not pending:
                reply = "当前会话没有等待回答或阻塞的任务。"
            elif command == "/resume" and len(pending) == 1 and pending[0]["status"] == "waiting_user":
                argument = str(pending[0]["run_id"])
                reply = ""
            else:
                entries = []
                options = []
                for item in pending:
                    body = dict(item.get("question_content") or {})
                    summary = str(
                        body.get("question_text") or body.get("approval_question")
                        or body.get("summary") or body.get("text") or "待处理任务"
                    ).strip().replace("\n", " ")
                    label = summary[:80]
                    short_id = str(item["run_id"])[:8]
                    entries.append(
                        f"- [{short_id}] {item['status']} · {item.get('stage') or item.get('agent_key')}\n"
                        f"  {label}"
                    )
                    if item["status"] == "waiting_user":
                        options.append({
                            "id": str(item["run_id"]),
                            "label": f"{short_id} · {label}"[:100],
                            "description": str(item.get("stage") or item.get("agent_key") or "")[:100],
                        })
                reply = "当前会话待处理任务：\n" + "\n".join(entries)
                if options:
                    reply += "\n\n可使用 /resume <run-id>，或在交互菜单中选择。"
                    reply_extra = {
                        "interaction_kind": "resume_select",
                        "resume_options": options,
                        "component_expires_at": (
                            datetime.now(timezone.utc) + timedelta(days=7)
                        ).isoformat(),
                    }
        if command == "/resume" and argument:
            try:
                run_id = str(uuid.UUID(argument.strip()))
                resumed = await repository.resume_waiting_run(
                    run_id=run_id,
                    project_id=project_id,
                    channel=channel,
                    chat_id=chat_id,
                    user_id=user_id,
                    source_message_id=str(inbound["message_id"]),
                    idempotency_key=f"conversation-resume:{channel}:{external_event_id}",
                )
            except (PermissionError, ValueError) as exc:
                reply = f"无法恢复任务: {exc}"
            else:
                if resumed["blocked"]:
                    reply = (
                        f"Run {run_id} 仍处于blocked状态，需要先解决其权限或依赖问题。"
                    )
                else:
                    return {
                        "message": inbound,
                        "run": resumed["run"],
                        "outbound": resumed["message"],
                        "duplicate": bool(recorded["duplicate"] or resumed["duplicate"]),
                        "command": command,
                    }
    elif command == "/help":
        reply = (
            "可用指令：\n"
            "/new 开始新对话并保留旧任务\n"
            "/goal 查看目标；/goal <目标> 更新；/goal clear 清除\n"
            "/resume 自动发现或恢复旧待答任务\n"
            "/pending 查看当前会话待处理任务\n"
            "/status 查看当前 Session、Harness、模型与上下文余额\n"
            "/kernel-status 查看当前 Kernel 与最近 Run 状态\n"
            "/task-status 查看当前唯一工程任务（自然语言“继续任务”同义）\n"
            "/supervisor <问题> 咨询无状态Goal Supervisor\n"
            "/prompt 查看当前Supervisor Action Plan；/stage 或 /why 解释当前流程\n"
            "/audit 执行完整只读审计；/audit quick 执行快速审计\n"
            "/whoami 查看当前Project和Agent\n"
            f"/binding 查看{chat_label}绑定"
        )
    elif command == "/bind_project":
        if binding_block_reason:
            reply = binding_block_reason
        elif binding_inherited:
            reply = "Thread继承父频道Project，不能单独绑定；请在父频道执行绑定。"
        elif not allow_chat_binding:
            reply = "当前用户无权修改频道与Project的绑定。"
        elif chat_type != "group":
            reply = f"/bind_project只能在{platform_chat_label}中使用。"
        elif not argument:
            reply = "用法: /bind_project <project-id>"
        else:
            try:
                card, target = _resolve_project_pm(settings, repository, argument)
                bound = await repository.bind_chat_project(
                    channel=channel,
                    chat_id=binding_chat_id,
                    project_id=argument,
                    agent_key=target.agent_key,
                    bound_by_user_id=str(user_id),
                    source_message_id=str(inbound["message_id"]),
                    project_routing_hash=str(card.get("routing_hash") or ""),
                    idempotency_key=f"chat-bind:{channel}:{external_event_id}",
                )
            except (KeyError, PermissionError, ValueError) as exc:
                reply = f"绑定失败: {exc}"
            else:
                selected_project = argument
                selected_agent = target.agent_key
                reply = (
                    f"本{chat_label}{'已绑定' if bound['changed'] else '已经绑定'}Project: {argument}\n"
                    f"默认Agent: {target.display_name} ({target.agent_id})"
                )
    elif command == "/unbind_project":
        if binding_block_reason:
            reply = binding_block_reason
        elif binding_inherited:
            reply = "Thread继承父频道Project，不能单独解绑；请在父频道执行解绑。"
        elif not allow_chat_binding:
            reply = "当前用户无权修改频道与Project的绑定。"
        elif chat_type != "group":
            reply = f"/unbind_project只能在{platform_chat_label}中使用。"
        elif not argument:
            reply = "用法: /unbind_project <当前project-id>"
        else:
            try:
                unbound = await repository.unbind_chat_project(
                    channel=channel,
                    chat_id=binding_chat_id,
                    expected_project_id=argument,
                    unbound_by_user_id=str(user_id),
                    source_message_id=str(inbound["message_id"]),
                    idempotency_key=f"chat-unbind:{channel}:{external_event_id}",
                )
            except ValueError as exc:
                reply = f"解除绑定失败: {exc}"
            else:
                selected_project = "system"
                selected_agent = "system-butler"
                reply = (
                    f"已解除本{chat_label}与Project {argument}的绑定。"
                    if unbound["changed"] else f"本{chat_label}当前没有有效的Project绑定。"
                )
    elif command == "/binding":
        if chat_type != "group":
            reply = f"当前不是{platform_chat_label}；私聊使用大管家路由。"
        elif group_binding is None:
            reply = f"本{chat_label}尚未绑定Project。可使用 /bind_project <project-id>。"
        else:
            selected_project = str(group_binding["project_id"])
            selected_agent = str(group_binding["agent_key"])
            reply = (
                f"当前{chat_label}绑定Project: {selected_project}\n"
                f"默认Agent: {selected_agent}\n"
                f"chat_id: {binding_chat_id}"
            )
            if binding_inherited:
                reply += f"\n当前Thread继承父频道绑定；thread_id: {chat_id}"
    elif command == "/whoami":
        reply = f"当前Project: {project_id}\n当前Agent: {agent_key}"
        if chat_type == "group":
            reply += f"\n{chat_label}绑定: {group_binding['project_id'] if group_binding else '未绑定'}"
            if binding_inherited:
                reply += f"\nThread继承父频道: {binding_chat_id}"
    elif command == "/butler":
        if chat_type == "group" and group_binding is not None:
            reply = f"Project{chat_label}不能切换到大管家；请在机器人私聊中使用大管家。"
        else:
            selected_project = "system"
            selected_agent = "system-butler"
            await repository.set_route_binding(
                channel=channel,
                chat_id=chat_id,
                user_id=user_id,
                project_id=selected_project,
                agent_key=selected_agent,
                reason=f"{channel}_slash_butler",
                source_message_id=str(inbound["message_id"]),
                idempotency_key=f"command-route:{channel}:{external_event_id}",
            )
            reply = (
                "Hi，我是你的个人超级助理大管家。\n"
                "现在已回到大管家总入口，有什么需要？"
            )
    elif command == "/agents":
        if project_id == "system":
            reply = "当前位于大管家。请先进入一个Project，再使用 /agents。"
        else:
            agents = [
                item for item in loader.list_agents(project_id)
                if item.status == "active" and item.entrypoints.get("feishu_slash", False)
            ]
            if not agents:
                reply = f"当前Project没有允许{platform_label}直接进入的Agent。"
            else:
                reply = "可用Agent:\n" + "\n".join(
                    f"- {item.agent_id}: {item.display_name}" for item in agents
                )
    elif command == "/pm":
        if chat_type == "group" and group_binding is not None:
            selected_project = str(group_binding["project_id"])
            selected_agent = str(group_binding["agent_key"])
            reply = (
                f"Hi，我是 Project {selected_project} 的 Project Manager。\n"
                "当前群已固定进入这个项目，有什么需要？"
            )
        elif project_id == "system":
            selected_agent = "system-butler"
            reply = "当前已在大管家。"
        else:
            try:
                selected_agent = loader.resolve_agent(project_id, "pm").agent_key
            except KeyError:
                selected_agent = f"project:{project_id}:manager"
            await repository.set_route_binding(
                channel=channel,
                chat_id=chat_id,
                user_id=user_id,
                project_id=project_id,
                agent_key=selected_agent,
                reason=f"{channel}_slash_pm",
                source_message_id=str(inbound["message_id"]),
                idempotency_key=f"command-route:{channel}:{external_event_id}",
            )
            reply = (
                f"Hi，我是 Project {project_id} 的 Project Manager。\n"
                "现在已进入这个项目，有什么需要？"
            )
    else:
        if chat_type == "group" and group_binding is not None:
            reply = f"Project{chat_label}不允许直接切换Agent；请由PM启动正式Stage。"
        elif not argument:
            reply = "用法: /agent <agent-id或alias>"
        elif project_id == "system":
            reply = "当前位于大管家，不能直接跨Project切换Agent。请先进入目标Project。"
        else:
            try:
                target = loader.resolve_agent(
                    project_id, argument, require_feishu_slash=True,
                )
            except (KeyError, PermissionError, ValueError) as exc:
                reply = f"无法切换Agent: {exc}"
            else:
                selected_agent = target.agent_key
                await repository.set_route_binding(
                    channel=channel,
                    chat_id=chat_id,
                    user_id=user_id,
                    project_id=project_id,
                    agent_key=selected_agent,
                    reason=f"{channel}_slash_agent",
                    source_message_id=str(inbound["message_id"]),
                    idempotency_key=f"command-route:{channel}:{external_event_id}",
                )
                reply = f"已切换到Agent: {target.display_name} ({target.agent_id})"

    outbound = await repository.create_outbound(
        project_id=selected_project,
        session_id=None,
        run_id=None,
        channel=channel,
        chat_id=chat_id,
        user_id=user_id,
        message_type="reply",
        content={
            "text": reply, "command": command, "agent_key": selected_agent,
            "origin": "kernel", "context_visibility": "ui_only",
            **reply_extra,
        },
        reply_to_message_id=str(inbound["message_id"]),
        idempotency_key=f"command-reply:{channel}:{external_event_id}",
    )
    return {
        "message": inbound,
        "run": None,
        "outbound": outbound,
        "duplicate": bool(recorded["duplicate"]),
        "command": command,
    }


def load_feishu_config(settings: KernelSettings) -> dict[str, Any]:
    if not settings.service_credentials_path.is_file():
        return {}
    data = json.loads(settings.service_credentials_path.read_text(encoding="utf-8-sig"))
    config = dict((data.get("services") or {}).get("feishu") or {})
    mode = str(os.getenv("FEISHU_MODE") or config.get("mode") or "full").strip().lower()
    if mode not in {"full", "standby"}:
        raise ValueError("Feishu mode must be 'full' or 'standby'")
    config["mode"] = mode
    return config


def verify_callback(config: dict[str, Any], payload: dict[str, Any]) -> None:
    expected = str(config.get("verification_token") or "")
    if not expected:
        raise RuntimeError("Feishu verification_token is not configured")
    supplied = str(((payload.get("header") or {}).get("token") or payload.get("token") or ""))
    if not hmac.compare_digest(expected, supplied):
        raise PermissionError("Feishu callback token mismatch")


async def ingest_event(
    settings: KernelSettings, payload: dict[str, Any], app_id: str | None = None,
    allow_explicit_route: bool = False,
) -> dict[str, Any]:
    feishu_config = load_feishu_config(settings)
    event = payload.get("event") or {}
    header = payload.get("header") or {}
    event_id = str(header.get("event_id") or payload.get("event_id") or "")
    if not event_id:
        raise ValueError("Feishu event_id is required")
    message = event.get("message") or {}
    action = event.get("action") or {}
    context = event.get("context") or {}
    sender_record = event.get("operator") if action else (event.get("sender") or {})
    sender_type = str((sender_record or {}).get("sender_type") or "")
    if not action and sender_type == "app":
        return {"ignored": True, "reason": "app_sender"}
    sender = sender_record if action else (sender_record or {}).get("sender_id")
    sender = sender or {}
    user_id = str(sender.get("open_id") or sender.get("user_id") or "") or None
    chat_id = str(context.get("open_chat_id") if action else message.get("chat_id") or "")
    if not chat_id:
        raise ValueError("Feishu chat_id is required")
    external_message_id = str(message.get("message_id") or "") or None
    chat_type = str(message.get("chat_type") or "")
    reply_external_id = str(
        context.get("open_message_id") if action
        else message.get("parent_id") or message.get("root_id") or ""
    ) or None
    text = ""
    if action:
        value = action.get("value") or {}
        text = str(value.get("text") or value.get("answer") or value.get("decision") or "")
        content: dict[str, Any] = {"text": text, "action": value, "raw": payload}
    else:
        raw_content = message.get("content") or "{}"
        text = _extract_feishu_message_text(raw_content)
        content = {"text": text, "raw": payload}

    slash_candidate = parse_slash_command(text) if not action else None
    repository = CoreRepository(settings)
    reply_to_message_id = None
    message_type = "action" if action else "text"
    before_resume = None
    original = None
    group_binding = None
    if reply_external_id and slash_candidate is None:
        async with repository.database.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT m.message_id,m.message_type,m.project_id,m.session_id,m.run_id,
                       m.content_json,
                       m.chat_id,m.user_id,r.agent_key
                FROM assistant.messages m
                LEFT JOIN assistant.runs r ON r.run_id=m.run_id
                WHERE m.channel='feishu' AND m.external_message_id=%s
                  AND m.direction='outbound' AND m.chat_id=%s
                  AND (m.user_id IS NULL OR m.user_id=%s)
                ORDER BY sent_at DESC NULLS LAST LIMIT 1
                """,
                (reply_external_id, chat_id, user_id),
            )
            original = await cursor.fetchone()
        if original:
            reply_to_message_id = str(original["message_id"])
            if original["message_type"] in {"question", "approval"}:
                message_type = "answer"
            else:
                reply_to_message_id = None
        else:
            raise ValueError("the replied Feishu message is not a known outbound message")
    if action and original is None:
        raise PermissionError("card action target is unknown or unauthorized")

    if original:
        project_id = str(original["project_id"])
        agent_key = str(original["agent_key"] or (
            "system-butler" if project_id == "system"
            else f"project:{project_id}:manager"
        ))
        if action:
            claimed = action.get("value") or {}
            if (
                original["message_type"] == "question"
                and claimed.get("kind") not in {"question_choice", "credential_submit"}
            ):
                raise PermissionError("question card has no supported action kind")
            for key, actual in (
                ("question_message_id", original["message_id"]),
                ("project_id", original["project_id"]),
                ("session_id", original["session_id"]),
                ("run_id", original["run_id"]),
            ):
                if claimed.get(key) is not None and str(claimed[key]) != str(actual):
                    raise PermissionError("Feishu action correlation does not match its message")
            if claimed.get("kind") == "question_choice":
                if original["message_type"] != "question":
                    raise PermissionError("a choice action must target a question card")
                for key, actual in (
                    ("question_message_id", original["message_id"]),
                    ("project_id", original["project_id"]),
                    ("run_id", original["run_id"]),
                ):
                    if str(claimed.get(key) or "") != str(actual):
                        raise PermissionError("question card action is missing its correlation")
                result = await repository.record_question_choice(
                    project_id=project_id, channel="feishu", chat_id=chat_id,
                    user_id=str(user_id or ""),
                    external_event_id=f"{app_id or 'default'}:{event_id}",
                    question_message_id=str(original["message_id"]),
                    run_id=str(original["run_id"]),
                    question_id=str(claimed.get("question_id") or ""),
                    interaction_seq=int(claimed.get("interaction_seq") or -1),
                    choice_id=str(claimed.get("choice_id") or ""),
                )
                return {"message": None, "run": None, **result}
            if claimed.get("kind") == "credential_submit":
                if original["message_type"] != "question":
                    raise PermissionError("a credential form must target a question")
                original_content = dict(original.get("content_json") or {})
                input_schema = dict(original_content.get("input_schema") or {})
                if input_schema.get("kind") != "project_credential":
                    raise PermissionError("credential form target is not a credential request")
                request_id = str(claimed.get("request_id") or "")
                if (
                    request_id != str(input_schema.get("request_id") or "")
                    or str(claimed.get("credential_key") or "")
                    != str(input_schema.get("credential_key") or "")
                ):
                    raise PermissionError("credential form does not match its request")
                credential_store = ProjectCredentialsStore(settings.data_root)
                credential_result: dict[str, Any] = {}

                def save_credential() -> dict[str, Any]:
                    result = credential_store.complete_request(
                        project_id=project_id,
                        request_id=request_id,
                        fields=dict(action.get("form_value") or {}),
                        external_event_id=f"{app_id or 'default'}:{event_id}",
                    )
                    credential_result.update(result)
                    return result

                before_resume = save_credential
                text = f"Project凭据已保存：{input_schema['credential_key']}"
                content = {
                    "text": text,
                    "action": {
                            "kind": "credential_submit",
                            "request_id": request_id,
                            "credential_key": input_schema["credential_key"],
                    },
                    "credential_result": {
                        "credential_key": input_schema["credential_key"],
                        "status": "pending_commit",
                    },
                }
    else:
        explicit = (payload.get("kernel") or {}) if allow_explicit_route else {}
        if chat_type == "group":
            group_binding = await repository.resolve_chat_project_binding("feishu", chat_id)
            if group_binding is None:
                project_id, agent_key = "system", "system-butler"
            else:
                project_id = str(group_binding["project_id"])
                try:
                    _, target = _resolve_project_pm(settings, repository, project_id)
                except (KeyError, PermissionError, ValueError):
                    project_id, agent_key = "system", "system-butler"
                    group_binding = None
                else:
                    agent_key = target.agent_key
        elif explicit.get("project_id"):
            project_id, agent_key = await RouteResolver(settings, repository).resolve(
                "feishu", chat_id, user_id,
                explicit.get("project_id"), explicit.get("agent_key"),
            )
        else:
            project_id, agent_key = await RouteResolver(settings, repository).resolve(
                "feishu", chat_id, user_id,
            )
    slash = slash_candidate if original is None and message_type == "text" else None
    if feishu_config.get("mode") == "standby":
        allowed_slash = bool(
            slash is not None
            and slash[0] in {"/help", "/pending", "/resume", "/whoami"}
        )
        allowed_answer = bool(
            action
            or (
                original is not None
                and str(original.get("message_type") or "")
                in {"question", "approval"}
            )
        )
        if not allowed_slash and not allowed_answer:
            recorded = await repository.record_channel_command(
                project_id="system",
                agent_key="system-butler",
                channel="feishu",
                chat_id=chat_id,
                user_id=user_id,
                external_event_id=f"{app_id or 'default'}:{event_id}",
                external_message_id=external_message_id,
                content={"text": text, "reason": "feishu_standby", "raw": payload},
            )
            outbound = await repository.create_outbound(
                project_id="system",
                session_id=None,
                run_id=None,
                channel="feishu",
                chat_id=chat_id,
                user_id=user_id,
                message_type="reply",
                content={
                    "text": (
                        "飞书当前为备用渠道：保留通知、审批、/pending、/resume 和 /whoami。"
                        "普通项目对话请在 Discord 继续。"
                    )
                },
                reply_to_message_id=str(recorded["message"]["message_id"]),
                idempotency_key=(
                    f"feishu-standby-reply:{app_id or 'default'}:{event_id}"
                ),
            )
            return {
                "message": recorded["message"],
                "run": None,
                "outbound": outbound,
                "duplicate": bool(recorded["duplicate"]),
                "standby": True,
            }
    if slash is not None:
        command, argument = slash
        return await _handle_slash_command(
            settings=settings,
            repository=repository,
            project_id=project_id,
            agent_key=agent_key,
            channel="feishu",
            chat_id=chat_id,
            user_id=user_id,
            external_event_id=f"{app_id or 'default'}:{event_id}",
            external_message_id=external_message_id,
            text=text,
            raw=payload,
            command=command,
            argument=argument,
            chat_type=chat_type,
            allow_chat_binding=(
                not set(feishu_config.get("owner_open_ids") or [])
                or str(user_id or "")
                in set(feishu_config.get("owner_open_ids") or [])
            ),
        )
    if chat_type == "group" and group_binding is None and original is None:
        recorded = await repository.record_channel_command(
            project_id="system",
            agent_key="system-butler",
            channel="feishu",
            chat_id=chat_id,
            user_id=user_id,
            external_event_id=f"{app_id or 'default'}:{event_id}",
            external_message_id=external_message_id,
            content={
                "text": text,
                "reason": "unbound_group",
                "raw": payload,
            },
        )
        outbound = await repository.create_outbound(
            project_id="system",
            session_id=None,
            run_id=None,
            channel="feishu",
            chat_id=chat_id,
            user_id=user_id,
            message_type="reply",
            content={
                "text": "本群尚未绑定Project。请发送 /bind_project <project-id>。"
            },
            reply_to_message_id=str(recorded["message"]["message_id"]),
            idempotency_key=f"unbound-group-reply:feishu:{app_id or 'default'}:{event_id}",
        )
        return {
            "message": recorded["message"],
            "run": None,
            "outbound": outbound,
            "duplicate": bool(recorded["duplicate"]),
            "unbound_group": True,
        }
    return await repository.ingest_message(
        project_id=project_id,
        agent_key=agent_key,
        channel="feishu",
        chat_id=chat_id,
        user_id=user_id,
        external_event_id=f"{app_id or 'default'}:{event_id}",
        external_message_id=external_message_id,
        content=content,
        message_type=message_type,
        reply_to_message_id=reply_to_message_id,
        auto_reply_to_unambiguous=bool(message_type == "text" and reply_to_message_id is None),
        before_resume=before_resume,
    )


def render_text(message: dict[str, Any]) -> str:
    content = message["content_json"] or {}
    text = str(content.get("text") or "")
    if text:
        return text
    return json.dumps(content, ensure_ascii=False)


def render_feishu_message(message: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Render normal conversation as text and true approvals as interactive cards."""
    text = render_text(message)
    message_type = str(message.get("message_type") or "")
    if message_type not in {"approval", "question"}:
        return "text", {"text": text}
    content = dict(message.get("content_json") or {})
    correlation = dict(content.get("correlation") or {})
    common = {
        "project_id": str(message.get("project_id") or correlation.get("project_id") or ""),
        "run_id": str(message.get("run_id") or correlation.get("run_id") or ""),
        "question_message_id": str(message.get("message_id") or ""),
    }
    if message_type == "question":
        input_schema = dict(content.get("input_schema") or {})
        if input_schema.get("kind") == "project_credential":
            request_id = str(input_schema.get("request_id") or "")
            credential_key = str(input_schema.get("credential_key") or "")
            fields = [
                dict(item) for item in input_schema.get("fields") or []
                if isinstance(item, dict)
            ]
            if (
                not request_id or not credential_key or not fields
                or any(not item.get("name") or not item.get("label") for item in fields)
            ):
                return "text", {"text": text}
            form_elements = [
                {
                    "tag": "input",
                    "name": str(item["name"]),
                    "label": {
                        "tag": "plain_text", "content": str(item["label"]),
                    },
                    "placeholder": {
                        "tag": "plain_text", "content": f"请输入 {item['label']}",
                    },
                    "required": bool(item.get("required", True)),
                }
                for item in fields
            ]
            form_elements.append({
                "tag": "button",
                "name": "credential_submit",
                "type": "primary",
                "action_type": "form_submit",
                "text": {"tag": "lark_md", "content": "保存并继续"},
                "value": {
                    **common,
                    "kind": "credential_submit",
                    "question_id": str(content.get("question_id") or ""),
                    "interaction_seq": content.get("interaction_seq"),
                    "request_id": request_id,
                    "credential_key": credential_key,
                },
            })
            return "interactive", {
                "config": {"wide_screen_mode": True},
                "header": {
                    "template": "blue",
                    "title": {"tag": "plain_text", "content": "需要Project凭据"},
                },
                "elements": [
                    {"tag": "markdown", "content": text},
                    {
                        "tag": "form",
                        "name": f"credential_{request_id}",
                        "elements": form_elements,
                    },
                ],
            }
        options = [dict(item) for item in content.get("options") or [] if isinstance(item, dict)]
        if (
            not 2 <= len(options) <= 3
            or any(not item.get("id") or not item.get("label") for item in options)
        ):
            return "text", {"text": text}
        question_id = str(content.get("question_id") or "")
        sequence = content.get("interaction_seq")
        if not question_id or sequence is None:
            return "text", {"text": text}
        return "interactive", {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": "blue",
                "title": {"tag": "plain_text", "content": "需要回答"},
            },
            "elements": [
                {"tag": "markdown", "content": text},
                {"tag": "action", "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": str(option.get("label") or "")},
                        "value": {
                            **common,
                            "kind": "question_choice",
                            "question_id": question_id,
                            "interaction_seq": sequence,
                            "choice_id": str(option.get("id") or ""),
                        },
                    }
                    for option in options
                ]},
            ],
        }
    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "orange",
            "title": {"tag": "plain_text", "content": "需要批准"},
        },
        "elements": [
            {"tag": "markdown", "content": text},
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "type": "primary",
                        "text": {"tag": "plain_text", "content": "批准进入下一阶段"},
                        "value": {**common, "decision": "approve"},
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "继续修改"},
                        "value": {**common, "decision": "revise"},
                    },
                    {
                        "tag": "button",
                        "type": "danger",
                        "text": {"tag": "plain_text", "content": "拒绝"},
                        "value": {**common, "decision": "reject"},
                    },
                ],
            },
        ],
    }
    return "interactive", card


class FeishuSender:
    def __init__(
        self, settings: KernelSettings, repository: CoreRepository,
        config: dict[str, Any], client: httpx.AsyncClient | None = None,
    ):
        self.settings = settings
        self.repository = repository
        self.config = config
        self.client = client

    async def send(self, message: dict[str, Any], worker_instance_id: str) -> None:
        app_id = str(self.config.get("app_id") or "")
        app_secret = str(self.config.get("app_secret") or "")
        if not app_id or not app_secret or not self.config.get("enabled"):
            raise RuntimeError("Feishu sender is not enabled")
        if not await self.repository.confirm_delivery_current(
            str(message["message_id"]), worker_instance_id
        ):
            return
        client = self.client or httpx.AsyncClient(base_url="https://open.feishu.cn", timeout=20)
        try:
            response = await client.post(
                "/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": app_id, "app_secret": app_secret},
            )
            response.raise_for_status()
            token_data = response.json()
            token = token_data.get("tenant_access_token")
            if token_data.get("code") != 0 or not token:
                raise RuntimeError("Feishu token request failed")
            msg_type, content = render_feishu_message(message)
            text_content = {"text": render_text(message)}
            reply_external_id = await self.repository.delivery_reply_external_id(
                str(message["message_id"])
            )
            delivery_mode = "chat"
            reply_failure = None

            async def send_to_chat(
                selected_type: str = msg_type,
                selected_content: dict[str, Any] = content,
            ) -> httpx.Response:
                return await client.post(
                    "/open-apis/im/v1/messages",
                    params={"receive_id_type": "chat_id"},
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "receive_id": message["chat_id"],
                        "msg_type": selected_type,
                        "content": json.dumps(selected_content, ensure_ascii=False),
                        "uuid": str(message["message_id"]),
                    },
                )

            if reply_external_id:
                response = await client.post(
                    f"/open-apis/im/v1/messages/{reply_external_id}/reply",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "msg_type": msg_type,
                        "content": json.dumps(content, ensure_ascii=False),
                        "reply_in_thread": False,
                        "uuid": str(message["message_id"]),
                    },
                )
                reply_data = {}
                try:
                    reply_data = response.json()
                except (json.JSONDecodeError, ValueError):
                    pass
                rejected_reply = (
                    response.status_code in {400, 404}
                    or (response.is_success and reply_data.get("code") not in {None, 0})
                )
                if rejected_reply:
                    reply_failure = {
                        "status_code": response.status_code,
                        "code": reply_data.get("code"),
                        "response_sha256": hashlib.sha256(response.content).hexdigest(),
                    }
                    response = await send_to_chat()
                    delivery_mode = "chat_fallback"
                else:
                    delivery_mode = "reply"
            else:
                response = await send_to_chat()
            data = {}
            try:
                data = response.json()
            except (json.JSONDecodeError, ValueError):
                pass
            if msg_type == "interactive" and (
                not response.is_success
                or data.get("code") not in {None, 0}
                or not (data.get("data") or {}).get("message_id")
            ):
                reply_failure = {
                    "status_code": response.status_code,
                    "code": data.get("code"),
                    "response_sha256": hashlib.sha256(response.content).hexdigest(),
                }
                response = await send_to_chat("text", text_content)
                delivery_mode = (
                    "question_text_fallback"
                    if message.get("message_type") == "question"
                    else "approval_text_fallback"
                )
                data = response.json()
            response.raise_for_status()
            external_id = (data.get("data") or {}).get("message_id")
            if data.get("code") != 0 or not external_id:
                raise RuntimeError("Feishu message send failed")
            accepted = await self.repository.acknowledge_delivery(
                str(message["message_id"]), worker_instance_id, str(external_id),
                {
                    "app_id": app_id,
                    "mode": delivery_mode,
                    "msg_type": (
                        "text" if delivery_mode in {
                            "approval_text_fallback", "question_text_fallback"
                        } else msg_type
                    ),
                    "reply_external_id": reply_external_id,
                    "reply_failure": reply_failure,
                    "response_sha256": hashlib.sha256(response.content).hexdigest(),
                },
            )
            if not accepted:
                raise RuntimeError("delivery lease was lost before acknowledgment")
        finally:
            if self.client is None:
                await client.aclose()


async def channel_worker_loop(
    settings: KernelSettings, health: ChannelHealthReporter | None = None,
) -> None:
    repository = CoreRepository(settings)
    health = health or ChannelHealthReporter(settings, "feishu")
    worker_id = f"{socket.gethostname()}:{os.getpid()}:feishu"
    worker_instance_id = str(uuid.uuid4())
    while True:
        config = load_feishu_config(settings)
        if not config.get("enabled"):
            await asyncio.sleep(max(settings.worker_poll_seconds, 1.0))
            continue
        try:
            message = await repository.claim_delivery(
                worker_id, worker_instance_id, 90, channel="feishu"
            )
            if message is None:
                await asyncio.sleep(settings.worker_poll_seconds)
                continue
            try:
                await FeishuSender(settings, repository, config).send(message, worker_instance_id)
                health.update(last_delivery_at=now_iso(), last_error_type=None)
            except Exception as exc:
                await repository.fail_delivery(
                    str(message["message_id"]), worker_instance_id, str(exc)
                )
                logging.exception("Feishu delivery failed")
                health.update(last_error_type=type(exc).__name__, last_error_at=now_iso())
        except Exception:
            logging.exception("Feishu worker loop failed")
            await asyncio.sleep(settings.worker_poll_seconds)


def _websocket_message_payload(event: Any) -> dict[str, Any] | None:
    event_data = getattr(event, "event", None)
    header = getattr(event, "header", None)
    if event_data is None or header is None:
        return None
    sender = getattr(event_data, "sender", None)
    if sender is None or getattr(sender, "sender_type", "") == "app":
        return None
    message = getattr(event_data, "message", None)
    if message is None:
        return None
    sender_id = getattr(sender, "sender_id", None)
    return {
        "header": {
            "event_id": getattr(header, "event_id", "") or "",
            "event_type": getattr(header, "event_type", "") or "",
            "app_id": getattr(header, "app_id", "") or "",
            "tenant_key": getattr(header, "tenant_key", "") or "",
        },
        "event": {
            "sender": {
                "sender_type": getattr(sender, "sender_type", "") or "",
                "sender_id": {
                    "open_id": getattr(sender_id, "open_id", "") or "",
                    "user_id": getattr(sender_id, "user_id", "") or "",
                }
            },
            "message": {
                "message_id": getattr(message, "message_id", "") or "",
                "chat_id": getattr(message, "chat_id", "") or "",
                "chat_type": getattr(message, "chat_type", "") or "",
                "parent_id": getattr(message, "parent_id", "") or "",
                "root_id": getattr(message, "root_id", "") or "",
                "message_type": getattr(message, "message_type", "") or "",
                "content": getattr(message, "content", "") or "{}",
            },
        },
    }


def _websocket_card_action_payload(event: Any) -> dict[str, Any] | None:
    event_data = getattr(event, "event", None)
    header = getattr(event, "header", None)
    if event_data is None or header is None:
        return None
    operator = getattr(event_data, "operator", None)
    action = getattr(event_data, "action", None)
    context = getattr(event_data, "context", None)
    if operator is None or action is None or context is None:
        return None
    value = dict(getattr(action, "value", None) or {})
    form_value = dict(getattr(action, "form_value", None) or {})
    event_id = str(getattr(header, "event_id", "") or "")
    if not event_id:
        event_id = "card:" + hashlib.sha256(json.dumps(
            {
                "open_message_id": getattr(context, "open_message_id", "") or "",
                "open_id": getattr(operator, "open_id", "") or "",
                "value": value,
                "form_value": form_value,
            }, ensure_ascii=False, sort_keys=True,
        ).encode("utf-8")).hexdigest()
    return {
        "header": {
            "event_id": event_id,
            "event_type": getattr(header, "event_type", "") or "card.action.trigger",
            "app_id": getattr(header, "app_id", "") or "",
            "tenant_key": getattr(header, "tenant_key", "") or "",
        },
        "event": {
            "operator": {
                "sender_type": "user",
                "open_id": getattr(operator, "open_id", "") or "",
                "user_id": getattr(operator, "user_id", "") or "",
            },
            "action": {
                "value": value,
                "form_value": form_value,
                "tag": getattr(action, "tag", "") or "",
                "name": getattr(action, "name", "") or "",
            },
            "context": {
                "open_message_id": getattr(context, "open_message_id", "") or "",
                "open_chat_id": getattr(context, "open_chat_id", "") or "",
            },
        },
    }


def enable_card_frame_dispatch(client: Any, ws: Any) -> None:
    """Bridge CARD frames dropped by lark-oapi 1.7.3; leave EVENT handling intact."""
    original = client._handle_data_frame

    async def handle(frame: Any) -> None:
        if ws._get_by_key(frame.headers, ws.HEADER_TYPE) != ws.MessageType.CARD.value:
            await original(frame)
            return
        headers = frame.headers
        payload = frame.payload
        if int(ws._get_by_key(headers, ws.HEADER_SUM)) > 1:
            payload = client._combine(
                ws._get_by_key(headers, ws.HEADER_MESSAGE_ID),
                int(ws._get_by_key(headers, ws.HEADER_SUM)),
                int(ws._get_by_key(headers, ws.HEADER_SEQ)), payload,
            )
            if payload is None:
                return
        response = ws.Response(code=ws.http.HTTPStatus.OK)
        try:
            started = time.monotonic()
            result = client._event_handler._do_without_validation(payload)
            header = headers.add()
            header.key = ws.HEADER_BIZ_RT
            header.value = str(int((time.monotonic() - started) * 1000))
            if result is not None:
                response.data = base64.b64encode(ws.JSON.marshal(result).encode(ws.UTF_8))
        except Exception:
            logging.exception("Feishu card frame dispatch failed")
            response = ws.Response(code=ws.http.HTTPStatus.INTERNAL_SERVER_ERROR)
        frame.payload = ws.JSON.marshal(response).encode(ws.UTF_8)
        await client._write_message(frame.SerializeToString())

    client._handle_data_frame = handle


async def feishu_gateway_loop(
    settings: KernelSettings, health: ChannelHealthReporter | None = None,
) -> None:
    """Receive Feishu messages over the official outbound WebSocket connection."""
    health = health or ChannelHealthReporter(settings, "feishu")
    while True:
        config = load_feishu_config(settings)
        if not config.get("enabled") or config.get("receive_mode") != "websocket":
            health.update(connected=False, enabled=False)
            await asyncio.sleep(max(settings.worker_poll_seconds, 2.0))
            continue
        app_id = str(config.get("app_id") or "")
        app_secret = str(config.get("app_secret") or "")
        if not app_id or not app_secret:
            health.update(connected=False, enabled=True, last_error_type="MissingCredential")
            await asyncio.sleep(max(settings.worker_poll_seconds, 2.0))
            continue
        main_loop = asyncio.get_running_loop()
        try:
            from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
            from lark_oapi.event.callback.model.p2_card_action_trigger import (
                P2CardActionTriggerResponse,
            )
            from lark_oapi.core.enum import LogLevel
            import lark_oapi.ws.client as lark_ws

            def report(result_future) -> None:
                try:
                    result_future.result()
                except Exception:
                    logging.exception("Feishu WebSocket event ingestion failed")

            def submit_payload(payload: dict[str, Any] | None) -> None:
                if payload is None or not str((payload.get("header") or {}).get("event_id") or ""):
                    return
                health.update(last_event_at=now_iso())
                future = asyncio.run_coroutine_threadsafe(
                    ingest_event(settings, payload, app_id), main_loop
                )
                future.add_done_callback(report)

            def handle_message(event: Any) -> None:
                submit_payload(_websocket_message_payload(event))

            def handle_card_action(event: Any) -> Any:
                payload = _websocket_card_action_payload(event)
                value = (((payload or {}).get("event") or {}).get("action") or {}).get("value") or {}
                if value.get("kind") != "question_choice":
                    submit_payload(payload)
                    content = "已收到，正在处理。"
                elif payload is None:
                    content = "卡片信息不完整，请回复问题消息。"
                else:
                    future = asyncio.run_coroutine_threadsafe(
                        ingest_event(settings, payload, app_id), main_loop
                    )
                    try:
                        outcome = future.result(timeout=1.5)
                    except FutureTimeout:
                        future.add_done_callback(report)
                        content = "选择尚未确认，请稍后查看结果或回复文字。"
                    except Exception:
                        logging.exception("Feishu question choice was not recorded")
                        content = "选择未记录，请重试或回复问题消息。"
                    else:
                        status = str(outcome.get("choice_status") or "")
                        content = {
                            "recorded": "选择已记录。",
                            "already_answered": "此题已回答，重复点击未更改答案。",
                            "expired": "这张卡片已失效，请使用 /resume 恢复旧任务。",
                        }.get(status, "选择尚未确认，请回复问题消息。")
                return P2CardActionTriggerResponse({
                    "toast": {"type": "info", "content": content}
                })

            dispatcher = (
                EventDispatcherHandler.builder(
                    str(config.get("encrypt_key") or ""),
                    str(config.get("verification_token") or ""),
                    LogLevel.WARNING,
                )
                .register_p2_im_message_receive_v1(handle_message)
                .register_p2_card_action_trigger(handle_card_action)
                .build()
            )

            def run_client() -> None:
                thread_loop = asyncio.new_event_loop()
                asyncio.set_event_loop(thread_loop)
                lark_ws.loop = thread_loop
                class HealthAwareClient(lark_ws.Client):
                    async def _connect(self) -> None:
                        await super()._connect()
                        if self._conn is not None:
                            health.update(
                                connected=True, enabled=True, last_ready_at=now_iso(),
                                last_error_type=None,
                            )

                client = HealthAwareClient(
                    app_id=app_id, app_secret=app_secret,
                    log_level=LogLevel.WARNING,
                    event_handler=dispatcher, auto_reconnect=True,
                )
                client.on_reconnecting = health.reconnecting
                client.on_reconnected = lambda: health.update(
                    connected=True, last_ready_at=now_iso(), last_error_type=None,
                )
                enable_card_frame_dispatch(client, lark_ws)
                client.start()

            await asyncio.to_thread(run_client)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            health.update(
                connected=False, last_error_type=type(exc).__name__,
                last_error_at=now_iso(),
            )
            logging.exception("Feishu WebSocket gateway stopped; retrying")
            await asyncio.sleep(5)
