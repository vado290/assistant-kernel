"""Project feedback inbox, backed by existing change_requests and Markdown files."""
from __future__ import annotations

import hashlib
import json
import os
import uuid

from psycopg.types.json import Jsonb

from assistant_kernel.repositories import ControlPlaneRepository, canonical_sha256
from assistant_kernel.storage import ProjectRuntimeManager


class FeedbackRepository:
    def __init__(self, settings):
        self.repository = ControlPlaneRepository(settings)
        self.runtime = ProjectRuntimeManager(settings.data_root)

    async def submit(self, project_id: str, feedback: dict, idempotency_key: str) -> dict:
        self.runtime.describe(project_id)
        feedback_id = str(uuid.uuid5(uuid.NAMESPACE_URL,
            json.dumps(["assistant-feedback", project_id, idempotency_key])))
        digest = canonical_sha256(feedback)
        async with self.repository.database.connection() as c:
            await c.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (feedback_id,))
            existing = await (await c.execute(
                "SELECT * FROM assistant.change_requests WHERE change_request_id=%s", (feedback_id,)
            )).fetchone()
            if existing:
                if existing["proposal"].get("request_sha256") != digest:
                    raise ValueError("idempotency key belongs to different feedback")
                # Restore a missing export after a filesystem failure; never overwrite edits.
                self._archive(project_id, feedback_id, feedback)
                return self._result(existing, True)
            if not await (await c.execute(
                "SELECT 1 FROM assistant.projects WHERE project_id=%s", (project_id,)
            )).fetchone():
                raise ValueError("project not found")
            task_id = feedback.get("task_id")
            if task_id and not await (await c.execute(
                "SELECT 1 FROM assistant.tasks WHERE task_id=%s AND project_id=%s", (task_id, project_id)
            )).fetchone():
                raise ValueError("task does not belong to this project")
            run_id = feedback.get("workflow_run_id")
            if run_id:
                run = await (await c.execute(
                    "SELECT r.task_id FROM assistant.workflow_runs r JOIN assistant.tasks t USING(task_id) "
                    "WHERE r.workflow_run_id=%s AND t.project_id=%s", (run_id, project_id)
                )).fetchone()
                if not run or (task_id and str(run["task_id"]) != task_id):
                    raise ValueError("run does not belong to this project/task")
                task_id = str(run["task_id"])
            message_id = feedback.get("source_message_id")
            if message_id and not await (await c.execute(
                "SELECT 1 FROM assistant.messages WHERE message_id=%s AND project_id_snapshot=%s",
                (message_id, project_id),
            )).fetchone():
                raise ValueError("message does not belong to this project")
            uri, archive_hash = self._archive(project_id, feedback_id, feedback)
            proposal = {
                "schema_version": "1.0", "kind": "user_feedback", "request_sha256": digest,
                "feedback": feedback, "storage_uri": uri, "content_sha256": archive_hash,
                "processing_status": "received", "optimizer_started": False,
            }
            row = await (await c.execute(
                "INSERT INTO assistant.change_requests(change_request_id,project_id,task_id,target_type,"
                "target_id,impact,proposal,evidence_refs) VALUES (%s,%s,%s,'user_feedback',%s,%s,%s,%s) RETURNING *",
                (feedback_id, project_id, task_id, feedback_id, feedback["impact"], Jsonb(proposal),
                 Jsonb([f"message://{message_id}"] if message_id else [])),
            )).fetchone()
            await c.commit()
            return self._result(row, False)

    def _archive(self, project_id: str, feedback_id: str, feedback: dict) -> tuple[str, str]:
        relative = f"artifacts/feedback/{feedback_id}.md"
        path = self.runtime.resolve_vault_file(project_id, relative)
        content = (
            f"# 人工改进反馈 {feedback_id}\n\n项目：{project_id}\n\n"
            "状态：已收集，尚未自动启动 Optimizer。\n\n"
            f"## 用户原话\n\n{feedback['text']}\n\n"
            f"## 期望结果\n\n{feedback.get('expected_behavior') or '尚未说明'}\n\n"
            "## 来源与关联\n\n" + json.dumps(
                {k: v for k, v in feedback.items() if k not in {'text', 'expected_behavior'}},
                ensure_ascii=False, indent=2,
            ) + "\n"
        ).encode("utf-8")
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if path.read_bytes() != content:
                raise ValueError("feedback archive differs from the recorded original")
        else:
            temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
            try:
                with temporary.open("xb") as f:
                    f.write(content)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
        return f"vault://{project_id}/{relative}", hashlib.sha256(content).hexdigest()

    @staticmethod
    def _result(row: dict, duplicate: bool = False) -> dict:
        return {"feedback_id": str(row["change_request_id"]), "project_id": row["project_id"],
                "status": row["proposal"]["processing_status"], "duplicate": duplicate,
                "storage_uri": row["proposal"]["storage_uri"],
                "content_sha256": row["proposal"]["content_sha256"],
                "feedback": row["proposal"]["feedback"], "created_at": row["created_at"],
                "optimizer_started": False}

    async def list(self, project_id: str, limit: int = 50) -> list[dict]:
        async with self.repository.database.connection() as c:
            rows = await (await c.execute(
                "SELECT * FROM assistant.change_requests WHERE project_id=%s AND target_type='user_feedback' "
                "ORDER BY created_at DESC,change_request_id DESC LIMIT %s", (project_id, min(max(limit, 1), 100)),
            )).fetchall()
        return [self._result(row) for row in rows]

    async def get(self, project_id: str, feedback_id: str) -> dict | None:
        async with self.repository.database.connection() as c:
            row = await (await c.execute(
                "SELECT * FROM assistant.change_requests WHERE project_id=%s AND change_request_id=%s "
                "AND target_type='user_feedback'", (project_id, feedback_id),
            )).fetchone()
        return self._result(row) if row else None
