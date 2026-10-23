"""Change stream helpers (docs/ROUND4_PLAN.md section 2): parse what the PM writes, derive artifacts and significance,
render rows for reviews, tools and the /iterations command. Reference resolution only reads; the state machine writes.
"""
from __future__ import annotations

import re
from typing import Any

import yaml

from assistant_kernel.core.i18n import heading

RECORD_SECTION = "iteration_record"   # contract: a YAML block in the implementation report (heading in any language)
EFFECT_SECTION = "effect_review"      # contract: a YAML list in the weekly review's improvement proposal
CHANGE_TYPES = ("code", "file", "skill", "charter", "map", "profile", "brief", "jobs", "grant", "shared", "config", "doc")
EFFECTS = ("effective", "ineffective", "pending")
MAX_TEXT = 1500

# Paths whose change is always significant (auto major), whatever the PM wrote.
MAJOR_PATHS = (re.compile(r"(^|/)sql/[^/]+\.sql$"), re.compile(r"(^|/)migrations?/"), re.compile(r"(^|/)jobs\.yaml$"),
               re.compile(r"(^|/)knowledge/charter\.md$"), re.compile(r"(^|/)project\.yaml$"),
               re.compile(r"(^|/)core/machine\.py$"), re.compile(r"(^|/)prompts/core/"))


def _yaml_block(markdown: str, title: str) -> Any:
    from assistant_kernel.core.skills import section_text
    block = section_text(markdown, title)
    if not block:
        raise ValueError(f"the '{heading(title)}' section is missing")
    fenced = re.search(r"```(?:ya?ml)?\s*\n(.*?)```", block, re.S)
    try:
        return yaml.safe_load(fenced.group(1) if fenced else block)
    except yaml.YAMLError as exc:
        raise ValueError(f"'{heading(title)}' is not valid YAML: {exc}") from exc


def _text(value: Any, limit: int = MAX_TEXT) -> str:
    return " ".join(str(value or "").split())[:limit]


def parse_record(markdown: str) -> dict[str, Any]:
    """The PM's iteration record from an implementation report; raises ValueError with what is wrong."""
    data = _yaml_block(markdown, RECORD_SECTION)
    if not isinstance(data, dict):
        raise ValueError(f"'{heading(RECORD_SECTION)}' must be a YAML object")
    title, reason = _text(data.get("title"), 200), _text(data.get("reason"))
    if not title or not reason:
        raise ValueError("the iteration record needs a title and a reason")
    changes = []
    for item in data.get("changes") or []:
        if not isinstance(item, dict) or not item.get("target"):
            raise ValueError("every item of changes must be an object with a target (name or path)")
        kind = str(item.get("type") or "file")
        if kind not in CHANGE_TYPES:
            raise ValueError(f"the type of a change must be one of {', '.join(CHANGE_TYPES)}")
        changes.append({"type": kind, "target": _text(item["target"], 200),
                        "action": str(item.get("action") or "modify"), "note": _text(item.get("note"), 400),
                        **({"version": _text(item["version"], 40)} if item.get("version") else {})})
    significance = str(data.get("significance") or "minor")
    if significance not in ("major", "minor"):
        raise ValueError("significance must be major or minor")
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
        if strict: raise ValueError("the effect review must be a YAML list; write [] when there is nothing to review")
        return []
    reviews = []
    for item in data:
        prefix = str(item.get("iteration") or "").lower() if isinstance(item, dict) else ""
        if not valid_iteration_prefix(prefix) or str(item.get("status")) not in EFFECTS or not _text(item.get("note")):
            if strict: raise ValueError("an effect review needs a valid record id (a UUID prefix of at least 8 characters), "
                                        "a status and a note with evidence")
            continue
        if strict and any(r["iteration"] == prefix for r in reviews):
            raise ValueError("the same record cannot be reviewed twice")
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
            problems.append(prefix or "(id missing)")
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
    from assistant_kernel.core.i18n import t
    lines = []
    for r in rows:
        tag = t("iter.major") if r["significance"] == "major" else ""
        source = r["source"] + ((" " + r["source_ref"]) if r.get("source_ref") else "")
        head = (f"- `{str(r['iteration_id'])[:8]}` {r['created_at']:%Y-%m-%d} {tag}{r['title']}"
                + t("iter.origin", kind=r["kind"], source=source))
        if brief:
            lines.append(f"{head}: {_text(r['reason'], 120)}")
            continue
        lines.append(head)
        lines.append("  - " + t("iter.reason", text=_text(r["reason"], 400)))
        for change in (r["changes"] or [])[:6]:
            what = (f"{change.get('type')} {change.get('target')} {change.get('action', '')}"
                    f"{(' ' + change['version']) if change.get('version') else ''}: {_text(change.get('note'), 160)}")
            lines.append("  - " + t("iter.change", text=what))
        if len(r["changes"] or []) > 6:
            lines.append("  - " + t("iter.more_changes", count=len(r["changes"]) - 6))
        for key in ("verification", "expected_effect", "measure", "followups"):
            if r.get(key):
                lines.append("  - " + t(f"iter.{key}", text=_text(r[key], 200)))
        if r.get("effect_status"):
            lines.append("  - " + t("iter.effect", status=t(f"iter.effect_{r['effect_status']}"),
                                    note=_text(r.get("effect_note"), 160)))
        if not r.get("complete", True):
            lines.append("  - " + t("iter.generated"))
    return "\n".join(lines) or t("iter.empty")
