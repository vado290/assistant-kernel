"""Change stream helpers (docs/ROUND4_PLAN.md section 2): parse what the PM writes, derive artifacts and significance,
render rows for reviews, tools and the /iterations command. Reference resolution only reads; the state machine writes.
"""
from __future__ import annotations

import re
from typing import Any

import yaml

RECORD_SECTION = "迭代记录"          # contract: a YAML block in the implementation report
EFFECT_SECTION = "上期效果复核"       # contract: a YAML list in the weekly review's improvement proposal
CHANGE_TYPES = ("code", "file", "skill", "charter", "map", "profile", "brief", "jobs", "grant", "shared", "config", "doc")
EFFECTS = {"effective": "有效", "ineffective": "无效", "pending": "待观察"}
MAX_TEXT = 1500

# Paths whose change is always significant (auto major), whatever the PM wrote.
MAJOR_PATHS = (re.compile(r"(^|/)sql/[^/]+\.sql$"), re.compile(r"(^|/)migrations?/"), re.compile(r"(^|/)jobs\.yaml$"),
               re.compile(r"(^|/)knowledge/charter\.md$"), re.compile(r"(^|/)project\.yaml$"),
               re.compile(r"(^|/)core/machine\.py$"), re.compile(r"(^|/)prompts/core/"))


def _yaml_block(markdown: str, title: str) -> Any:
    from assistant_kernel.core.skills import section_text
    block = section_text(markdown, title)
    if not block:
        raise ValueError(f"缺少'{title}'一节")
    fenced = re.search(r"```(?:ya?ml)?\s*\n(.*?)```", block, re.S)
    try:
        return yaml.safe_load(fenced.group(1) if fenced else block)
    except yaml.YAMLError as exc:
        raise ValueError(f"'{title}'不是合法的 YAML：{exc}") from exc


def _text(value: Any, limit: int = MAX_TEXT) -> str:
    return " ".join(str(value or "").split())[:limit]


def parse_record(markdown: str) -> dict[str, Any]:
    """The PM's iteration record from an implementation report; raises ValueError with what is wrong."""
    data = _yaml_block(markdown, RECORD_SECTION)
    if not isinstance(data, dict):
        raise ValueError(f"'{RECORD_SECTION}'必须是一个 YAML 对象")
    title, reason = _text(data.get("title"), 200), _text(data.get("reason"))
    if not title or not reason:
        raise ValueError("迭代记录必须有 title 和 reason")
    changes = []
    for item in data.get("changes") or []:
        if not isinstance(item, dict) or not item.get("target"):
            raise ValueError("changes 的每一项必须是对象，并写明 target（名称或路径）")
        kind = str(item.get("type") or "file")
        if kind not in CHANGE_TYPES:
            raise ValueError(f"changes 的 type 只能是 {', '.join(CHANGE_TYPES)}")
        changes.append({"type": kind, "target": _text(item["target"], 200),
                        "action": str(item.get("action") or "modify"), "note": _text(item.get("note"), 400),
                        **({"version": _text(item["version"], 40)} if item.get("version") else {})})
    significance = str(data.get("significance") or "minor")
    if significance not in ("major", "minor"):
        raise ValueError("significance 只能是 major 或 minor")
    return {"title": title, "reason": reason, "changes": changes, "significance": significance,
            **{key: _text(data.get(key)) for key in ("verification", "expected_effect", "measure", "followups")}}


def parse_effect_reviews(markdown: str, *, strict: bool = True) -> list[dict[str, str]]:
    """An explicit empty list is different from missing or malformed evidence."""
    try:
        data = _yaml_block(markdown, EFFECT_SECTION)
    except ValueError:
        if strict: raise
        return []
    if not isinstance(data, list):
        if strict: raise ValueError("上期效果复核必须是 YAML 列表；没有需复核的记录时写 []")
        return []
    reviews = []
    for item in data:
        prefix = str(item.get("iteration") or "").lower() if isinstance(item, dict) else ""
        if not valid_iteration_prefix(prefix) or str(item.get("status")) not in EFFECTS or not _text(item.get("note")):
            if strict: raise ValueError("效果复核需要合法记录编号（至少 8 位 UUID 前缀）、status 和有证据的 note")
            continue
        if strict and any(r["iteration"] == prefix for r in reviews):
            raise ValueError("同一效果记录不能重复复核")
        reviews.append({"iteration": prefix, "status": item["status"], "note": _text(item["note"], 400)})
    return reviews


def valid_iteration_prefix(value: str) -> bool:
    shape = "00000000-0000-0000-0000-000000000000"
    return 8 <= len(value) <= len(shape) and all(
        c == "-" if shape[i] == "-" else c in "0123456789abcdef" for i, c in enumerate(value))


async def resolve_effect_reviews(c, project_id, reviews):
    """Resolve only unique, legal references inside one project; conflicting duplicates update neither verdict."""
    from assistant_kernel.core.db import all_rows
    resolved, problems, duplicates = {}, [], set()
    for review in reviews or []:
        review = review if isinstance(review, dict) else {}
        prefix = str(review.get("iteration") or "").lower()
        matches = await all_rows(c, "SELECT iteration_id FROM core.iterations WHERE project_id=%s AND iteration_id::text LIKE %s",
                                 (project_id, prefix + "%")) if valid_iteration_prefix(prefix) else []
        if len(matches) != 1 or str(review.get("status")) not in EFFECTS or not str(review.get("note") or "").strip():
            problems.append(prefix or "（缺少编号）")
            continue
        ident = matches[0]["iteration_id"]
        if ident in resolved:
            duplicates.add(ident)
            problems.append(prefix)
        resolved[ident] = review
    return {i: r for i, r in resolved.items() if i not in duplicates}, problems


def artifact_of(path: str) -> str:
    path = path.split("://", 1)[-1].split("/", 1)[-1] if "://" in path else path
    match = re.search(r"\.agents/skills/([^/]+)/", path)
    if match:
        return f"skill:{match.group(1)}"
    if path.endswith(("knowledge/map.yaml", "docs/map.yaml")):
        return "map"
    if path.endswith("knowledge/charter.md"):
        return "charter"
    if path.endswith("jobs.yaml"):
        return "jobs"
    if path.endswith("knowledge/user-profile.md"):
        return "profile"
    if path.endswith("knowledge/domain-brief.md"):
        return "brief"
    return f"file:{path}"


def artifacts(changes: list[dict[str, Any]], files: list[str]) -> list[str]:
    found = [artifact_of(f) for f in files]
    for change in changes:
        found.append(change["type"] if change["type"] in ("map", "charter", "jobs", "profile", "brief")
                     else f"{change['type']}:{change['target']}")
    return sorted(set(found))


def auto_major(files: list[str]) -> bool:
    return any(pattern.search(f.split("://", 1)[-1]) for f in files for pattern in MAJOR_PATHS)


def search_text(row: dict[str, Any]) -> str:
    notes = " ".join(f"{c.get('target', '')} {c.get('note', '')}" for c in row.get("changes") or [])
    return _text(" ".join([row.get("title", ""), row.get("reason", ""), notes, " ".join(row.get("artifacts") or [])]),
                 4000)


def render(rows: list[dict[str, Any]], *, brief: bool = False) -> str:
    """Rows as compact Markdown; `brief` gives one line per row."""
    lines = []
    for r in rows:
        tag = "【重大】" if r["significance"] == "major" else ""
        head = (f"- `{str(r['iteration_id'])[:8]}` {r['created_at']:%Y-%m-%d} {tag}{r['title']}"
                f"（{r['kind']}，来源 {r['source']}{(' ' + r['source_ref']) if r.get('source_ref') else ''}）")
        if brief:
            lines.append(f"{head}：{_text(r['reason'], 120)}")
            continue
        lines.append(head)
        lines.append(f"  - 原因：{_text(r['reason'], 400)}")
        for change in (r["changes"] or [])[:6]:
            lines.append(f"  - 改动：{change.get('type')} {change.get('target')} {change.get('action', '')}"
                         f"{(' ' + change['version']) if change.get('version') else ''}：{_text(change.get('note'), 160)}")
        if len(r["changes"] or []) > 6:
            lines.append(f"  - 改动：另有 {len(r['changes']) - 6} 项")
        for key, label in (("verification", "验证"), ("expected_effect", "预期效果"), ("measure", "衡量"),
                           ("followups", "遗留")):
            if r.get(key):
                lines.append(f"  - {label}：{_text(r[key], 200)}")
        if r.get("effect_status"):
            lines.append(f"  - 效果复核：{EFFECTS[r['effect_status']]}（{_text(r.get('effect_note'), 160)}）")
        if not r.get("complete", True):
            lines.append("  - （PM 的迭代记录缺失或格式有误，本条由平台根据任务信息生成）")
    return "\n".join(lines) or "（没有记录）"
