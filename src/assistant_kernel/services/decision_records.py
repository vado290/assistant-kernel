"""Kernel-written user decision records: the authority layer-1 entity.

Decisions are appended when the user answers a question card, approves a
document, answers a Supervisor clarification, or adopts a recommended
prompt. Supervisor and PM agents read these as ``当前用户明确表达``.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from assistant_kernel.config import KernelSettings
from assistant_kernel.storage import ProjectRuntimeManager


KINDS = {
    "question_answer", "approval", "rejection", "supervisor_clarification",
    "prompt_adoption", "correction",
}


class DecisionRecordService:
    def __init__(self, settings: KernelSettings):
        self.runtime = ProjectRuntimeManager(settings.data_root)

    def _path(self, project_id: str, task_id: str) -> Path:
        return (
            self.runtime.ensure(project_id).vault_root
            / "runs" / task_id / "decisions" / "records.jsonl"
        )

    def append(
        self, project_id: str, task_id: str, *, kind: str,
        text: str, source_message_ids: list[str] | None = None,
        scope: dict[str, Any] | None = None,
        record_id: str | None = None, actor_user_id: str | None = None,
        target_ref: dict[str, Any] | None = None, choice: str | None = None,
        status: str = "active", expires_at: str | None = None,
        supersedes_ref: str | None = None, revokes_ref: str | None = None,
    ) -> dict[str, Any]:
        if kind not in KINDS:
            raise ValueError("unsupported decision kind")
        if status not in {"active", "superseded", "revoked"}:
            raise ValueError("unsupported decision status")
        decision_id = str(record_id or f"dec-{uuid.uuid4().hex[:12]}")
        effective_at = datetime.now(timezone.utc).isoformat()
        record = {
            "schema_version": "decision-record.v2",
            "id": decision_id,
            "decision_id": decision_id,
            "ts": effective_at,
            "effective_at": effective_at,
            "task_id": task_id,
            "kind": kind,
            "text": str(text).strip()[:2000],
            "actor_user_id": str(actor_user_id) if actor_user_id else None,
            "source_message_ids": [
                str(item) for item in source_message_ids or [] if str(item)
            ],
            "target_ref": dict(target_ref or {}) or None,
            "choice": str(choice) if choice is not None else None,
            "scope": dict(scope or {}),
            "status": status,
            "expires_at": str(expires_at) if expires_at else None,
            "supersedes_ref": str(supersedes_ref) if supersedes_ref else None,
            "revokes_ref": str(revokes_ref) if revokes_ref else None,
        }
        target = self._path(project_id, task_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        return record

    def recent(self, project_id: str, task_id: str, limit: int = 10) -> list[dict[str, Any]]:
        target = self._path(project_id, task_id)
        if not target.is_file():
            return []
        records: list[dict[str, Any]] = []
        with target.open("r", encoding="utf-8") as stream:
            for line in stream:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict):
                    records.append(payload)
        return records[-max(1, limit):]
