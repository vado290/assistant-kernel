"""Render a project digest (Kernel.project_digest) as Markdown evidence for an evolution review."""
from __future__ import annotations

from typing import Any

UNSURE = ("不清楚", "不知道", "不确定", "随便", "都行")


def render_digest(digest: dict[str, Any]) -> str:
    lines = [f"### 最近 {digest['days']} 天的运行记录（Kernel 统计，事实以此为准）",
             f"- 用户发言 {digest['user_messages']} 条；任务 {len(digest['tasks'])} 个"]
    for t in digest["tasks"]:
        lines.append(f"  - [{t['state']}/{t['stage']}] {t['goal']}（PM 回合 {t['turn_count']}"
                     + (f"，结束原因 {t['close_reason']}" if t["close_reason"] else "") + "）")
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
    counts = digest["event_counts"]
    lines.append(f"- 卡片因新输入被改写 {counts.get('card_upgraded', 0)} 次；Supervisor 因新消息重新评估 "
                 f"{counts.get('supervisor_reevaluate', 0)} 次；作废的过期结果 {counts.get('stale_result', 0)} 次")
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
