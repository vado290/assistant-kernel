"""Deterministic processor for Supervisor consultation Runs.

Owns the hard budget gate, the snapshot → panels
→ handoff → two-pass evaluation pipeline, decision persistence, and the
status card. Never triggers another consultation (recursion guard) and never
writes task artifacts — the Supervisor stays purely advisory.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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

    async def process(
        self, run: dict[str, Any], worker_instance_id: str,
        cancel_event: Any = None,
    ) -> dict[str, Any]:
        request = dict(run.get("request_json") or {})
        if request.get("interaction_kind") != "supervisor_consultation":
            raise ValueError("SupervisorProcessor requires interaction_kind supervisor_consultation")
        project_id = str(run["project_id"])
        task_id = str(request.get("task_id") or "")
        if not task_id:
            raise ValueError("supervisor consultation requires request.task_id")
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
        config = self.supervisor.configuration(project_id)
        if not config["enabled"]:
            resume_degraded = getattr(self.repository, "resume_supervision_degraded", None)
            if callable(resume_degraded):
                await resume_degraded(
                    task_id=task_id,
                    turn_number=int(request.get("turn_number") or 0),
                    reason="Supervisor disabled",
                )
            return await self._finish(
                run_id, worker_instance_id, claim_token,
                {"outcome": "skipped_disabled", "summary": "supervisor disabled"},
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
            await self._queue_status(
                run, request,
                f"⏸️ Supervisor预算门禁已触发：{budget_reason}。本轮未调用模型。",
                "skipped_due_to_budget", None,
            )
            resume_degraded = getattr(self.repository, "resume_supervision_degraded", None)
            if callable(resume_degraded):
                await resume_degraded(
                    task_id=task_id, turn_number=turn_number,
                    reason=f"Supervisor budget gate: {budget_reason}",
                )
            return await self._finish(
                run_id, worker_instance_id, claim_token,
                {"outcome": "skipped_due_to_budget", "reason": budget_reason},
            )

        trigger_message = (
            await self.repository.get_message(str(run["trigger_message_id"]))
            if run.get("trigger_message_id") else None
        )
        try:
            snapshot = await self.supervisor.build_snapshot(
                project_id=project_id, task_id=task_id, trigger=trigger,
                current_message=trigger_message,
                agent_turn_report=request.get("agent_turn_report"),
                consultation_context=request,
            )
        except Exception as exc:
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
        if has_new_user_text:
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
            resume_degraded = getattr(
                self.repository, "resume_supervision_degraded", None
            )
            resumed = None
            if callable(resume_degraded):
                resumed = await resume_degraded(
                    task_id=task_id,
                    turn_number=int(request.get("turn_number") or 0),
                    reason="Supervisor returned an invalid decision contract",
                )
            try:
                await self._queue_status(
                    run, request,
                    (
                        "Supervisor本轮输出合同无效，未采用任何Action Plan。"
                        "PM Task保持原状态；若原本在等待Supervisor，已按当前Work Mode受限降级继续。"
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
                    "task_resumed": resumed is not None,
                    "invalid_decision": exc.invalid_decision,
                    "trace_uri": exc.trace_uri,
                    "trace_sha256": exc.trace_sha256,
                },
                artifact_refs=[exc.trace_uri] if exc.trace_uri else [],
            )
        except Exception as exc:
            raise SupervisorPipelineError("main_evaluation", exc) from exc
        if not await self.repository.run_claim_is_active(
            run_id, worker_instance_id, claim_token
        ):
            raise RuntimeError(
                "Supervisor consultation lease was lost before persistence"
            )
        try:
            decision_ref = self._persist_decision(
                project_id, task_id, decision,
                supervisor_run_id=run_id, attempt=attempt,
            )
        except Exception as exc:
            raise SupervisorPipelineError("persistence", exc) from exc
        decision_id = "supdec-" + decision_ref["sha256"][:16]

        source_context_epoch = int(request.get("source_context_epoch") or 0)
        if not await self.repository.supervisor_context_is_current(
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

        recommended_prompt = str(
            (decision.get("next_prompt") or {}).get("recommended_user_prompt")
            or (decision.get("next_prompt") or {}).get("assembled_prompt")
            or ""
        )
        confirmation_class = (
            "safe"
            if (
                trigger == "after_agent_turn"
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
                },
                artifact_refs=[decision_ref["uri"], action_ref["uri"]],
                session_action=str(action_payload["session_action"]),
            )
        else:
            await self._finish(
                run_id, worker_instance_id, claim_token,
                {"outcome": "evaluated", "decision_ref": decision_ref["uri"]},
                artifact_refs=[decision_ref["uri"], action_ref["uri"]],
            )
            commit_result = {"state_revision": 1}
        self.task_documents.update_current(
            project_id=project_id, task_id=task_id,
            document_kind="supervisor-action-plan", ref=action_ref,
            commit_id=f"supervisor-action:{run_id}",
            state_revision=int(commit_result["state_revision"]),
        )

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
            "status": "ready",
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
        })

        if hypotheses:
            try:
                self.supervisor.memory.append(project_id, {
                    "task_id": task_id,
                    "status": "proposed",
                    "created_by": "supervisor",
                    "user_text_excerpt": next(
                        (str(item.get("text") or "") for item in reversed(delta)
                         if item.get("direction") == "inbound"),
                        "",
                    ),
                    "source_message_ids": [
                        str(item["message_id"]) for item in delta[-5:]
                    ],
                    "intent_statement": str(hypotheses[0].get("statement") or ""),
                    "tags": [
                        str(item.get("statement") or "").split()[:3][0]
                        for item in hypotheses if item.get("statement")
                    ][:3],
                    "decision_ref": decision_ref["uri"],
                })
            except ValueError:
                logging.exception("supervisor memory append rejected for %s", task_id)

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
