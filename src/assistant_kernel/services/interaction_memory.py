"""Project-scoped interaction memory: append-only, rebuttable intent records.

Every entry states where it came from and whether the user confirmed it.
Only ``user_confirmed`` entries may feed mature intent output; history hits
are reference material and never promote an assumption into a fact.
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from assistant_kernel.config import KernelSettings
from assistant_kernel.storage import ProjectRuntimeManager


STATUSES = {"proposed", "user_confirmed", "rejected", "superseded"}
CREATORS = {"kernel", "supervisor", "user"}


def _normalize_entry(raw: dict[str, Any]) -> dict[str, Any]:
    status = str(raw.get("status") or "proposed")
    if status not in STATUSES:
        raise ValueError("interaction memory entry has an unsupported status")
    created_by = str(raw.get("created_by") or "kernel")
    if created_by not in CREATORS:
        raise ValueError("interaction memory entry has an unsupported creator")
    excerpt = str(raw.get("user_text_excerpt") or "").strip()
    if len(excerpt) > 200:
        excerpt = excerpt[:200]
    return {
        "id": str(raw.get("id") or f"mem-{uuid.uuid4().hex[:12]}"),
        "ts": str(raw.get("ts") or datetime.now(timezone.utc).isoformat()),
        "task_id": str(raw.get("task_id") or "") or None,
        "status": status,
        "supersedes_id": str(raw.get("supersedes_id")) if raw.get("supersedes_id") else None,
        "created_by": created_by,
        "user_text_excerpt": excerpt,
        "source_message_ids": [
            str(item) for item in raw.get("source_message_ids") or [] if str(item)
        ],
        "evidence_refs": [
            {"uri": str(item.get("uri")), "sha256": item.get("sha256")}
            for item in raw.get("evidence_refs") or []
            if isinstance(item, dict) and item.get("uri")
        ],
        "intent_statement": str(raw.get("intent_statement") or "").strip(),
        "tags": [str(item).strip() for item in raw.get("tags") or [] if str(item).strip()],
        "decision_ref": str(raw.get("decision_ref")) if raw.get("decision_ref") else None,
    }


class InteractionMemory:
    """Keyword/recency retrieval over an append-only JSONL knowledge file."""

    def __init__(self, settings: KernelSettings):
        self.runtime = ProjectRuntimeManager(settings.data_root)

    def _path(self, project_id: str) -> Path:
        return (
            self.runtime.ensure(project_id).vault_root
            / "knowledge" / "interaction-memory.jsonl"
        )

    def append(self, project_id: str, raw: dict[str, Any]) -> dict[str, Any]:
        entry = _normalize_entry(raw)
        target = self._path(project_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
        return entry

    def _read_all(self, project_id: str) -> list[dict[str, Any]]:
        target = self._path(project_id)
        if not target.is_file():
            return []
        entries: list[dict[str, Any]] = []
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
                    entries.append(payload)
        return entries

    @staticmethod
    def _tokens(text: str) -> set[str]:
        """ASCII words plus individual CJK characters (Chinese has no spaces)."""
        return set(re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]", text.lower()))

    def retrieve(
        self, project_id: str, query_terms: list[str], limit: int = 5,
        *, exclude_task_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Deterministic recency+keyword scoring; newest wins ties."""
        terms = {
            token for token in self._tokens(" ".join(query_terms))
            if len(token) > 1 or ("\u4e00" <= token <= "\u9fff")
        }
        scored: list[tuple[float, float, dict[str, Any]]] = []
        for index, entry in enumerate(self._read_all(project_id)):
            if exclude_task_id and entry.get("task_id") == exclude_task_id:
                continue
            haystack = " ".join(
                str(entry.get(field) or "")
                for field in ("intent_statement", "user_text_excerpt", "tags")
            )
            overlap = len(terms & self._tokens(haystack))
            if overlap == 0:
                continue
            scored.append((float(overlap), float(index), entry))
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        results = []
        for _, _, entry in scored[: max(1, limit)]:
            item = dict(entry)
            item.pop("evidence_refs", None)
            results.append(item)
        return results
