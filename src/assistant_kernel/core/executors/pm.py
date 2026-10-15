"""PM turn executor (plan 2.4–2.6): one turn of the project PM in the current stage."""
from __future__ import annotations

import asyncio
from typing import Any

from assistant_kernel.core.executors.common import (
    CANDIDATE_TOOLS, MVP_TOOLS, call_for_json, load_agent, prompt_text, render_documents, write_document,
)

STAGE_PROMPT = {"requirements": "pm-requirements", "design": "pm-design", "build": "pm-build",
                "review": "pm-review", "operate": "pm-operate"}
DOCUMENT_STAGE = {"task_contract": "requirements", "solution_design": "design",
                  "implementation_report": "build", "review_report": "review"}


def render_turn(task: dict[str, Any], run_input: dict[str, Any], inputs: list[dict[str, Any]]) -> str:
    lines = [f"- 任务目标：{task['goal']}", f"- 当前阶段：{run_input['stage']}"]
    if run_input.get("kind") == "instruction":
        lines.append(f"\n### 本轮指令（用户已确认）\n\n{run_input['instruction']}")
    elif run_input.get("kind") == "answers":
        asked = {q["id"]: q["text"] for q in run_input.get("questions") or []}
        answered = "\n".join(f"- {asked.get(qid, qid)}：{value}" for qid, value in run_input["answers"].items())
        lines.append(f"\n### 用户对你上轮问题的回答\n\n{answered}")
        if run_input.get("pm_questions") and run_input.get("pm_questions") != run_input.get("questions"):
            original = "\n".join(f"- [{q['id']}] {q['text']}" for q in run_input["pm_questions"])
            lines.append(f"\n（以上问题由 Supervisor 整理自你的原始问题：\n{original}）")
    if run_input.get("note"):
        lines.append(f"\n### 用户附言\n\n{run_input['note']}")
    texts = [m["body"].get("text") for m in inputs if m["body"].get("text")]
    if texts:
        lines.append("\n### 用户在 PM 频道的补充消息（按时间顺序）\n\n" + "\n".join(f"- {t}" for t in texts))
    return "\n".join(lines)


async def execute_pm(run: dict[str, Any], ctx) -> dict[str, Any]:
    kernel, settings = ctx.kernel, ctx.services["settings"]
    task = await kernel.get_task(run["task_id"])
    run_input = run["input"]
    stage = run_input["stage"]
    config = await asyncio.to_thread(load_agent, settings, run["project_id"], "pm", stage)
    inputs = await kernel.turn_inputs(run)
    prompt = "\n\n".join([
        prompt_text("pm-common"),
        "## 项目说明\n\n" + config.role_text,
        prompt_text(STAGE_PROMPT[stage]),
        *(["## 已加载技能\n\n" + "\n\n".join(config.skill_texts)] if config.skill_texts else []),
        "## 已生效文档\n\n" + render_documents(settings, task),
        "## 本轮上下文\n\n" + render_turn(task, run_input, inputs),
        prompt_text("pm-output"),
    ])
    fresh = run_input.get("fresh_session", True)
    tools = MVP_TOOLS if stage == "build" else MVP_TOOLS - CANDIDATE_TOOLS
    parsed, model_result = await call_for_json(
        ctx, run, config, prompt, task_id=str(task["task_id"]), stage=stage,
        session_id=None if fresh else task["pm_session"].get("native_session_id"),
        tools=tools, sandbox="danger-full-access", candidates=stage == "build")
    if parsed is None:
        return {"outcome": "invalid_output", "raw": model_result.final_text[:2000]}
    result = {"outcome": parsed.get("outcome"), "summary": str(parsed.get("summary") or ""),
              "questions": parsed.get("questions") or [], "session_id": model_result.physical_session_id,
              "config_sha256": config.config_sha256, "usage": model_result.usage.model_dump()}
    document = parsed.get("document")
    if isinstance(document, dict) and document.get("markdown"):
        kind = document.get("kind")
        if DOCUMENT_STAGE.get(kind) != stage:
            return {"outcome": "invalid_output", "raw": f"document kind {kind!r} does not belong to stage {stage}"}
        payload = {"markdown": document["markdown"]}
        if kind == "review_report":
            payload["verdict"] = document.get("verdict")
        report = await asyncio.to_thread(write_document, settings, run["project_id"], str(task["task_id"]),
                                         str(run["run_id"]), kind, payload, "pm")
        if kind == "implementation_report":
            report["bundle_sha"] = await asyncio.to_thread(seal_candidates, settings, run["project_id"],
                                                           str(task["task_id"]), report["sha256"])
        if kind == "review_report":
            report["verdict"] = payload["verdict"]
            report["bundle_sha"] = (task["documents"].get("validation") or {}).get("bundle_sha")
        result["report"] = report
    return result


def seal_candidates(settings, project_id: str, task_id: str, report_sha: str) -> str:
    """Kernel seals the candidate files itself; a build with no files gets a report-bound marker."""
    from assistant_kernel.services.candidate_bundle import CandidateBundleService
    try:
        return CandidateBundleService(settings).seal(project_id, task_id)["bundle_sha256"]
    except FileNotFoundError:
        return f"no-files:{report_sha}"
