"""Triage executor (docs/ROUND2_PLAN.md R2.1): a read-only first look at a PM-channel message while the
project is idle. It answers in the PM's voice, or asks Kernel to start a task or route to another project."""
from __future__ import annotations

import asyncio
from typing import Any

from assistant_kernel.core.executors.common import (
    READ_ONLY_TOOLS, call_for_json, capabilities, charter, load_agent, project_catalog, prompt_text,
)
from assistant_kernel.core.executors.supervisor import render_messages


async def execute_triage(run: dict[str, Any], ctx) -> dict[str, Any]:
    kernel, settings = ctx.kernel, ctx.services["settings"]
    project_id = run["project_id"]
    caps = await asyncio.to_thread(capabilities, settings, project_id)
    config = await asyncio.to_thread(load_agent, settings, project_id, "pm", purpose="triage")
    message = await kernel.get_message(run["input"]["message_id"])
    sections = [f"# 项目 {project_id} · PM 分诊"]
    project_charter = await asyncio.to_thread(charter, settings, project_id)
    if project_charter:
        sections.append("## 项目章程\n\n" + project_charter)
    sections.append("## 本项目对你的角色要求\n\n" + config.role_text)
    if config.skills_text:
        sections.append(config.skills_text)
    if caps & {"routing", "project_management"}:
        catalog = await asyncio.to_thread(project_catalog, settings, await kernel.active_projects(), exclude=project_id)
        sections.append("## 项目名录（Kernel 汇总，各项目章程的用途、范围和路由信息）\n\n" + catalog)
    sections.append("## 最近的对话\n\n" + render_messages(await kernel.recent_messages(project_id)))
    sections.append(f"## 用户刚在 PM 频道发的消息（项目当前没有进行中的任务）\n\n{message['body'].get('text', '')}")
    sections.append(prompt_text("triage"))
    if "routing" in caps:
        sections.append(prompt_text("routing"))
    if "project_management" in caps:
        sections.append(prompt_text("triage-projects"))
    parsed, model_result = await call_for_json(
        ctx, run, config, "\n\n".join(sections), task_id=None, stage=None, session_id=None,
        tools=READ_ONLY_TOOLS, sandbox="read-only")
    if parsed is None:
        return {"decision": "invalid_output", "raw": model_result.final_text[:2000]}
    return {**parsed, "config_sha256": config.config_sha256, "skills": list(config.skill_labels),
            "usage": model_result.usage.model_dump(), "_capabilities": sorted(caps)}
