"""Per-task document drafts: the PM edits them through Kernel tools, Kernel snapshots them into revisions.

A draft is ``runs/<task>/drafts/<kind>.json`` in the project vault: the working Markdown plus the sha of the
revision it started from. Only the document kind of the current stage can be edited. At turn end the PM
executor writes a new immutable revision when the draft differs from the current one; approvals stay bound
to revision shas exactly as before.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from assistant_kernel.storage import ProjectRuntimeManager

DOCUMENT_STAGE = {"task_contract": "requirements", "solution_design": "design",
                  "implementation_report": "build", "review_report": "review",
                  "improvement_proposal": "operate"}
STAGE_DOCUMENT = {stage: kind for kind, stage in DOCUMENT_STAGE.items()}
NOTES = "working_notes"   # the PM's own running notes (ROUND3_PLAN 4.2): editable in every stage, never approved
KINDS = (*DOCUMENT_STAGE, NOTES)


def editable(stage: str | None) -> set[str]:
    """Document kinds a PM turn in `stage` may edit: that stage's document and the working notes."""
    return {kind for kind in (STAGE_DOCUMENT.get(stage or ""), NOTES) if kind}


def text_sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def revision_markdown(settings, project_id: str, document: dict[str, Any] | None) -> str:
    """Markdown body of a stored revision ('' when there is none or it is unreadable)."""
    from assistant_kernel.services.document_runtime import TaskDocumentService
    if not document:
        return ""
    try:
        payload = TaskDocumentService(settings).read(project_id, document["ref"])["payload"]
    except (KeyError, OSError, ValueError, PermissionError):
        return ""
    return str(payload.get("markdown") or "")


def path(settings, project_id: str, task_id: str, kind: str) -> Path:
    vault = ProjectRuntimeManager(settings.data_root).ensure(project_id).vault_root
    return vault / "runs" / str(task_id) / "drafts" / f"{kind}.json"


def load(settings, project_id: str, task_id: str, kind: str) -> dict[str, Any] | None:
    target = path(settings, project_id, task_id, kind)
    return json.loads(target.read_text("utf-8")) if target.is_file() else None


def save(settings, project_id: str, task_id: str, kind: str, markdown: str, base_sha: str | None) -> dict[str, Any]:
    target = path(settings, project_id, task_id, kind)
    target.parent.mkdir(parents=True, exist_ok=True)
    draft = {"kind": kind, "base_sha256": base_sha, "markdown": markdown}
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(draft, ensure_ascii=False), "utf-8")
    temporary.replace(target)
    return draft


def ensure(settings, project_id: str, task_id: str, kind: str, document: dict[str, Any] | None, *,
           reset: bool = False) -> dict[str, Any]:
    """The draft for `kind`, (re)started from the current revision when it is missing, stale or `reset`.

    A draft based on the current revision is kept, so edits from a failed attempt in the same session survive.
    """
    base_sha = (document or {}).get("sha256")
    draft = None if reset else load(settings, project_id, task_id, kind)
    if draft is None or draft.get("base_sha256") != base_sha:
        draft = save(settings, project_id, task_id, kind, revision_markdown(settings, project_id, document), base_sha)
    return draft


def apply_edits(text: str, edits: list[dict[str, Any]]) -> str:
    """Apply exact-text replacements in order; every `old` must occur exactly once at the time it is applied."""
    for number, edit in enumerate(edits, 1):
        if not isinstance(edit, dict) or not isinstance(edit.get("old"), str) or not isinstance(edit.get("new"), str):
            raise ValueError(f"edit {number}: needs string fields old and new")
        old, count = edit["old"], text.count(edit["old"]) if edit["old"] else 0
        if count == 0:
            raise ValueError(f"edit {number}: old text not found in the draft (read_task_document shows the current text)")
        if count > 1:
            raise ValueError(f"edit {number}: old text occurs {count} times; include more surrounding text")
        text = text.replace(old, edit["new"], 1)
    return text
