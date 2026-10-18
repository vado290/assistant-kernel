"""PM turn executor (plan 2.4–2.6): one turn of the project PM in the current stage."""
from __future__ import annotations

import asyncio
from typing import Any

from assistant_kernel.core import drafts, gitdelivery, grants, iterlog, rules
from assistant_kernel.core.drafts import DOCUMENT_STAGE, STAGE_DOCUMENT
from assistant_kernel.core.executors.common import (
    BRIEF_HEADING, CANDIDATE_TOOLS, DOMAIN_BRIEF, MVP_TOOLS, PROFILE_HEADING, USER_PROFILE, call_for_json, charter,
    document_sections, knowledge_text, load_agent, project_map, prompt_text, write_document,
)
from assistant_kernel.core.executors.digest import evolution_material
from assistant_kernel.core.executors.state import render_state, write_state

STAGE_NAMES = {"requirements": "需求分析", "design": "方案设计", "build": "实施与自测", "review": "独立 Review",
               "operate": "运营", "evolution": "周度复盘"}
SUBAGENT_STAGES = {"requirements", "design", "build", "review", "evolution"}
QUICK_NOTES = {   # process facts for the quick track (R2.3); Kernel states them, skills need not
    "requirements": "本任务不单独做设计和 Review。需求文档除常规章节外，必须包含'简要方案'一节：要改哪些文件、怎么做、怎么测试；"
                    "用户批准需求即同时批准这个方案",
    "build": "按需求文档中的'简要方案'实施并自测；完成后 Kernel 验证，验证通过即可申请发布，没有单独的 Review",
}
MAX_SUBAGENTS = 3
MAX_DELEGATIONS = 10
BRIEF_STAGES = {"requirements", "design", "evolution"}   # where the archived industry brief is worth its tokens
RUNTIME_STAGES = {"design", "build", "review"}            # where the runtime manifest contract is needed
MAP_STAGES = {"design", "build", "review", "evolution"}   # where the project map is worth its tokens
RESUMED_NOTE = ("（同一会话的续轮：章程、角色要求、技能和文档中与上一轮相同的部分不再重复发送，下面只有变化的部分；"
                "需要文档原文时调用 read_task_document，需要完整的任务记录时读 {state}。）")
OUTPUT_REMINDER = "输出要求与上一轮相同：最后一条消息只输出那一个 JSON 对象。"


def skill_stage(task: dict[str, Any], stage: str) -> str:
    """The stage name skills are matched against; the weekly review is its own one."""
    return "evolution" if task["kind"] == "evolution" else stage


def render_turn(task: dict[str, Any], run_input: dict[str, Any], inputs: list[dict[str, Any]]) -> str:
    stage = skill_stage(task, run_input["stage"])
    lines = [f"- 任务目标：{task['goal']}", f"- 当前阶段：{STAGE_NAMES.get(stage, stage)}（{stage}）"]
    if task.get("reason"):
        lines.append(f"- 发起原因：{task['reason']}")
    if task.get("track") == "quick" and stage in QUICK_NOTES:
        lines.append(f"- 流程：轻量（{QUICK_NOTES[stage]}）")
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
    matched = skill_stage(task, stage)
    code = await asyncio.to_thread(gitdelivery.config, settings, run["project_id"])   # None: file delivery
    in_worktree = bool(code) and stage in ("build", "review")   # then Codex cannot find the PM's skills itself
    config = await asyncio.to_thread(load_agent, settings, run["project_id"], "pm", stage=matched,
                                     task_kind=task["kind"], native=not in_worktree)
    inputs = await kernel.turn_inputs(run)
    project_charter = await asyncio.to_thread(charter, settings, run["project_id"])
    profile = await asyncio.to_thread(knowledge_text, settings, run["project_id"], USER_PROFILE)
    brief = await asyncio.to_thread(knowledge_text, settings, run["project_id"], DOMAIN_BRIEF) \
        if matched in BRIEF_STAGES else ""
    digest = await evolution_material(kernel, settings, run["project_id"]) if task["kind"] == "evolution" else ""
    task_id = str(task["task_id"])
    session_id = None if run_input.get("fresh_session", True) else task["pm_session"].get("native_session_id")
    stage_kind = STAGE_DOCUMENT.get(stage)
    for kind in drafts.editable(stage):   # a new session starts from the current revision; a resumed one keeps edits
        await asyncio.to_thread(drafts.ensure, settings, run["project_id"], task_id, kind,
                                task["documents"].get(kind), reset=session_id is None)
    # The ledger is the handoff: a new session gets it in full, a resumed one already lived through it.
    state_text = render_state(task, await kernel.task_ledger(task["task_id"]), STAGE_NAMES)
    state_file = await asyncio.to_thread(write_state, settings, run["project_id"], task_id, state_text)
    build = stage == "build"
    workspace = None
    if in_worktree:   # the task's own worktree, cut from main
        workspace = await asyncio.to_thread(gitdelivery.ensure_worktree, code, task_id)
    map_text = ""
    if matched in MAP_STAGES:   # the project map: where things are (ROUND4 R4.2)
        where, text = await asyncio.to_thread(project_map, settings, run["project_id"])
        map_text = (f"## 项目地图（{where}；结构或组件有改动时在实施阶段同步更新）\n\n"
                    + (text or "本项目还没有地图：第一次实施时按 assistant-task-continuity 的\"项目地图\"格式建立。"))
    workspace_text = (f"{prompt_text('git-delivery')}\n\n- 本任务的工作区：`{workspace}`（分支 `{gitdelivery.branch(task_id)}`，"
                      f"从 `{code['main']}` 拉出；你的当前目录就是它）\n- 测试命令：`{code['test_command'] or '未配置'}`"
                      if workspace else "")
    context = [
        *([("charter", drafts.text_sha(project_charter), "## 项目章程\n\n" + project_charter)] if project_charter else []),
        *([("profile", drafts.text_sha(profile), PROFILE_HEADING + profile)] if profile else []),
        *([("brief", drafts.text_sha(brief), BRIEF_HEADING + brief)] if brief else []),
        ("config", config.config_sha256, "\n\n".join(["## 本项目对你的角色要求\n\n" + config.role_text,
                                                      *([config.skills_text] if config.skills_text else [])])),
        *([("digest", drafts.text_sha(digest), digest)] if digest else []),
        *([("bootstrap", drafts.text_sha(creation := prompt_text("pm-bootstrap")), creation)]
          if task["kind"] == "bootstrap" else []),
        *([("runtime", drafts.text_sha(manifest := prompt_text("runtime-manifest")), manifest)]
          if matched in RUNTIME_STAGES else []),
        *([("map", drafts.text_sha(map_text), map_text)] if map_text else []),
        *([("workspace", drafts.text_sha(workspace_text), workspace_text)] if workspace_text else []),
    ]
    documents = await asyncio.to_thread(document_sections, settings, task)
    output = prompt_text("pm-output")
    seen = {key: fingerprint for key, fingerprint, _ in context + documents} | {"output": drafts.text_sha(output)}
    previous = task["pm_session"].get("seen") if session_id else None
    prompt = render_prompt(f"# 项目 {run['project_id']} · PM 回合（{STAGE_NAMES.get(matched, matched)}）",
                           context, documents, render_turn(task, run_input, inputs), output, seen, previous,
                           state=state_text, state_file=state_file)
    # Outside build the PM only reads: project files change solely through candidates → validate → publish.
    # Code delivery: the PM edits its own worktree instead of writing candidates; Kernel commits at turn end.
    candidates = build and not code
    tools = MVP_TOOLS if candidates else MVP_TOOLS - CANDIDATE_TOOLS
    parsed, model_result = await call_for_json(
        ctx, run, config, prompt, task_id=task_id, stage=stage, session_id=session_id,
        tools=tools, sandbox="danger-full-access" if build else "read-only", candidates=candidates,
        subagents=MAX_SUBAGENTS if matched in SUBAGENT_STAGES else 0, cwd=workspace)
    misfit = outcome_misfit(parsed, stage)
    if misfit and model_result.physical_session_id:   # tell the PM once, in the same session, instead of failing blind
        parsed, model_result = await call_for_json(
            ctx, run, config, misfit + OUTPUT_REMINDER, task_id=task_id, stage=stage,
            session_id=model_result.physical_session_id, tools=tools,
            sandbox="danger-full-access" if build else "read-only", candidates=candidates, cwd=workspace)
    if code and build and parsed is not None:   # keep every turn's work as a commit on the task branch
        await asyncio.to_thread(gitdelivery.commit_all, workspace,
                                f"{task['goal'][:72]}\n\n{str(parsed.get('summary') or '')[:1500]}\n\n任务 {task_id}")
    if parsed is None:
        return {"outcome": "invalid_output", "raw": model_result.final_text[:2000]}
    result = {"outcome": parsed.get("outcome"), "summary": str(parsed.get("summary") or ""),
              "questions": parsed.get("questions") or [], "session_id": model_result.physical_session_id,
              "config_sha256": config.config_sha256, "skills": list(config.skill_labels),
              "usage": model_result.usage.model_dump(), "seen": seen}
    document = parsed.get("document") if isinstance(parsed.get("document"), dict) else {}
    named, markdown = document.get("kind"), document.get("markdown")
    if (named or markdown) and DOCUMENT_STAGE.get(named) != stage:
        return {"outcome": "invalid_output", "raw": f"document kind {named!r} does not belong to stage {stage}"}
    if not markdown and stage_kind:   # the PM edited the draft through edit_task_document
        draft = await asyncio.to_thread(drafts.load, settings, run["project_id"], task_id, stage_kind)
        current = await asyncio.to_thread(drafts.revision_markdown, settings, run["project_id"],
                                          task["documents"].get(stage_kind))
        edited = str((draft or {}).get("markdown") or "")
        if named == stage_kind and not edited.strip():
            return {"outcome": "invalid_output", "raw": f"{named} was named but its draft is empty"}
        if named == stage_kind or edited != current:
            markdown = edited
    delegations = checked_delegations(parsed.get("delegations"))
    if delegations:
        result["delegations"] = delegations
    free = []
    for kind in sorted(drafts.free_kinds(stage)):
        saved = await asyncio.to_thread(save_free_document, settings, run["project_id"], task_id, str(run["run_id"]),
                                        kind, task["documents"].get(kind))
        if saved:
            free.append(saved)
            seen[kind] = saved["sha256"]   # the PM wrote it, so a resumed turn need not resend it
    if free:
        result["free_documents"] = free
    if markdown:
        kind = stage_kind
        payload = {"markdown": markdown}
        if kind == "review_report":
            payload["verdict"] = document.get("verdict")
        report = await asyncio.to_thread(write_document, settings, run["project_id"], task_id,
                                         str(run["run_id"]), kind, payload, "pm")
        await asyncio.to_thread(drafts.save, settings, run["project_id"], task_id, kind, markdown, report["sha256"])
        seen[kind] = report["sha256"]   # the PM wrote it, so the next resumed turn need not resend it
        if kind == "implementation_report":   # the PM's iteration record, approved with the release (ROUND4 R4.1)
            try:
                report["iteration"] = iterlog.parse_record(markdown)
            except ValueError as exc:
                report["iteration_error"] = str(exc)
        if kind == "improvement_proposal":
            report["effect_reviews"] = iterlog.parse_effect_reviews(markdown)
        if kind == "task_contract" and task["kind"] == "bootstrap":   # the charter: Kernel creates the project from it
            try:
                spec = rules.charter_spec(markdown)
                spec["directories"] = [grants.check_directory(d, spec["project_id"], settings.data_root)
                                       for d in spec["directories"]]   # existence and visibility, on this host
                report["spec"] = spec
            except ValueError as exc:
                report["spec_error"] = str(exc)
        if kind == "implementation_report" and code:   # bound to the commit; no changes → the no-files marker
            commit = await asyncio.to_thread(gitdelivery.seal, code, task_id, f"{task['goal'][:72]}\n\n任务 {task_id}")
            report["bundle_sha"] = commit or f"no-files:{report['sha256']}"
        elif kind == "implementation_report":
            report["bundle_sha"] = await asyncio.to_thread(seal_candidates, settings, run["project_id"],
                                                           str(task["task_id"]), report["sha256"])
        if kind == "review_report":
            report["verdict"] = payload["verdict"]
            report["bundle_sha"] = (task["documents"].get("validation") or {}).get("bundle_sha")
        result["report"] = report
    return result


def outcome_misfit(parsed: dict[str, Any] | None, stage: str) -> str:
    """A correction for an outcome the state machine would reject in this stage ('' when it fits)."""
    outcome = (parsed or {}).get("outcome")
    if outcome == "ready_for_validation" and stage != "build":
        return ("你的 outcome 是 ready_for_validation，但它只用于实施阶段（提交候选文件供 Kernel 验证）。"
                f"当前是 {STAGE_NAMES.get(stage, stage)} 阶段：本阶段工作完成请用 completed，其余内容不变。"
                "不要做任何新的工作，只重新输出那个 JSON 对象。")
    return ""


def checked_delegations(value: Any) -> list[dict[str, str]]:
    """The subagents the PM reports it used this turn (Kernel cannot see them itself); malformed entries are dropped."""
    if not isinstance(value, list):
        return []
    entries = [{key: str(item.get(key) or "")[:300] for key in ("name", "goal", "result")}
               for item in value[:MAX_DELEGATIONS] if isinstance(item, dict)]
    return [entry for entry in entries if entry["name"] or entry["goal"]]


def save_free_document(settings, project_id: str, task_id: str, run_id: str, kind: str,
                       current: dict[str, Any] | None):
    """Store a free document's draft (working notes, domain brief) as a new revision when it changed this turn."""
    draft = drafts.load(settings, project_id, task_id, kind)
    text = str((draft or {}).get("markdown") or "")
    if not text.strip() or text == drafts.revision_markdown(settings, project_id, current):
        return None
    report = write_document(settings, project_id, task_id, run_id, kind, {"markdown": text}, "pm")
    drafts.save(settings, project_id, task_id, kind, text, report["sha256"])
    return report


def render_prompt(header: str, context: list[tuple[str, str, str]], documents: list[tuple[str, str, str]],
                  turn: str, output: str, seen: dict[str, str], previous: dict[str, str] | None, *,
                  state: str = "", state_file: str = "runs/<任务>/state.md") -> str:
    """The full prompt for a new session; on a resumed one only what changed since the previous PM turn.

    A resumed Codex session already holds the earlier prompts, and the appended prompt is not part of the cached
    prefix, so repeating unchanged sections is paid in full every turn.
    """
    if previous is None:
        return "\n\n".join([header, *(text for _, _, text in context),
                            *(["## 任务状态簿（Kernel 记录的事实，优先于你的记忆）\n\n" + state] if state else []),
                            "## 已生效文档\n\n" + ("\n\n".join(t for _, _, t in documents) or "（暂无已生效文档）"),
                            "## 本轮上下文\n\n" + turn, output])
    changed = lambda items: [text for key, fingerprint, text in items if previous.get(key) != fingerprint]  # noqa: E731
    changed_documents = changed(documents)
    return "\n\n".join([
        header, RESUMED_NOTE.format(state=state_file), *changed(context),
        *(["## 有更新的文档\n\n" + "\n\n".join(changed_documents)] if changed_documents else []),
        "## 本轮上下文\n\n" + turn,
        output if previous.get("output") != seen["output"] else OUTPUT_REMINDER,
    ])


def seal_candidates(settings, project_id: str, task_id: str, report_sha: str) -> str:
    """Kernel seals the candidate files itself; a build with no files gets a report-bound marker."""
    from assistant_kernel.services.candidate_bundle import CandidateBundleService
    try:
        return CandidateBundleService(settings).seal(project_id, task_id)["bundle_sha256"]
    except FileNotFoundError:
        return f"no-files:{report_sha}"
