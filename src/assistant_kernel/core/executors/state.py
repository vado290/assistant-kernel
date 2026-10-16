"""Task state ledger (ROUND3_PLAN 4.1): the facts Kernel recorded about one task, rendered as Markdown.

A new PM session reads it instead of relying on an earlier conversation; it is also written to the vault
(`runs/<task>/state.md`) so subagents can read it. Built only from stored rows, never by a model.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from assistant_kernel.core import rules
from assistant_kernel.storage import ProjectRuntimeManager

FULL_TURNS = 10           # the latest turns in full; earlier ones as one line each
ACTION_LIMIT = 20
APPROVAL_NAMES = {"requirements": "需求", "design": "设计", "release": "发布"}
ACTION_NAMES = {"execute": "确认执行", "submit": "提交回答", "revise": "提出修改", "stop": "停止", "approve": "批准",
                "reject": "不同意", "retry": "重试", "continue": "继续"}


def _cut(text: Any, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


def render_state(task: dict[str, Any], ledger: dict[str, Any], stage_names: dict[str, str]) -> str:
    lines = ["### 任务", f"- 目标：{task['goal']}"]
    if task.get("reason"):
        lines.append(f"- 发起原因：{task['reason']}")
    lines.append(f"- 当前阶段：{stage_names.get(task['stage'], task['stage'])}；流程：{'轻量' if task.get('track') == 'quick' else '完整'}；"
                 f"已完成 PM 回合：{len(ledger['turns'])}")
    approvals = [f"{APPROVAL_NAMES.get(k, k)}（{'有效' if rules.approval_valid(task, k) else '已失效：文档在批准后改过'}）"
                 for k in task["approvals"] if k in APPROVAL_NAMES]
    lines.append("- 批准：" + ("、".join(approvals) if approvals else "暂无"))
    docs = [f"{kind}（sha {doc['sha256'][:12]}）" for kind, doc in task["documents"].items()
            if isinstance(doc, dict) and doc.get("sha256")]
    lines.append("- 已有文档：" + ("、".join(docs) if docs else "暂无") + "（全文用 read_task_document 读取）")

    lines.append("\n### 讨论记录（按回合，Kernel 记录的事实）")
    turns = ledger["turns"]
    if not turns:
        lines.append("（还没有完成的 PM 回合）")
    early = len(turns) - FULL_TURNS
    for number, turn in enumerate(turns, 1):
        given, result = turn["input"] or {}, turn["result"] or {}
        stage = stage_names.get(given.get("stage"), given.get("stage"))
        head = f"- 回合 {number}（{stage}，{turn['created_at']:%m-%d %H:%M}）"
        if number <= early:
            lines.append(f"{head}：{_cut(result.get('summary'), 150)}")
            continue
        lines.append(head)
        if given.get("kind") == "instruction":
            lines.append(f"  - 指令（用户已确认）：{_cut(given.get('instruction'), 600)}")
        elif given.get("kind") == "answers":
            asked = {q["id"]: q["text"] for q in given.get("questions") or []}
            for qid, value in (given.get("answers") or {}).items():
                lines.append(f"  - 用户回答：{_cut(asked.get(qid, qid), 120)} → {_cut(value, 300)}")
        if given.get("note"):
            lines.append(f"  - 用户附言：{_cut(given['note'], 400)}")
        for text in turn["supplements"]:
            lines.append(f"  - 用户补充：{_cut(text, 400)}")
        lines.append(f"  - PM 结果（{result.get('outcome', '')}）：{_cut(result.get('summary'), 500)}")
        for q in result.get("questions") or []:
            lines.append(f"  - PM 提问：{_cut(q.get('text'), 200)}")

    others = [a for a in ledger["actions"] if a.get("action") != "submit"][-ACTION_LIMIT:]
    if others:
        lines.append("\n### 卡片操作（最近）")
        for a in others:
            text = f"；用户写道：{_cut(a['text'], 300)}" if a.get("text") else ""
            lines.append(f"- {a['at']:%m-%d %H:%M} {ACTION_NAMES.get(a.get('action'), a.get('action'))}"
                         f"「{_cut(a.get('message'), 120)}」{text}")
    return "\n".join(lines)


def state_path(settings, project_id: str, task_id: str) -> Path:
    vault = ProjectRuntimeManager(settings.data_root).ensure(project_id).vault_root
    return vault / "runs" / str(task_id) / "state.md"


def write_state(settings, project_id: str, task_id: str, text: str) -> str:
    """Write the ledger where subagents can read it; returns its vault-relative path."""
    target = state_path(settings, project_id, task_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, "utf-8", newline="\n")
    return f"runs/{task_id}/state.md"
