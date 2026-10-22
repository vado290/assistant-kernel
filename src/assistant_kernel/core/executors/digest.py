"""Render a project digest (Kernel.project_digest) as Markdown evidence for an evolution review."""
from __future__ import annotations

import json
from typing import Any

RECENT_LIMIT, MAJOR_LIMIT = 30, 20   # hard caps on the change stream in a review (ROUND4)
STAGE_LABELS = {"requirements": "需求", "design": "设计", "build": "实施", "review": "Review", "operate": "运营"}
UNSURE = ("不清楚", "不知道", "不确定", "随便", "都行")
SHARED_CANDIDATE_LIMIT = 20
SHARED_CANDIDATES_HEADING = ("### 其他项目背景存档里标“公共”、公共背景还没有的事实（候选；值得采纳的由用户决定，"
                             "用 propose_shared_update 一节一节提议）\n\n")


def shared_candidates(settings, project_ids: list[str], *, exclude: str) -> str:
    """Facts other projects archived as "公共" (true beyond that project) that the shared files do not hold yet.
    A plain text check against the shared files: good enough to show a short list for a person to decide on."""
    from assistant_kernel.core import skills
    from assistant_kernel.core.executors.common import charter
    from assistant_kernel.core.vault_sync import ARCHIVE
    shared = "\n".join(s.body for s in skills.shared_context(settings.data_root))
    found = []
    for project_id in project_ids:
        if project_id == exclude:
            continue
        for line in skills.section_text(charter(settings, project_id), ARCHIVE).splitlines():
            fact = line.strip().lstrip("-*").strip()
            if not fact.startswith("公共"):
                continue
            fact = fact[2:].lstrip("：:").strip()
            if fact and fact[:20] not in shared:
                found.append(f"- {project_id}：{fact}")
    return "\n".join(found[:SHARED_CANDIDATE_LIMIT])


async def evolution_material(kernel, settings, project_id: str) -> str:
    """Everything a weekly review reads: the project's own digest, template updates its skill copies have not taken,
    and — for the kernel (kernel_maintenance) — what the other projects share with it (ROUND2 R4b)."""
    from assistant_kernel.core import skills
    from assistant_kernel.storage import ProjectRuntimeManager
    runtime = ProjectRuntimeManager(settings.data_root)
    vault = runtime.ensure(project_id).vault_root
    from assistant_kernel.core import iterlog
    parts = [render_digest(await kernel.project_digest(project_id))]
    context = await kernel.evolution_context(project_id)
    parts.append("### 运营与后续任务入口（有界只读；登记状态不等于实时健康，截断不等于没有其他对象）\n"
                 + json.dumps(context, ensure_ascii=False, default=str)
                 + "\n时间口径：observed_at 是本次数据库资料读取时间，不是健康检查时间；"
                   "kind=job 的 last_at 是最后执行结果时间，kind=service 的 last_at 是最后状态变化时间。"
                   "服务状态不变时 last_at 不刷新，长期稳定运行也可能为旧值或空值；不能仅据此认定检查过期或服务异常。"
                   "缺少实时健康证据时说明尚未核实，结合已有日志/回执判断，不补造检查时间。"
                 + "\n已运行任务可读已生成的 runs/<task_id>/state.md，排队任务先用上面的状态与原因；历史复盘按 proposal_ref 读取；"
                   "先核对重复提案、覆盖与继续入口。若列表截断，结合地图/配置定向查证并注明无法取得的状态，不宣称全覆盖。")
    parts.append("运行版本仅列最近至多 12 条有 skills/config_sha256 的成功记录：技能标签包含按需可发现项，"
                 "不证明全文已读或实际遵循；配置指纹不保存旧 role 内容，缺可靠基线标未知。")
    from assistant_kernel.core.skill_updates import list_updates
    adoption = await list_updates(kernel, project_id)
    if adoption:
        parts.append("### 技能采用记录（文件已应用不代表已加载或效果通过；受阻项交后续任务，引用 update_id）\n"
                     + json.dumps(adoption, ensure_ascii=False, default=str))
    baseline = await kernel.operational_baseline(project_id)
    if baseline:
        released = await kernel.get_task(baseline["task_id"])
        refs = {k: v.get("ref") for k, v in (released or {}).get("documents", {}).items()
                if k in ("task_contract", "solution_design", "implementation_report", "review_report")}
        parts.append("### 已发布变更入口（最近增量，不代表全部运营要求；用 read_project_document 按 ref 核实）\n"
                     + json.dumps({**baseline, "document_refs": refs}, ensure_ascii=False, default=str))
    else:
        parts.append("### 发布依据\n尚无 Kernel 发布基线；核实实际产物及人工/外部发布证据，不补造批准或推断没有运营对象。")
    recent = await kernel.iterations(project_id, days=28, limit=RECENT_LIMIT + 1)
    parts.append("### 近 4 周的变更流水（有界显示；记录编号用于上期效果复核）\n" + iterlog.render(recent[:RECENT_LIMIT])
                 + ("\n另有记录未显示，需用 list_iterations 定向查询。" if len(recent) > RECENT_LIMIT else ""))
    recent = recent[:RECENT_LIMIT]
    for label, filters in (("尚待效果复核（含超过默认窗口的记录，pending 不算关闭）", {"pending_only": True}),
                           ("有预期效果但缺衡量方法（补证，不补造历史指标）", {"needs_measure": True})):
        page = await kernel.iteration_page(project_id, **filters, oldest_first=True, limit=RECENT_LIMIT)
        parts.append("### " + label + "\n" + iterlog.render(page["items"])
                     + "\n分页入口：" + json.dumps({"filters": {**filters, "oldest_first": True},
                         "as_of": page["as_of"], "truncated": page["truncated"], "next_cursor": page["next_cursor"]})
                     + "\n本轮纳入项逐条结论或暂缓；未覆盖项写继续入口，[] 不证明无待办。状态可变化，下轮重新核对。")
    shown = {r["iteration_id"] for r in recent}
    majors = [r for r in await kernel.iterations(project_id, days=180, major_only=True, limit=MAJOR_LIMIT + len(shown))
              if r["iteration_id"] not in shown][:MAJOR_LIMIT]
    if majors:
        parts.append("### 6 个月内更早的重大改动（每条一行；需要细节时用 list_iterations 查）\n"
                     + iterlog.render(majors, brief=True))
    gaps = skills.collaboration_gaps(vault)
    if gaps:
        parts.append("### 技能协作断链（按各技能的 io 元数据检查；断链处往往是交接出问题的地方）\n"
                     + "\n".join(f"- {g}" for g in gaps))
    updates = skills.template_updates(vault)
    if updates:
        parts.append("### 公共模板有更新（本项目的副本没有跟进；是否采用、采用哪些部分由你在建议中提出）\n" + "\n".join(
            f"- {u['name']}：本项目 {u['copy']}，模板 {u['template']}。模板最近的变更：{u['changes']}" for u in updates))
    if "kernel_maintenance" in skills.project_config(vault)["capabilities"]:
        lines = []
        for other in await kernel.active_projects():
            if other == project_id:
                continue
            share = skills.project_config(runtime.ensure(other).vault_root)["share_with_kernel"]
            lines.append(summarize_project(other, share, await kernel.project_digest(other)))
        if lines:
            parts.append("### 其他项目的运营概况（按各项目的共享级别；用来发现多个项目共同的问题，据此改进公共模板）\n"
                         + "\n".join(lines))
        candidates = shared_candidates(settings, await kernel.active_projects(), exclude=project_id)
        if candidates:
            parts.append(SHARED_CANDIDATES_HEADING + candidates)
    return "\n\n".join(parts)


def summarize_project(project_id: str, share: str, digest: dict[str, Any]) -> str:
    tasks = digest["tasks"]
    done = sum(1 for t in tasks if t["state"] == "completed")
    stopped = sum(1 for t in tasks if t["state"] == "cancelled")
    actions = digest["card_actions"]
    revised = sum(1 for a in actions if a.get("action") == "revise")
    rejected = sum(1 for a in actions if a.get("action") in ("reject", "stop"))
    failed = sum(f["n"] for f in digest["failures"])
    jobs_failed = sum(j["failed"] for j in digest.get("jobs") or [])
    line = (f"- {project_id}（{share}）：任务 {len(tasks)}（完成 {done}、停止 {stopped}），用户发言 {digest['user_messages']}，"
            f"卡片修改 {revised}、拒绝或停止 {rejected}，失败或重试的执行 {failed}，作业失败 {jobs_failed}")
    if share in ("improvements", "full"):
        goals = [t["goal"][:60] for t in tasks if t.get("kind") == "delivery"][:5]
        if goals:
            line += "；最近的任务：" + "；".join(goals)
    if share == "full":
        texts = [str(a.get("text"))[:80] for a in actions if a.get("text")][:3]
        if texts:
            line += "；用户的修改意见：" + "；".join(texts)
    return line


def render_digest(digest: dict[str, Any]) -> str:
    lines = [f"### 最近 {digest['days']} 天的运行记录（Kernel 统计，事实以此为准）",
             f"- 用户发言 {digest['user_messages']} 条；任务 {len(digest['tasks'])} 个"]
    for t in digest["tasks"]:
        lines.append(f"  - [{t['state']}/{t['stage']}] {t['goal']}（PM 回合 {t['turn_count']}"
                     + (f"，结束原因 {t['close_reason']}" if t["close_reason"] else "") + "）")
        if t.get("task_id"):
            lines.append(f"    状态簿：runs/{t['task_id']}/state.md")
    actions = digest["card_actions"]
    revises = [a for a in actions if a.get("action") == "revise" and a.get("text")]
    negative = [a for a in actions if a.get("action") in ("stop", "reject")]
    unsure = [a for a in actions if any(word in str(a.get("answers") or "") for word in UNSURE)]
    lines.append(f"- 卡片操作 {len(actions)} 次：修改 {len(revises)}、拒绝或停止 {len(negative)}、回答中表示不清楚 {len(unsure)}")
    for a in revises[:10]:
        lines.append(f"  - 用户修改意见：「{a['text'][:200]}」（针对：{a.get('message', '')[:100]}）")
    for a in negative[:10]:
        lines.append(f"  - 用户{('停止' if a['action'] == 'stop' else '拒绝')}：{a.get('message', '')[:150]}")
    for a in unsure[:5]:
        lines.append(f"  - 用户难以回答的问题：{a.get('message', '')[:150]} → {str(a.get('answers'))[:150]}")
    if len(revises) > 10 or len(negative) > 10 or len(unsure) > 5:
        lines.append("- 卡片例子已截断，不能代表全部反馈；需读相关任务状态簿，消息可用 list_recent_history 分页补查。")
    counts = digest["event_counts"]
    lines.append(f"- 卡片因新输入被改写 {counts.get('card_upgraded', 0)} 次；Supervisor 因新消息重新评估 "
                 f"{counts.get('supervisor_reevaluate', 0)} 次；作废的过期结果 {counts.get('stale_result', 0)} 次")
    per_task: dict[str, list[str]] = {}
    for row in digest.get("stage_turns") or []:
        per_task.setdefault(row["goal"], []).append(f"{STAGE_LABELS.get(row['stage'], row['stage'])} {row['n']}")
    if per_task:
        lines.append("- 各任务按阶段的 PM 回合数：")
        lines += [f"  - {goal}：{'、'.join(parts)}" for goal, parts in per_task.items()]
    for job in digest.get("jobs") or []:
        lines.append(f"- 定时作业 {job['job_id']}：成功 {job['ok']} 次，失败 {job['failed']} 次"
                     + (f"（例：{str(job['example'])[:150]}）" if job["failed"] and job["example"] else ""))
    delegations = digest.get("delegations") or []
    lines.append("- PM 自报使用的子 agent：" + ("、".join(f"{name} × {n}" for name, n in delegations) if delegations
                                             else "无（需求阶段没有派调研子 agent 时，留意是否该派而未派）"))
    if digest["failures"]:
        lines.append("- 失败或重试过的执行：")
        for f in digest["failures"]:
            lines.append(f"  - {f['run_type']}:{f['purpose']} × {f['n']}，例：{f['example'] or '（无错误信息）'}")
    else:
        lines.append("- 没有失败或重试过的执行")
    return "\n".join(lines)
