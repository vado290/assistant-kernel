"""Deterministic processor for Supervisor consultation Runs.

Owns the hard budget gate, the snapshot → panels
→ handoff → two-pass evaluation pipeline, decision persistence, and the
status card. Never triggers another consultation (recursion guard); only
Kernel-accepted Action Plans can change the PM control state.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID
from psycopg.types.json import Jsonb
import yaml

from assistant_kernel.config import KernelSettings
from assistant_kernel.repositories.core import CoreRepository, json_compatible
from assistant_kernel.services.goal_supervisor import (
    GoalSupervisorService, SupervisorDecisionContractError,
)
from assistant_kernel.services.document_runtime import TaskDocumentService, resource_ref
from assistant_kernel.storage import ProjectRuntimeManager


CARD_TEXT_LIMIT = 1900


class SupervisorPipelineError(RuntimeError):
    def __init__(self, step: str, cause: Exception):
        self.step = step
        self.cause = cause
        self.cause = cause
        super().__init__(f"Supervisor {step} failed: {type(cause).__name__}: {cause}")


def render_status_card(
    *, task_id: str, trigger: str, turn_number: int, decision: dict[str, Any],
) -> str:
    """Deterministic one-screen card; over-budget content is truncated."""
    hypotheses = list(decision.get("intent_hypothesis") or [])
    if decision.get("insufficient_evidence") or not hypotheses:
        intent_lines = ["还在理解你的目标（证据不足，未虚构推断）"]
    else:
        intent_lines = [
            f"{index + 1}. {item.get('statement')}（{item.get('confidence')}）"
            for index, item in enumerate(hypotheses[:3])
        ]
    reasons = [str(item) for item in decision.get("reason") or []][:2]
    recommended = str(
        (decision.get("next_prompt") or {}).get("recommended_user_prompt") or ""
    ).strip()
    if not recommended:
        recommended = str(
            (decision.get("next_prompt") or {}).get("assembled_prompt") or ""
        ).strip()
    lines = [
        f"🎯 Task `{task_id[:8]}` · 第 {turn_number} 轮 · {trigger}",
        "📋 意图假设",
        *[f"   {item}" for item in intent_lines],
        f"🚦 判定：{decision.get('decision')}"
        + (f" — {reasons[0]}" if reasons else ""),
    ]
    clarification = next(iter(decision.get("clarifications") or []), None)
    if clarification:
        lines.append(
            f"❓ 澄清：{str(clarification.get('question_text'))[:300]}"
        )
    user_message = str(decision.get("user_message") or "").strip()
    if user_message:
        lines.append(f"💬 {user_message[:400]}")
    if recommended:
        lines.append("💡 建议下一步（复制发送给 PM）：")
        lines.append("```text")
        lines.append(recommended)
        lines.append("```")
    lines.append("↩️ 对本条结果有疑问，直接回复这条消息与 Supervisor 讨论。")
    text = "\n".join(lines)
    if len(text) > CARD_TEXT_LIMIT:
        keep = CARD_TEXT_LIMIT - len(lines[-1]) - 20
        truncated = "\n".join(lines[:-1])[:keep] + "\n…（已截断）"
        text = truncated + "\n" + lines[-1]
    return text


class SupervisorProcessor:
    def __init__(
        self, settings: KernelSettings, repository: CoreRepository,
        supervisor: GoalSupervisorService | None = None,
    ):
        self.settings = settings
        self.repository = repository
        self.runtime = ProjectRuntimeManager(settings.data_root)
        self.supervisor = supervisor or GoalSupervisorService(settings, repository)
        self.task_documents = TaskDocumentService(settings)

    def _decision_path(self, project_id: str, task_id: str) -> Path:
        return (
            self.runtime.ensure(project_id).vault_root
            / "runs" / task_id / "supervisor"
        )

    def _persist_decision(
        self, project_id: str, task_id: str, decision: dict[str, Any], *,
        supervisor_run_id: str, attempt: int,
    ) -> dict[str, Any]:
        key = f"{supervisor_run_id}-a{max(attempt, 0)}"
        target = self._decision_path(project_id, task_id) / f"decision-{key}.json"
        encoded = json.dumps(
            decision, ensure_ascii=False, sort_keys=True, indent=2, default=str,
        ).encode("utf-8")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_bytes(encoded)
        temporary.replace(target)
        relative = target.relative_to(self.runtime.ensure(project_id).vault_root)
        return {
            "uri": f"vault://{project_id}/{relative.as_posix()}",
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "size": len(encoded),
        }

    def _update_supervisor_current(
        self, project_id: str, task_id: str, patch: dict[str, Any],
    ) -> dict[str, Any]:
        current = self.supervisor.current.read(project_id, task_id, "supervisor")
        payload = dict((current or {}).get("payload") or {})
        payload.update(json_compatible(patch))
        return self.supervisor.current.write(
            project_id, task_id, "supervisor", payload,
            expected_sha256=(current or {}).get("sha256"),
        )

    async def _queue_status(
        self, run: dict[str, Any], request: dict[str, Any], text: str,
        decision: str, prompt_sha256: str | None, card_revision: int = 0,
        decision_id: str | None = None, decision_ref: str | None = None,
        action_plan_ref: dict[str, Any] | None = None,
        recommended_prompt: str | None = None,
        confirmation_class: str = "user_required",
    ) -> None:
        trigger = (
            await self.repository.get_message(str(run["trigger_message_id"]))
            if run.get("trigger_message_id") else None
        )
        channel = str((trigger or {}).get("channel") or request.get("channel") or "")
        chat_id = str((trigger or {}).get("chat_id") or request.get("chat_id") or "")
        user_id = (trigger or {}).get("user_id") or request.get("user_id")
        task_id = str(request.get("task_id") or "")
        if not channel or not chat_id or not task_id:
            return
        delivery_route = "trigger_channel"
        if channel == "discord":
            # v6.1: the card lives in the Project's dedicated supervisor
            # channel when one is registered; the trigger chat is the fallback.
            from assistant_kernel.services.discord_interfaces import (
                DiscordInterfaceRegistry,
            )
            try:
                interface = DiscordInterfaceRegistry(self.settings).find(
                    project_id=str(run["project_id"]), role="supervisor"
                )
            except (OSError, ValueError):
                interface = None
            if interface is not None:
                chat_id = str(interface["channel_id"])
                delivery_route = "project_supervisor_interface"
        await self.repository.queue_supervisor_status(
            project_id=str(run["project_id"]), run_id=task_id,
            channel=channel, chat_id=chat_id,
            user_id=str(user_id) if user_id else None,
            text=text,
            turn_number=int(request.get("turn_number") or 0),
            decision=decision, prompt_sha256=prompt_sha256,
            card_revision=card_revision,
            delivery_route=delivery_route,
            decision_id=decision_id,
            decision_ref=decision_ref,
            action_plan_ref=action_plan_ref,
            recommended_prompt=recommended_prompt,
            confirmation_class=confirmation_class,
            source_context_epoch=int(request.get("source_context_epoch") or 0),
            source_turn_number=int(
                request.get("source_turn_number") or request.get("turn_number") or 0
            ),
            source_supervisor_run_id=str(run["run_id"]),
        )

    async def _pause(self, run: dict[str, Any], request: dict[str, Any], reason: str) -> None:
        task_id = str(request.get("task_id") or "")
        archive_key = task_id or str(request.get("consultation_id") or f"consult-{run['run_id']}")
        failed_brief = self.task_documents.write_revision(
            project_id=str(run["project_id"]), task_id=archive_key, document_kind="supervisor-brief",
            document_schema="supervisor-brief.v1", status="failed", actor_type="kernel", actor_id="kernel",
            run_id=str(run["run_id"]), reason=reason, source_refs=[],
            payload={"schema_version": "supervisor-brief.v1", "evaluation_status": "not_evaluated",
                     "source": {"supervisor_run_id": str(run["run_id"]), "context_epoch": request.get("source_context_epoch")},
                     "project_assessment": {"status": "not_evaluated", "confidence": "low"},
                     "user_intent_context": {"status": "not_evaluated", "scope": "project_only", "hypotheses": []}},
        )
        request["failure_brief_ref"] = failed_brief
        self._update_supervisor_current(str(run["project_id"]), archive_key, {
            "schema_version": "supervisor-current.v2", "status": "paused",
            "evaluation_status": "not_evaluated", "failure_reason": reason,
            "last_attempt_run_id": str(run["run_id"]),
            "failure_brief_ref": failed_brief,
        })
        if task_id:
            await self.repository.pause_supervision(
                task_id=task_id, turn_number=int(request.get("turn_number") or 0),
                reason=reason, retry_run_id=str(run["run_id"]),
            )
            return
        await self.repository.create_outbound(
            project_id=str(run["project_id"]), session_id=None, run_id=str(run["run_id"]),
            channel=str(request.get("channel") or "discord"), chat_id=str(request.get("chat_id") or ""),
            user_id=request.get("user_id"), message_type="kernel_notice",
            content={"text": f"⏸️ 本次只读咨询尚未完成：{reason}。可重试或取消；未执行项目变更。",
                     "origin": "kernel", "context_visibility": "ui_only",
                     "interaction_kind": "supervision_recovery", "task_id": None,
                     "action_status": "waiting",
                     "supervisor_run_id": str(run["run_id"]), "turn_number": 0,
                     "phase": "consultation", "context_epoch": 0, "cancel_only_mainline": False,
                     "actions": [{"id": "retry", "label": "重试监督"},
                                 {"id": "wait", "label": "暂时等待"},
                                 {"id": "cancel", "label": "取消咨询"}]},
            reply_to_message_id=run.get("trigger_message_id"),
            idempotency_key=f"supervision-recovery:{run['run_id']}:{run.get('attempt', 0)}",
        )

    async def _wait_on_questions(
        self, run: dict[str, Any], request: dict[str, Any], decision: dict[str, Any],
        decision_ref: dict[str, Any], archive_key: str, worker_instance_id: str,
    ) -> dict[str, Any]:
        project_id = str(run["project_id"])
        channel = str(request.get("channel") or "discord")
        chat_id = str(request.get("chat_id") or "")
        mainline_task_id = str(request.get("task_id") or "")
        request['control_run_id']=str(run['run_id'])
        if mainline_task_id:
            mainline = await self.repository.get_run(mainline_task_id)
            if (mainline or {}).get("waiting_question_message_id") or (
                (mainline or {}).get("request_json") or {}
            ).get("pending_interaction_ref"):
                await self.repository.defer_supervisor_questions(
                    task_id=mainline_task_id, supervisor_run_id=str(run["run_id"]),
                    questions=decision["clarifications"], decision_ref=decision_ref,
                )
                self._update_supervisor_current(project_id, archive_key, {
                    "schema_version": "supervisor-current.v2", "status": "questions_deferred",
                    "evaluation_status": "evaluated",
                    "brief_ref": decision.get("brief_ref"),
                    "project_assessment": decision.get("project_assessment"),
                    "user_intent_context": decision.get("user_intent_context"),
                    "pending_supervisor_questions": decision["clarifications"],
                    "last_decision": decision_ref,
                })
                await self._queue_status(run, request,
                    "Supervisor补充问题已保存。请先完成当前业务问卷；提交后统一评估是否仍需补问。",
                    "questions_deferred", None, confirmation_class="invalid")
                return await self._finish(str(run["run_id"]), worker_instance_id,
                    str(run.get("claim_token") or ""),
                    {"outcome": "questions_deferred", "decision_ref": decision_ref},
                    artifact_refs=[decision_ref["uri"]])
        session = await self.repository.ensure_session(
            project_id, str(run["agent_key"]), channel, chat_id,
        )
        self._update_supervisor_current(project_id, archive_key, {
            "schema_version": "supervisor-current.v2", "status": "waiting_user",
            "evaluation_status": "evaluated",
            "brief_ref": decision.get("brief_ref"),
            "project_assessment": decision.get("project_assessment"),
            "user_intent_context": decision.get("user_intent_context"),
            "open_questions": decision["clarifications"], "last_decision": decision_ref,
        })
        message = await self.repository.finalize_agent_waiting(
            run_id=str(run["run_id"]), worker_instance_id=worker_instance_id,
            claim_token=str(run.get("claim_token") or ""),
            session_id=str(session["session_id"]),
            native_session_id=str(decision.get("request_id") or session.get("native_session_id") or ""),
            input_tokens=0,  # Interaction archive only; Supervisor never resumes this native Session.
            context_window_tokens=self.settings.default_context_window_tokens,
            archive_uri=decision.get("trace_uri"), archive_sha256=decision.get("trace_sha256"),
            project_id=project_id, channel=channel, chat_id=chat_id, user_id=request.get("user_id"),
            source_message_id=str(run.get("trigger_message_id") or ""),
            question={"questions": decision["clarifications"],
                      "summary": str(decision.get("user_message") or ""),
                      "stage": "Supervisor", "owner_role": "supervisor",
                      "task_id": request.get("task_id"), "consultation_id": request.get("consultation_id")},
            artifact_refs=[decision_ref["uri"]], usage_snapshot=decision.get("usage") or {},
        )
        if mainline_task_id and message.get("message_id"):
            await self.repository.bind_supervisor_interaction(
                task_id=mainline_task_id, supervisor_run_id=str(run["run_id"]),
                message_id=str(message["message_id"]),
            )
        return {"outcome": "needs_input", "question_message_ids": message.get("question_message_ids") or [],
                "decision_ref": decision_ref["uri"], "task_id": request.get("task_id")}

    async def _finish_readonly_mainline(
        self, run: dict[str, Any], request: dict[str, Any], decision: dict[str, Any],
        decision_ref: dict[str, Any], worker_instance_id: str, *, reason: str,
    ) -> dict[str, Any]:
        # Preserve research and its evidence without changing phase, approvals,
        # blocking interactions or the mainline's active execution contract.
        await self.repository.create_outbound(
            project_id=str(run["project_id"]), session_id=None, run_id=str(run["run_id"]),
            channel=str(request.get("channel") or "discord"), chat_id=str(request.get("chat_id") or ""),
            user_id=request.get("user_id"), message_type="reply",
            content={"text": str(decision.get("user_message") or "\n".join(decision.get("reason") or []))
                     + "\n本轮建议已只读归档；主线运行或恢复状态保持原状。",
                     "origin": "supervisor", "context_visibility": "semantic",
                     "decision_ref": decision_ref, "readonly_reason": reason},
            reply_to_message_id=run.get("trigger_message_id"),
            idempotency_key=f"supervisor-readonly:{run['run_id']}:{run.get('attempt', 0)}",
        )
        return await self._finish(str(run["run_id"]), worker_instance_id,
            str(run.get("claim_token") or ""),
            {"outcome": "consultation_readonly", "decision_ref": decision_ref,
             "brief_ref": decision.get("brief_ref"), "reason": reason},
            artifact_refs=[decision_ref["uri"]])

    async def _prepare_task_actions(self, project_id, request, snapshot, decision):
        actions = []
        for index, raw in enumerate(decision.get("proposed_actions") or []):
            action = dict(raw)
            candidates = [action.get("source_message_id")] if action.get("source_message_id") else [
                request.get("source_message_id"),
                *(item.get("message_id") for item in reversed(snapshot.get("interaction_delta") or [])
                  if item.get("direction") == "inbound"),
            ]
            source = None
            for source_id in candidates:
                if not source_id:
                    continue
                try:
                    UUID(str(source_id))
                except (ValueError, TypeError):
                    continue
                message = await self.repository.get_message(str(source_id))
                content = (message or {}).get("content_json") or {}
                if (message and str(message.get("project_id")) == project_id
                    and message.get("direction") == "inbound" and message.get("message_type") in {"text", "answer"}
                    and message.get("user_id") and content.get("context_visibility") not in {"ui_only", "audit_only"}
                    and not (content.get("discord") or {}).get("author_bot")):
                    source = str(message["message_id"])
                    break
            if not source or (action["kind"] == "queue_task" and not str(action.get("goal") or "").strip()):
                decision.setdefault("clarifications", []).append({"question_id": f"sup-task-action-{index}",
                    "question_text": "请确认任务路由目标；当前建议没有完整目标或可核验的本项目用户消息来源。", "required": True})
                continue
            action["source_message_id"] = source
            if action["kind"] != "queue_task":
                target = str(action.get("target_task_id") or request.get("task_id") or "")
                try:
                    UUID(target)
                except (ValueError, TypeError):
                    target = ""
                found = await self.repository.get_run(target) if target else None
                valid = found and str(found.get("project_id")) == project_id
                if action["kind"] == "suspend_task":
                    valid = valid and target == str(request.get("task_id") or "")
                else:
                    valid = valid and (found.get("request_json") or {}).get("task_activation") == "suspended"
                if not valid:
                    decision.setdefault("clarifications", []).append({"question_id": f"sup-task-action-{index}",
                        "question_text": "请确认本项目内要挂起或恢复的任务；建议目标不是合法的当前主线或已挂起任务。", "required": True})
                    continue
                action["target_task_id"] = target
            actions.append(action)
        decision["proposed_actions"] = actions

    async def _execute_task_actions(self, project_id, decision):
        from assistant_kernel.services.task_queue import ProjectTaskQueue
        queue = ProjectTaskQueue(self.settings, self.repository)
        results = []
        for action in decision.get("proposed_actions") or []:
            try:
                if action["kind"] == "queue_task":
                    item = await queue.queue(project_id, str(action["goal"]), action["source_message_id"],
                                             priority=str(action.get("priority") or "normal"))
                    ref = resource_ref(kind="message", identity=str(item["message_id"]), purpose="queued_work_request")
                elif action["kind"] == "suspend_task":
                    item = await queue.suspend(action["target_task_id"], str(action.get("reason") or "Supervisor safe-boundary suspension"))
                    ref = resource_ref(kind="source", identity=str(item["run_id"]), purpose="suspended_project_task")
                else:
                    item = await queue.resume(action["target_task_id"])
                    ref = resource_ref(kind="source", identity=str(item["run_id"]), purpose="resumed_project_task")
                results.append({"kind": action["kind"], "status": "accepted", "ref": ref})
            except Exception as exc:
                results.append({"kind": action["kind"], "status": "blocked", "error_type": type(exc).__name__, "reason": str(exc)[:500]})
        if any(item.get("kind") == "queue_task" and item.get("status") == "accepted" for item in results):
            try:
                activated = await queue.activate_next(project_id)
                results.append({"kind": "activate_next", "result": activated})
            except Exception as exc:
                results.append({"kind": "activate_next", "status": "pending", "error_type": type(exc).__name__})
        return results

    async def _capture_brief_map(self, run, decision, *, previous_ref=None, state_revision=0, context_epoch=0):
        ref = decision.get("brief_ref")
        if not isinstance(ref, dict) or ref.get("kind") != "document":
            return
        try:
            from assistant_kernel.services.project_changes import ProjectChangeService
            from assistant_kernel.services.workspace_scope import WorkspaceScopeService
            previous = previous_ref if isinstance(previous_ref, dict) and previous_ref.get("kind") == "document" else None
            receipt = self.task_documents.change_receipt(str(run["project_id"]), ref,
                previous_ref=previous, purpose="Supervisor project assessment and short-lived intent context",
                state_revision=state_revision, context_epoch=context_epoch)
            if receipt:
                roots = WorkspaceScopeService(self.settings).resolve(str(run["project_id"]),
                    str((run.get("request_json") or {}).get("task_id") or run["run_id"]), "requirements")
                captured = await ProjectChangeService(self.settings, self.repository.database).capture_batch(
                    str(run["project_id"]), batch_id=f"supervisor-brief:{run['run_id']}:{ref['revision_id']}",
                    roots=roots, changes=[receipt], task_id=(run.get("request_json") or {}).get("task_id"),
                    run_id=str(run["run_id"]), actor="kernel", scope_kind="artifact",
                    reason="Kernel recorded this Supervisor assessment document; it grants no approval or permissions.",
                    reason_source_refs=[ref])
                if captured.get("map_refresh_pending"):
                    logging.warning("Supervisor brief accepted; Map refresh is pending")
        except Exception:
            logging.exception("Supervisor brief reference preserved; Map capture pending without model replay")

    async def process(
        self, run: dict[str, Any], worker_instance_id: str,
        cancel_event: Any = None,
    ) -> dict[str, Any]:
        request = dict(run.get("request_json") or {})
        request["supervisor_run_id"] = str(run["run_id"])
        request["control_run_id"] = str(run["run_id"])
        if request.get("interaction_kind") != "supervisor_consultation":
            raise ValueError("SupervisorProcessor requires interaction_kind supervisor_consultation")
        project_id = str(run["project_id"])
        mainline_task_id = str(request.get("task_id") or "")
        task_id = mainline_task_id or str(request.get("consultation_id") or f"consult-{run['run_id']}")
        if not mainline_task_id:
            request["consultation_id"] = task_id
            request["consultation_run_id"] = str(run["run_id"])
        trigger = str(request.get("trigger") or "user_consultation")
        run_id = str(run["run_id"])
        claim_token = str(run.get("claim_token") or "")
        attempt = int(run.get("attempt") or 0)

        async def process_started(pid: int, start_ticks: int | None) -> None:
            recorded = await self.repository.record_process_identity(
                run_id, worker_instance_id, claim_token, pid, start_ticks
            )
            if not recorded:
                raise RuntimeError("Supervisor consultation lease was lost at process start")
        try:
            resolver = getattr(self.supervisor, 'invocation_configuration', None)
            config = (await resolver(project_id, run) if callable(resolver)
                else self.supervisor.configuration(project_id))
        except (ValueError, OSError, yaml.YAMLError) as exc:
            await self._pause(run, request, f"Supervisor configuration unavailable: {type(exc).__name__}")
            return await self._finish(run_id, worker_instance_id, claim_token,
                {"outcome": "paused_configuration_error", "error_type": type(exc).__name__, "brief_ref": request.get("failure_brief_ref")})
        if not config["enabled"]:
            await self._pause(run, request, "Supervisor disabled")
            return await self._finish(
                run_id, worker_instance_id, claim_token,
                {"outcome": "skipped_disabled", "summary": "supervisor disabled", "brief_ref": request.get("failure_brief_ref")},
            )

        turn_number = int(request.get("turn_number") or 0)
        usage_rollup = await self.repository.project_usage_rollup(project_id)
        supervisor_usage = next((
            item for item in usage_rollup.get("by_actor_role") or []
            if item.get("actor_role") == "goal_supervisor"
        ), {})
        budget_reason = None
        if turn_number > int(config.get("max_task_turns") or 24):
            budget_reason = "task_turn_limit"
        elif int(supervisor_usage.get("input_tokens") or 0) >= int(
            config.get("daily_input_tokens") or 200_000
        ):
            budget_reason = "daily_input_tokens"
        elif int(supervisor_usage.get("output_tokens") or 0) >= int(
            config.get("daily_output_tokens") or 40_000
        ):
            budget_reason = "daily_output_tokens"
        if budget_reason:
            self._update_supervisor_current(project_id, task_id, {
                "schema_version": "supervisor-current.v2",
                "status": "skipped_due_to_budget",
                "budget_reason": budget_reason,
                "usage_rollup": usage_rollup,
            })
            await self._pause(run, request, f"Supervisor budget gate: {budget_reason}")
            return await self._finish(
                run_id, worker_instance_id, claim_token,
                {"outcome": "skipped_due_to_budget", "reason": budget_reason, "brief_ref": request.get("failure_brief_ref")},
            )

        trigger_message = (
            await self.repository.get_message(str(run["trigger_message_id"]))
            if run.get("trigger_message_id") else None
        )
        try:
            if config.get('result_protocol_revision')=='1.2':
                from assistant_kernel.services.supervisor_v12 import SupervisorV12
                protocol=SupervisorV12(self.settings,self.repository)
                compilation_context=await protocol.prepare_context(run)
                snapshot=protocol.model_context(run,compilation_context,trigger_message)
                snapshot['meta']['trigger']=trigger
            else:
                snapshot = await self.supervisor.build_snapshot(
                    project_id=project_id, task_id=task_id, trigger=trigger,
                    current_message=trigger_message,
                    agent_turn_report=request.get("agent_turn_report"),
                    consultation_context=request,
                )
            snapshot['supervisor_configuration'] = config
        except Exception as exc:
            if isinstance(exc, (ValueError, PermissionError, yaml.YAMLError)):
                await self._pause(run, request, f"Supervisor evidence unavailable: {type(exc).__name__}")
                return await self._finish(run_id, worker_instance_id, claim_token,
                    {"outcome": "paused_evidence_error", "error_type": type(exc).__name__, "brief_ref": request.get("failure_brief_ref")})
            raise SupervisorPipelineError("snapshot", exc) from exc

        supervisor_now = dict(
            (self.supervisor.current.read(project_id, task_id, "supervisor") or {})
            .get("payload") or {}
        )
        intent_panel = None
        has_new_user_text = any(
            str(item.get("direction") or "") == "inbound"
            and bool(str(item.get("text") or "").strip())
            for item in snapshot.get("interaction_delta") or []
        )
        trigger_snapshot = dict(snapshot.get("trigger_message") or {})
        has_new_user_text = has_new_user_text or (
            str(trigger_snapshot.get("direction") or "") == "inbound"
            and bool(str(trigger_snapshot.get("text") or "").strip())
        )
        if has_new_user_text and config.get('result_protocol_revision')!='1.2':
            try:
                intent_panel = await self.supervisor.run_intent_panel(
                    project_id=project_id, task_id=task_id, snapshot=snapshot,
                    cancel_event=cancel_event,
                    process_started_callback=process_started,
                )
            except Exception:
                logging.exception(
                    "supervisor intent panel failed for task %s; continuing without it",
                    task_id,
                )

        try:
            decision = await self.supervisor.run_consultation(
                project_id=project_id, task_id=task_id,
                snapshot=snapshot, intent_panel=intent_panel,
                artifact_key=f"{run_id}-a{attempt}",
                cancel_event=cancel_event,
                process_started_callback=process_started,
            )
        except SupervisorDecisionContractError as exc:
            await self._pause(run, request, "Supervisor returned an invalid decision contract")
            try:
                await self._queue_status(
                    run, request,
                    (
                        "Supervisor本轮输出合同无效，未采用任何Action Plan。"
                        "PM主线保持暂停，请使用恢复卡重新评估；未放行执行。"
                    ),
                    "degraded_invalid_decision", None,
                    card_revision=int(supervisor_now.get("card_revision") or 0) + 1,
                    decision_id=None, decision_ref=None,
                    recommended_prompt=None, confirmation_class="invalid",
                )
            except Exception:
                logging.exception(
                    "degraded Supervisor status could not be queued for task %s",
                    task_id,
                )
            return await self._finish(
                run_id, worker_instance_id, claim_token,
                {
                    "outcome": "degraded_invalid_decision",
                    "task_resumed": False,
                    "invalid_decision": exc.invalid_decision,
                    "trace_uri": exc.trace_uri,
                    "trace_sha256": exc.trace_sha256,
                    "brief_ref": request.get("failure_brief_ref"),
                },
                artifact_refs=[exc.trace_uri] if exc.trace_uri else [],
            )
        except Exception as exc:
            if isinstance(exc, (ValueError, PermissionError)):
                await self._pause(run, request, f"Supervisor decision evidence invalid: {type(exc).__name__}")
                return await self._finish(run_id, worker_instance_id, claim_token,
                    {"outcome": "paused_invalid_evidence", "error_type": type(exc).__name__, "brief_ref": request.get("failure_brief_ref")})
            raise SupervisorPipelineError("main_evaluation", exc) from exc
        if not await self.repository.run_claim_is_active(
            run_id, worker_instance_id, claim_token
        ):
            raise RuntimeError(
                "Supervisor consultation lease was lost before persistence"
            )
        if decision.get('protocol_revision')=='1.2':
            from assistant_kernel.services.supervisor_v12 import SupervisorV12
            return await SupervisorV12(self.settings,self.repository).settle(control=run,
                worker_instance_id=worker_instance_id,decision=decision,cancel_event=cancel_event)
        try:
            decision_ref = self._persist_decision(
                project_id, task_id, decision,
                supervisor_run_id=run_id, attempt=attempt,
            )
        except Exception as exc:
            raise SupervisorPipelineError("persistence", exc) from exc
        decision_id = "supdec-" + decision_ref["sha256"][:16]

        if mainline_task_id and callable(getattr(self.repository, "get_run", None)):
            head = await self.repository.get_run(mainline_task_id)
            if head and (str(head.get("status")) in {
                "running", "reconciling", "succeeded", "failed", "cancelled", "dead_letter",
            } or head.get("cancel_requested_at")):
                return await self._finish_readonly_mainline(run, request, decision, decision_ref,
                    worker_instance_id, reason=f"mainline_{head.get('status')}")

        source_context_epoch = int(request.get("source_context_epoch") or 0)
        if mainline_task_id and not await self.repository.supervisor_context_is_current(
            task_id=task_id, source_context_epoch=source_context_epoch,
        ):
            return await self._finish(
                run_id, worker_instance_id, claim_token,
                {
                    "outcome": "evaluated_stale",
                    "decision": decision.get("decision"),
                    "decision_ref": decision_ref["uri"],
                    "decision_id": decision_id,
                    "source_context_epoch": source_context_epoch,
                    "usage": decision.get("usage"),
                },
                artifact_refs=[decision_ref["uri"]],
            )

        await self._prepare_task_actions(project_id, request, snapshot, decision)
        if decision.get("clarifications"):
            return await self._wait_on_questions(
                run, request, decision, decision_ref, task_id, worker_instance_id,
            )
        if not mainline_task_id:
            task_action_results = await self._execute_task_actions(project_id, decision)
            await self._capture_brief_map(run, decision, previous_ref=supervisor_now.get("brief_ref"))
            self._update_supervisor_current(project_id, task_id, {
                "schema_version": "supervisor-current.v2", "status": "ready",
                "evaluation_status": "evaluated",
                "consultation_id": task_id, "project_assessment": decision.get("project_assessment"),
                "user_intent_context": decision.get("user_intent_context"),
                "brief_ref": decision.get("brief_ref"), "last_decision": decision_ref,
            })
            await self.repository.create_outbound(
                project_id=project_id, session_id=None, run_id=run_id,
                channel=str(request.get("channel") or (trigger_message or {}).get("channel")),
                chat_id=str(request.get("chat_id") or (trigger_message or {}).get("chat_id")),
                user_id=request.get("user_id"), message_type="reply",
                content={"text": str(decision.get("user_message") or "\n".join(decision.get("reason") or [])),
                         "origin": "supervisor", "context_visibility": "semantic",
                         "consultation_id": task_id, "decision_ref": decision_ref},
                reply_to_message_id=str((trigger_message or {}).get("message_id") or "") or None,
                idempotency_key=f"supervisor-consultation-result:{run_id}:{attempt}",
            )
            return await self._finish(run_id, worker_instance_id, claim_token,
                {"outcome": "consultation_reply", "decision_ref": decision_ref,
                 "consultation_id": task_id, "task_action_results": task_action_results, "brief_ref": decision.get("brief_ref")}, artifact_refs=[decision_ref["uri"]])

        recommended_prompt = str(
            (decision.get("next_prompt") or {}).get("recommended_user_prompt")
            or (decision.get("next_prompt") or {}).get("assembled_prompt")
            or ""
        )
        confirmation_class = (
            "safe"
            if (
                (trigger == "after_agent_turn" or (
                    trigger == "user_consultation" and bool(decision.get("proposed_actions"))
                    and not decision.get("delegations") and decision.get("session_action", "keep") == "keep"
                    and decision.get("target_work_mode") in {None, (snapshot.get("task_state") or {}).get("state", {}).get("current_stage")}
                ))
                and str(decision.get("decision") or "") not in {
                    "needs_user_input", "reconcile_required", "blocked", "failed",
                }
                and not decision.get("clarifications")
                and not bool(decision.get("insufficient_evidence"))
            )
            else "user_required"
        )
        current_lifecycle = dict(snapshot.get("task_lifecycle") or {})
        target_work_mode = str(
            decision.get("target_work_mode")
            or current_lifecycle.get("current_stage")
            or "requirements"
        )
        delegation_records: list[dict[str, Any]] = []
        for delegation in decision.get("delegations") or []:
            normalized = dict(delegation)
            delegation_id = str(normalized.get("delegation_id") or "")
            kind_suffix = hashlib.sha256(delegation_id.encode("utf-8")).hexdigest()[:20]
            request_ref = self.task_documents.write_revision(
                project_id=project_id, task_id=task_id,
                document_kind=f"delegation-{kind_suffix}",
                document_schema="delegation-request.v1",
                payload={
                    **normalized,
                    "capability_inheritance": "full_parent",
                },
                status="requested", actor_type="supervisor",
                actor_id=f"project:{project_id}:supervisor", run_id=run_id,
                reason="Supervisor delegated bounded Task work",
                source_refs=[resource_ref(
                    kind="source", identity=decision_id, uri=decision_ref["uri"],
                    sha256=decision_ref["sha256"], purpose="supervisor_decision",
                )],
            )
            delegation_records.append({**normalized, "request_ref": request_ref})
        action_payload = {
            "plan_id": f"action-{decision_id}",
            "evaluated_turn_ref": request.get("turn_handoff_ref"),
            "source_context_epoch": source_context_epoch,
            "decision": decision.get("decision"),
            "reason": list(decision.get("reason") or []),
            "evidence_refs": list(decision.get("evidence_refs") or []),
            "missing_evidence": list(decision.get("missing_evidence") or []),
            "target_work_mode": target_work_mode,
            "session_action": str(decision.get("session_action") or "keep"),
            "context_requests": list(decision.get("fetch_requests") or []),
            "delegations": delegation_records,
            "confirmation_class": confirmation_class,
            "pm_instruction": recommended_prompt,
            "selected_skills": list(decision.get("selected_skills") or []),
            "proposed_actions": list(decision.get("proposed_actions") or []),
        }
        action_ref = self.task_documents.write_revision(
            project_id=project_id, task_id=task_id,
            document_kind="supervisor-action-plan",
            document_schema="supervisor-action-plan.v1",
            payload=action_payload, status="proposed",
            actor_type="supervisor", actor_id=f"project:{project_id}:supervisor",
            run_id=run_id, reason=f"Supervisor evaluation: {trigger}",
            source_refs=[resource_ref(
                kind="source", identity=decision_id, uri=decision_ref["uri"],
                sha256=decision_ref["sha256"], purpose="supervisor_decision",
            )],
        )
        commit_action = getattr(self.repository, "commit_supervisor_action", None)
        if callable(commit_action):
            try:
                commit_result = await commit_action(
                    supervisor_run_id=run_id, worker_instance_id=worker_instance_id,
                    claim_token=claim_token, task_id=task_id,
                    source_context_epoch=source_context_epoch,
                    source_turn_number=int(
                        request.get("source_turn_number") or request.get("turn_number") or 0
                    ),
                    action_ref=action_ref, confirmation_class=confirmation_class,
                    pm_instruction=recommended_prompt,
                    target_work_mode=target_work_mode,
                    delegations=delegation_records,
                    result={
                        "outcome": "evaluated",
                        "decision": decision.get("decision"),
                        "decision_ref": decision_ref["uri"],
                        "action_plan_ref": action_ref,
                        "confirmation_class": confirmation_class,
                        "usage": decision.get("usage"),
                        "selected_skills": list(decision.get("selected_skills") or []),
                        "brief_ref": decision.get("brief_ref"),
                    },
                    artifact_refs=[decision_ref["uri"], action_ref["uri"]],
                    session_action=str(action_payload["session_action"]),
                )
            except PermissionError:
                # Cancellation/new input can win after the preflight. A stale
                # control proposal is settled as evidence, never retried as a
                # temporary execution failure that might block the PM.
                return await self._finish_readonly_mainline(run, request, decision, decision_ref,
                    worker_instance_id, reason="control_commit_superseded")
        else:
            await self._finish(
                run_id, worker_instance_id, claim_token,
                {"outcome": "evaluated", "decision_ref": decision_ref["uri"]},
                artifact_refs=[decision_ref["uri"], action_ref["uri"]],
            )
            commit_result = {"state_revision": 1}
        await self._capture_brief_map(run, decision,
            previous_ref=(snapshot.get("task_state", {}).get("supervisor_brief") or {}).get("ref"),
            state_revision=int(commit_result["state_revision"]), context_epoch=source_context_epoch)
        self.task_documents.update_current(
            project_id=project_id, task_id=task_id,
            document_kind="supervisor-action-plan", ref=action_ref,
            commit_id=f"supervisor-action:{run_id}",
            state_revision=int(commit_result["state_revision"]),
        )
        task_action_results = await self._execute_task_actions(project_id, decision) if confirmation_class == "safe" else []
        if (commit_result.get("task") or {}).get("status") == "succeeded":
            from assistant_kernel.services.task_queue import ProjectTaskQueue
            try:
                task_action_results.append({"kind": "activate_next", "result": await ProjectTaskQueue(self.settings, self.repository).activate_next(project_id)})
            except Exception as exc:
                task_action_results.append({"kind": "activate_next", "status": "pending", "error_type": type(exc).__name__})
        if task_action_results:
            async with self.repository.database.connection() as connection:
                await connection.execute("UPDATE assistant.runs SET result_json=COALESCE(result_json,'{}'::jsonb) || %s WHERE run_id=%s AND project_id=%s AND status='succeeded'",
                    (Jsonb(json_compatible({"task_action_results": task_action_results})), run_id, project_id))
                await connection.commit()

        watermark_ts = datetime.now(timezone.utc).isoformat()
        delta = list(snapshot.get("interaction_delta") or [])
        last_message_id = str(delta[-1]["message_id"]) if delta else None
        recommended_sha256 = hashlib.sha256(
            recommended_prompt.encode("utf-8")
        ).hexdigest() if recommended_prompt else None

        dialogue = list(supervisor_now.get("dialogue") or [])
        for item in delta[-3:]:
            if str(item.get("direction") or "") == "inbound":
                dialogue.append({
                    "role": "user",
                    "ts": str(item.get("created_at") or watermark_ts),
                    "message_id": str(item.get("message_id") or ""),
                    "interface_role": item.get("interface_role"),
                    "text": str(item.get("text") or "")[:400],
                })
        dialogue.append({
            "role": "supervisor",
            "ts": watermark_ts,
            "text": str(
                decision.get("user_message")
                or next(iter(decision.get("reason") or []), "")
            )[:400],
        })
        hypotheses = list(decision.get("intent_hypothesis") or [])
        card_revision = int(supervisor_now.get("card_revision") or 0) + 1
        raw_interaction_head = request.get("interaction_head")
        interaction_head = (
            dict(raw_interaction_head)
            if isinstance(raw_interaction_head, dict)
            else {"source_message_id": str(raw_interaction_head or "")}
        )
        self._update_supervisor_current(project_id, task_id, {
            "schema_version": "supervisor-current.v2",
            "status": "historical_routing_receipt" if task_action_results else "ready",
            "evaluation_status": "evaluated",
            "task_id": task_id,
            "watermark": {"ts": watermark_ts, "message_id": last_message_id},
            "dialogue": dialogue[-6:],
            "open_question": (
                {
                    **dict((decision.get("clarifications") or [])[0]),
                    "status": "open",
                    "decision_id": decision_id,
                }
                if decision.get("clarifications") else None
            ),
            "last_decision": {
                "ts": watermark_ts,
                "decision": decision.get("decision"),
                "reason": decision.get("reason"),
                "intent_hypothesis": hypotheses,
                "insufficient_evidence": decision.get("insufficient_evidence"),
                "next_prompt": decision.get("next_prompt"),
                "decision_ref": decision_ref,
                "decision_id": decision_id,
            },
            "last_handoff_ref": decision.get("handoff_ref"),
            "last_agent_outcome": (
                (request.get("agent_turn_report") or {}).get("outcome")
            ),
            "card_revision": card_revision,
            "context_epoch": source_context_epoch,
            "interaction_head": interaction_head,
            "project_assessment": decision.get("project_assessment"),
            "user_intent_context": decision.get("user_intent_context"),
            "brief_ref": decision.get("brief_ref"),
        })

        if task_action_results:
            receipts = []
            for item in task_action_results:
                activated = item.get("result") or {}
                receipts.append({"kind": item["kind"], "status": item.get("status") or activated.get("outcome"),
                    "ref": item.get("ref"), "active_task_id": (activated.get("task") or {}).get("run_id")})
            try:
                await self.repository.create_outbound(
                    project_id=project_id, session_id=None, run_id=run_id,
                    channel=str(request.get("channel") or "discord"), chat_id=str(request.get("chat_id") or ""),
                    user_id=request.get("user_id"), message_type="kernel_notice",
                    content={"text": "任务路由结果已存档：\n" + "\n".join(
                        f"{item['kind']}: {item.get('status')}" + (f" · Task {item['active_task_id']}" if item.get("active_task_id") else "")
                        for item in receipts), "origin": "kernel", "context_visibility": "ui_only",
                        "interaction_kind": "task_action_receipt", "receipts": receipts, "decision_ref": decision_ref},
                    reply_to_message_id=run.get("trigger_message_id"),
                    idempotency_key=f"supervisor-task-action-receipt:{run_id}:{attempt}",
                )
            except Exception:
                logging.exception("Task routing committed; receipt UI remains pending without replay")
            return {"outcome": "evaluated", "decision": decision.get("decision"), "decision_ref": decision_ref["uri"],
                    "action_plan_ref": action_ref, "confirmation_class": confirmation_class,
                    "task_action_results": task_action_results}

        card = render_status_card(
            task_id=task_id, trigger=trigger,
            turn_number=int(request.get("turn_number") or 0), decision=decision,
        )
        try:
            await self._queue_status(
                run, request, card,
                str(decision.get("decision") or ""), recommended_sha256,
                card_revision=card_revision,
                decision_id=decision_id,
                decision_ref=decision_ref["uri"],
                action_plan_ref=action_ref,
                recommended_prompt=recommended_prompt,
                confirmation_class=confirmation_class,
            )
        except Exception as exc:
            raise SupervisorPipelineError("delivery", exc) from exc
        return {
            "outcome": "evaluated",
            "decision": decision.get("decision"),
            "insufficient_evidence": decision.get("insufficient_evidence"),
            "hypotheses": len(hypotheses),
            "decision_ref": decision_ref["uri"],
            "decision_id": decision_id,
            "action_plan_ref": action_ref,
            "confirmation_class": confirmation_class,
            "usage": decision.get("usage"),
            "task_action_results": task_action_results,
        }

    async def _finish(
        self, run_id: str, worker_instance_id: str, claim_token: str,
        result: dict[str, Any], *, artifact_refs: list[str] | None = None,
    ) -> dict[str, Any]:
        finished = await self.repository.finish_run(
            run_id, worker_instance_id, claim_token, status="succeeded",
            result=result, artifact_refs=artifact_refs,
        )
        if not finished:
            raise RuntimeError("supervisor consultation lease was lost before finishing")
        return result
