"""Deterministic reconciliation diagnostics and human intervention routing."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from assistant_kernel.config import KernelSettings
from assistant_kernel.repositories import CoreRepository
from assistant_kernel.storage import ProjectRuntimeManager
from assistant_kernel.worker.process_control import (
    process_start_ticks,
    terminate_recorded_process_group,
)


class ReconciliationService:
    def __init__(self, settings: KernelSettings, repository: CoreRepository):
        self.settings = settings
        self.repository = repository
        self.runtime = ProjectRuntimeManager(settings.data_root)

    def _trace_evidence(
        self, run: dict[str, Any]
    ) -> tuple[bool, str | None, list[dict[str, str]]]:
        executor = dict(run.get("executor_json") or {})
        uri = str(executor.get("trace_uri") or "")
        prefix = f"vault://{run['project_id']}/"
        if not uri.startswith(prefix):
            return False, None, []
        trace_root = self.runtime.describe(str(run["project_id"])).vault_root / uri[len(prefix):]
        trace = trace_root / "stdout.jsonl" if trace_root.is_dir() else trace_root
        if not trace.is_file():
            return False, None, []
        completed = False
        summary = None
        changed: dict[str, dict[str, str]] = {}
        with trace.open("r", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "turn.completed":
                    completed = True
                item = event.get("item") if event.get("type") == "item.completed" else None
                if isinstance(item, dict) and item.get("type") == "agent_message":
                    text = str(item.get("text") or "").strip()
                    if text:
                        summary = text
                if isinstance(item, dict) and item.get("type") == "file_change":
                    for change in item.get("changes") or []:
                        if not isinstance(change, dict):
                            continue
                        path = str(change.get("path") or "").strip()
                        if path:
                            changed[path] = {
                                "path": path,
                                "kind": str(change.get("kind") or "unknown"),
                            }
        return completed, summary, [changed[path] for path in sorted(changed)]

    def _trace_state(self, run: dict[str, Any]) -> tuple[bool, str | None]:
        completed, summary, _ = self._trace_evidence(run)
        return completed, summary

    def _candidate_state(self, run: dict[str, Any]) -> list[dict[str, str]]:
        vault = self.runtime.describe(str(run["project_id"])).vault_root
        manifest = vault / "runs" / str(run["run_id"]) / "candidate" / "manifest.json"
        if not manifest.is_file():
            return []
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        result: list[dict[str, str]] = []
        for item in payload.get("files") or []:
            if not isinstance(item, dict):
                continue
            target_uri = str(item.get("target_uri") or "").strip()
            if target_uri:
                result.append({
                    "target_uri": target_uri,
                    "operation": str(item.get("operation") or "unknown"),
                })
        return result

    def diagnose(self, run: dict[str, Any]) -> dict[str, Any]:
        pid = int(run.get("process_pid") or 0)
        expected_ticks = run.get("process_start_ticks")
        alive = bool(
            pid and expected_ticks is not None
            and process_start_ticks(pid) == int(expected_ticks)
        )
        trace_completed, summary, trace_changes = self._trace_evidence(run)
        candidate_changes = self._candidate_state(run)
        error = dict(run.get("error_json") or {})
        executor = dict(run.get("executor_json") or {})
        file_effects = dict(executor.get("file_effects") or {})
        changed_count = file_effects.get("changed_entry_count")
        if changed_count is None and trace_changes:
            changed_count = len(trace_changes)
        supersession = dict(executor.get("supersession") or {})
        superseded = bool(supersession.get("replacement_run_id"))
        recoverable = bool(
            not alive and trace_completed and summary and run.get("run_type") == "chat"
        )
        if alive:
            actions = [
                {"id": "wait", "label": "继续等待", "style": "secondary"},
                {"id": "cancel", "label": "终止并取消", "style": "danger"},
            ]
            recommended = "wait"
        elif recoverable:
            actions = [
                {"id": "recover", "label": "恢复已完成结果", "style": "success"},
                {"id": "retry", "label": "重新执行", "style": "secondary"},
                {"id": "fail", "label": "标记失败", "style": "danger"},
            ]
            recommended = "recover"
        elif superseded:
            actions = [
                {"id": "cancel", "label": "取消过期任务", "style": "success"},
                {"id": "fail", "label": "标记失败", "style": "secondary"},
            ]
            recommended = "cancel"
        elif candidate_changes and not changed_count:
            actions = [
                {"id": "resume", "label": "继续完成候选", "style": "success"},
                {"id": "cancel", "label": "取消任务", "style": "danger"},
                {"id": "fail", "label": "标记失败", "style": "secondary"},
            ]
            recommended = "resume"
        else:
            actions = [
                {"id": "retry", "label": "重新执行", "style": "success"},
                {"id": "fail", "label": "标记失败", "style": "secondary"},
                {"id": "cancel", "label": "取消任务", "style": "danger"},
            ]
            recommended = "fail" if changed_count else "retry"
        reason = str(error.get("message") or error.get("type") or "执行结果需要人工核对")
        text = (
            "任务异常，等待人工处理。\n"
            f"Project：{run['project_id']}\n"
            f"Run ID：{run['run_id']}\n"
            f"阶段：{run.get('stage') or run.get('run_type') or 'chat'}\n"
            f"原因：{reason[:700]}\n"
            f"进程：{'仍在运行' if alive else '已退出或不存在'}\n"
            f"Codex结果：{'已有完整结果，可直接恢复' if recoverable else ('Trace已完成但不能自动恢复' if trace_completed else '没有完整完成结果')}\n"
            f"文件变化记录：{changed_count if changed_count is not None else '未知'}\n"
            + (
                "已检测变更：" + "、".join(
                    item["path"] for item in trace_changes[:8]
                ) + (" 等" if len(trace_changes) > 8 else "") + "\n"
                if trace_changes else ""
            )
            + ("任务已被新的阶段修订取代。\n" if superseded else "")
            + (
                f"候选变更：{len(candidate_changes)} 个目标，尚未发布。\n"
                if candidate_changes else ""
            )
            + f"Kernel建议：{recommended}。请选择下一步。"
        )
        return {
            "text": text,
            "schema_version": "reconciliation-card.v1",
            "reason_type": str(error.get("type") or "unknown"),
            "reason_message": reason[:2000],
            "process_alive": alive,
            "trace_completed": trace_completed,
            "recoverable": recoverable,
            "recommended_action": recommended,
            "trace_file_changes": trace_changes,
            "candidate_changes": candidate_changes,
            "superseded": superseded,
            "changed_count": changed_count,
            "actions": actions,
        }

    async def notify_due(self) -> list[str]:
        queued: list[str] = []
        for run in await self.repository.list_reconciling_runs_due():
            message = await self.repository.queue_reconciliation_card(
                run_id=str(run["run_id"]), diagnostic=self.diagnose(run),
            )
            if message is not None:
                queued.append(str(message["message_id"]))
        return queued

    async def resolve_card(
        self, *, card_message_id: str, external_event_id: str,
        actor_user_id: str, action: str,
    ) -> dict[str, Any]:
        card = await self.repository.get_message(card_message_id)
        if card is None:
            raise PermissionError("reconciliation card is unknown")
        run = await self.repository.get_run(str(card.get("run_id") or ""))
        if run is None:
            raise PermissionError("reconciliation Run is unknown")
        alive = bool(
            run.get("process_pid") and run.get("process_start_ticks") is not None
            and process_start_ticks(int(run["process_pid"])) == int(run["process_start_ticks"])
        )
        if action in {"retry", "resume"} and alive:
            raise PermissionError("cannot resume while the recorded process is alive")
        if action == "cancel" and alive:
            await terminate_recorded_process_group(
                int(run["process_pid"]), int(run["process_start_ticks"]),
            )
        summary = None
        if action == "recover":
            completed, summary = self._trace_state(run)
            if not completed or not summary:
                raise ValueError("completed trace result is unavailable")
        return await self.repository.resolve_reconciliation_card(
            card_message_id=card_message_id,
            external_event_id=external_event_id,
            actor_user_id=actor_user_id,
            action=action,
            recovered_summary=summary,
        )
