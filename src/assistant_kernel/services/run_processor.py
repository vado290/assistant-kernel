from __future__ import annotations
from assistant_kernel.services.scheduling_scope import SCHEDULING_NOTICE

import asyncio
import difflib
import hashlib
import json
import logging
import os
import re
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from assistant_kernel.config import KernelSettings, SharedVolcengineConfig
from assistant_kernel.harness import AgentTurnRequest, HarnessAdapter
from assistant_kernel.repositories import CoreRepository
from assistant_kernel.repositories.core import json_compatible
from assistant_kernel.services.agent_files import AgentFileLoader, FileAgentConfig
from assistant_kernel.services.audit import (
    AUDIT_SCHEMA_VERSION,
    is_audit_schema,
)
from assistant_kernel.services.context_budget import ContextBudgetManager
from assistant_kernel.services.candidate_bundle import CandidateBundleService
from assistant_kernel.services.execution_plan import ProjectResourceResolver
from assistant_kernel.services.file_effects import FileEffectRecorder
from assistant_kernel.services.document_runtime import (
    TaskDocumentService,
    resource_ref,
)
from assistant_kernel.services.goal_supervisor import GoalSupervisorService
from assistant_kernel.services.questionnaire import is_questionnaire
from assistant_kernel.services.project_credentials import ProjectCredentialsStore
from assistant_kernel.services.task_lifecycle import task_lifecycle_view
from assistant_kernel.services.runtime_catalog import ConversationDigestService
from assistant_kernel.services.validation_runner import ValidationRunner
from assistant_kernel.storage import ProjectRuntimeManager


EXECUTOR_NAME = re.compile(r"^[A-Za-z0-9._:/-]{1,256}$")
PM_TOOL_EVIDENCE_SCHEMA_VERSION = "pm-tool-evidence.v1"
PM_TOOL_EVIDENCE_LIMIT = 20


def message_context_visibility(message: dict[str, Any]) -> str:
    """Classify persisted UI/control messages before building semantic history."""
    content = dict(message.get("content_json") or {})
    explicit = str(content.get("context_visibility") or "").strip().lower()
    if explicit in {"ui_only", "structured", "semantic"}:
        return explicit
    if explicit == "audit_only":
        return "structured"
    if str(message.get("message_type") or "") in {
        "command", "reconciliation", "kernel_notice",
    }:
        return "ui_only"
    if (
        message.get("run_id") is None
        and str(message.get("direction") or "") == "outbound"
        and content.get("command")
    ):
        return "ui_only"
    return "semantic"


def prompt_message_content(message: dict[str, Any]) -> dict[str, Any]:
    """Return semantic message content without channel or delivery metadata."""
    content = dict(message.get("content_json") or {})
    message_type = str(message.get("message_type") or "")

    if is_questionnaire(content):
        return {"questions": list(content.get("questions") or []), "summary": content.get("summary", "")}
    if content.get("schema_version") == "questionnaire-answer.v1":
        return {
            key: content[key] for key in ("questions", "answers", "choice_ids", "additional_text")
            if key in content
        }

    if message_type == "question" and content.get("question_text"):
        semantic = {
            "question_id": str(content.get("question_id") or ""),
            "question_text": str(content["question_text"]),
        }
        if "required" in content:
            semantic["required"] = bool(content["required"])
        if content.get("options"):
            semantic["options"] = list(content["options"])
        return semantic

    if content.get("schema_version") == "choice-answer.v1":
        if message_type == "answer":
            return {
                "question_id": str(content.get("question_id") or ""),
                "choice_id": str(content.get("choice_id") or ""),
                "text": str(content.get("text") or ""),
            }
        return {}

    semantic = {
        key: value
        for key, value in content.items()
        if key not in {
            "raw", "delivery", "correlation", "interaction_seq", "ordinal",
            "discord", "feishu", "author_context", "origin", "context_visibility",
        }
    }
    if message_type == "approval" and semantic.get("approval_question"):
        semantic.pop("text", None)
    return semantic


def _has_successful_mcp_tool_call(events: list[dict], tool_name: str) -> bool:
    """Accept a recovered handshake when any completed call succeeded."""
    for event in events:
        if event.get("type") != "item.completed":
            continue
        item = event.get("item") or {}
        if (
            item.get("type") == "mcp_tool_call"
            and item.get("tool") == tool_name
            and item.get("status") == "completed"
            and not item.get("error")
        ):
            return True
    return False


def _pm_tool_error_type(error: Any) -> str | None:
    if not error:
        return None
    if isinstance(error, dict):
        for key in ("error_type", "type", "code"):
            value = str(error.get(key) or "").strip()
            if value:
                return value[:200]
        return "ToolError"
    return type(error).__name__ if not isinstance(error, str) else "ToolError"


def _bounded_pm_artifact_refs(
    artifact_refs: list[Any] | None,
) -> list[dict[str, Any]]:
    """Keep only the bounded, non-content Artifact evidence fields."""
    bounded: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for raw in artifact_refs or []:
        item = dict(raw) if isinstance(raw, dict) else {"uri": raw}
        uri = str(item.get("uri") or "").strip()
        if not uri:
            continue
        sha256 = str(item.get("sha256") or "").strip()
        identity = (uri, sha256)
        if identity in seen:
            continue
        seen.add(identity)
        bounded.append({
            "uri": uri,
            "sha256": sha256 or None,
            "kind": str(item.get("kind") or "").strip() or None,
            "producer": str(item.get("producer") or "pm").strip() or "pm",
        })
        if len(bounded) >= PM_TOOL_EVIDENCE_LIMIT:
            break
    return bounded


def summarize_pm_tool_evidence(
    *, invocation_id: str, events: list[dict[str, Any]],
    trace_uri: str | None, trace_sha256: str | None,
    usage: dict[str, Any], artifact_refs: list[Any] | None = None,
) -> dict[str, Any]:
    """Build a deterministic, content-free index over a PM invocation trace."""
    calls: dict[str, dict[str, Any]] = {}
    for index, event in enumerate(events):
        item = event.get("item") or {}
        if not isinstance(item, dict) or item.get("type") != "mcp_tool_call":
            continue
        call_id = str(item.get("id") or item.get("call_id") or f"event-{index}")
        call = calls.setdefault(call_id, {
            "call_id": call_id,
            "server": "",
            "tool": "",
            "status": "interrupted",
            "error_type": None,
        })
        call["server"] = str(item.get("server") or call["server"] or "")
        call["tool"] = str(item.get("tool") or call["tool"] or "")
        error = item.get("error")
        status = str(item.get("status") or "").strip().lower()
        event_type = str(event.get("type") or "").strip().lower()
        if error or status in {"failed", "error"} or event_type == "item.failed":
            call["status"] = "failed"
            call["error_type"] = _pm_tool_error_type(error) or "ToolError"
        elif (
            status in {"interrupted", "cancelled", "canceled"}
            or event_type == "item.interrupted"
        ):
            call["status"] = "interrupted"
        elif event_type == "item.completed" or status == "completed":
            call["status"] = "completed"
            call["error_type"] = None

    all_calls = list(calls.values())
    visible_calls = all_calls[:PM_TOOL_EVIDENCE_LIMIT]
    usage_fields = {
        key: int(usage.get(key) or 0)
        for key in (
            "input_tokens", "cached_input_tokens", "output_tokens",
            "reasoning_output_tokens",
        )
    }
    return {
        "schema_version": PM_TOOL_EVIDENCE_SCHEMA_VERSION,
        "invocation": {
            "invocation_id": str(invocation_id),
            "trace_ref": {
                "uri": str(trace_uri) if trace_uri else None,
                "sha256": str(trace_sha256) if trace_sha256 else None,
            },
            "usage": usage_fields,
        },
        "tool_summary": {
            "total": len(all_calls),
            "completed": sum(call["status"] == "completed" for call in all_calls),
            "failed": sum(call["status"] == "failed" for call in all_calls),
            "interrupted": sum(call["status"] == "interrupted" for call in all_calls),
            "truncated": len(all_calls) > PM_TOOL_EVIDENCE_LIMIT,
        },
        "tool_calls": visible_calls,
        "artifact_refs": _bounded_pm_artifact_refs(artifact_refs),
    }


def summarize_native_delegations(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Keep a bounded index over Codex native multi-agent coordination events."""
    items: list[dict[str, Any]] = []
    for event in events:
        raw = event.get("item") or {}
        if not isinstance(raw, dict):
            continue
        item_type = str(raw.get("type") or "")
        tool = str(raw.get("tool") or raw.get("name") or "")
        marker = f"{item_type} {tool}".lower()
        if not any(token in marker for token in (
            "subagent", "spawn_agent", "wait_for_subagents", "multi_agent",
            "send_subagent", "interrupt_agent", "collab_tool_call",
        )):
            continue
        items.append({
            "event_type": str(event.get("type") or ""),
            "item_type": item_type,
            "call_id": str(raw.get("id") or raw.get("call_id") or "") or None,
            "agent_id": str(
                raw.get("agent_id") or raw.get("subagent_id") or ""
            ) or None,
            "receiver_thread_ids": [
                str(item) for item in raw.get("receiver_thread_ids") or []
            ][:3],
            "status": str(raw.get("status") or "") or None,
        })
        if len(items) >= 30:
            break
    return {
        "schema_version": "native-delegation-evidence.v1",
        "events": items,
        "event_count": len(items),
        "truncated": len(items) >= 30,
    }


def parse_agent_envelope(text: str, *, require_object: bool = False, phase: str | None = None,
                         expected_revision: str | None = None) -> dict[str, Any]:
    stripped = text.strip()
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        if require_object or expected_revision is not None:
            raise ValueError("Stage Run must return exactly one JSON result object")
        return {"outcome": "completed", "summary": stripped, "artifacts": []}
    if not isinstance(value, dict):
        if require_object or expected_revision is not None:
            raise ValueError("Stage Run must return a JSON object")
        return {"outcome": "completed", "summary": stripped, "artifacts": []}
    if value.get("schema_version") == "agent-result.v1":
        from assistant_kernel.services.agent_results import normalize_agent_result, pm_internal_result
        return pm_internal_result(normalize_agent_result(
            value, phase=phase or str((value.get("payload") or {}).get("stage") or "requirements"),
            expected_revision=expected_revision,
        ))
    if expected_revision is not None:
        raise ValueError('The accepted package does not permit a legacy result decoder')
    outcome = str(value.get("outcome") or "completed")
    if outcome not in {
        "completed", "continue", "ready_for_validation", "ready_for_release",
        "needs_input", "needs_approval", "return_to_requirements", "return_to_design",
        "request_changes", "blocked", "failed"
    }:
        raise ValueError(f"unsupported agent outcome: {outcome}")
    value["outcome"] = outcome
    value.setdefault("summary", "")
    value.setdefault("questions", [])
    value.setdefault("artifacts", [])
    return value


def normalize_work_order(raw: Any) -> dict[str, Any]:
    """Validate one bounded Project-Agent work package before user approval."""
    if not isinstance(raw, dict) or raw.get("schema_version") != "work-order.v1":
        raise ValueError("Project plan approval requires work_order.v1")
    package_id = str(raw.get("package_id") or "").strip()
    goal = str(raw.get("goal") or "").strip()
    target_uris = [str(item).strip() for item in raw.get("target_uris") or []]
    acceptance = [str(item).strip() for item in raw.get("acceptance_criteria") or []]
    non_goals = [str(item).strip() for item in raw.get("non_goals") or []]
    requested_skills = [str(item).strip() for item in raw.get("requested_skills") or []]
    risk_class = str(raw.get("risk_class") or "medium").strip().lower()
    if not package_id or len(package_id) > 128 or not goal:
        raise ValueError("work order requires a bounded package_id and goal")
    if not 1 <= len(target_uris) <= 6 or any(not item for item in target_uris):
        raise ValueError("one work order must bind between one and six target URIs")
    if len(set(target_uris)) != len(target_uris):
        raise ValueError("work order target URIs must be unique")
    if not 1 <= len(acceptance) <= 12 or any(not item for item in acceptance):
        raise ValueError("work order requires between one and twelve acceptance criteria")
    if risk_class not in {"low", "medium", "high"}:
        raise ValueError("work order risk_class must be low, medium, or high")
    return {
        **raw,
        "schema_version": "work-order.v1",
        "package_id": package_id,
        "goal": goal,
        "target_uris": target_uris,
        "acceptance_criteria": acceptance,
        "non_goals": [item for item in non_goals if item],
        "requested_skills": list(dict.fromkeys(
            item for item in requested_skills if item
        )),
        "risk_class": risk_class,
    }


def normalize_permission_request(
    raw: Any, *, project_id: str, run_id: str, agent_key: str, stage: str | None,
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("permission_request must be an object")
    if raw.get("schema_version") != "permission-request.v1":
        raise ValueError("permission_request must use permission-request.v1")
    action = str(raw.get("action") or "")
    # Keep "network" parseable for old Agent outputs; it no longer requests a grant.
    allowed = {
        "workspace", "discover", "read", "candidate_write", "candidate_execute",
        "publish_once", "execute_once", "network", "credential_use",
        "account_connect", "trade",
    }
    if action not in allowed:
        raise ValueError("permission_request action is unsupported")
    reason = str(raw.get("reason") or "").strip()
    resources = [dict(item) for item in (raw.get("resources") or []) if isinstance(item, dict)]
    if not reason or not resources:
        raise ValueError("permission_request requires a reason and resources")
    return {
        **raw,
        "schema_version": "permission-request.v1",
        "action": action,
        "reason": reason,
        "resources": resources,
        "project_id": project_id,
        "source_run_id": run_id,
        "requested_by_agent": agent_key,
        "source_stage": stage,
    }


def normalize_question_options(raw: Any) -> list[dict[str, str]] | None:
    """Validate optional finite choices without changing free-text questions."""
    if raw is None:
        return None
    if not isinstance(raw, list) or not 2 <= len(raw) <= 3:
        raise ValueError("question options must contain two or three choices")
    options: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("each question option must be an object")
        option_id = str(item.get("id") or "").strip()
        label = str(item.get("label") or "").strip()
        description = str(item.get("description") or "").strip()
        if (
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", option_id)
            or option_id in seen or not 1 <= len(label) <= 60
            or len(description) > 300
        ):
            raise ValueError("question option ID, label or description is invalid")
        seen.add(option_id)
        options.append({"id": option_id, "label": label, "description": description})
    return options


class RunProcessor:
    def __init__(
        self, settings: KernelSettings, repository: CoreRepository, harness: HarnessAdapter
    ):
        self.settings = settings
        self.repository = repository
        self.harness = harness
        self.runtime = ProjectRuntimeManager(settings.data_root)
        self.agents = AgentFileLoader(settings)
        self.validation = ValidationRunner(settings)
        self.plans = ProjectResourceResolver(settings)
        self.credentials = ProjectCredentialsStore(settings.data_root)
        self.candidate_bundles = CandidateBundleService(settings)
        self.task_documents = TaskDocumentService(settings)
        self.goal_supervisor = GoalSupervisorService(settings, repository)
        self.conversation_digests = ConversationDigestService(settings)

    def _write_turn_documents(
        self, *, run: dict[str, Any], project_id: str, trigger: dict[str, Any],
        turn_number: int, envelope: dict[str, Any], usage_snapshot: dict[str, Any],
        artifacts: list[Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any], int]:
        """Write the immutable Task contract and Turn record.

        The returned refs are accepted by the parent Task Run transaction.  No
        current pointer moves until that DB acceptance succeeds.
        """
        request = dict(run.get("request_json") or {})
        accepted = dict(request.get("accepted_documents") or {})
        state_revision = int(request.get("state_revision") or 0) + 1
        source_message_id = str(trigger.get("message_id") or "")
        source_refs = (
            [resource_ref(kind="message", identity=source_message_id, purpose="turn_input")]
            if source_message_id else []
        )
        source_refs.extend(resource_ref(kind="message", identity=str(mid), purpose="turn_supplement") for mid in (run.get("executor_json") or {}).get("turn_input_message_ids") or [])
        contract_ref = accepted.get("task_contract")
        if contract_ref:
            self.task_documents.read(project_id, contract_ref)
        else:
            contract_ref = self.task_documents.write_revision(
                project_id=project_id, task_id=str(run["run_id"]),
                document_kind="task-contract", document_schema="task-contract.v1",
                payload={
                    "goal": str(request.get("task_goal") or request.get("goal") or "").strip(),
                    "scope": {"included": [], "excluded": []},
                    "requirements": [], "acceptance_criteria": [],
                    "constraints": list(
                        (envelope.get("task_checkpoint") or {}).get("constraints") or []
                    ),
                    "source_refs": source_refs,
                },
                status="draft", actor_type="kernel", actor_id="kernel",
                run_id=str(run["run_id"]), reason="bootstrap Task contract",
                source_refs=source_refs,
            )
        checkpoint = dict(envelope.get("task_checkpoint") or {})
        turn_ref = self.task_documents.write_revision(
            project_id=project_id, task_id=str(run["run_id"]),
            document_kind="turn-record", document_schema="turn-record.v1",
            payload={
                "turn_number": turn_number,
                "input_refs": source_refs,
                "action_plan_ref": accepted.get("current_action_plan"),
                "work_performed": str(envelope.get("summary") or ""),
                "claims": list(checkpoint.get("completed") or []),
                "questions": list(envelope.get("questions") or []),
                "tool_evidence": dict(usage_snapshot.get("pm_evidence") or {}),
                "changed_files": [],
                "external_effect_refs": list(checkpoint.get("external_effects") or []),
                "artifact_refs": _bounded_pm_artifact_refs(artifacts),
                "delegation_refs": list(envelope.get("delegations") or []),
                "native_delegation_evidence": dict(
                    usage_snapshot.get("native_delegations") or {}
                ),
                "outcome": str(envelope.get("outcome") or ""),
                "next_action": str(checkpoint.get("next_action") or ""),
                "previous_turn_ref": accepted.get("last_turn"),
            },
            status="settled", actor_type="pm",
            actor_id=str(run.get("agent_key") or f"project:{project_id}:manager"),
            run_id=str(run["run_id"]), reason="PM Turn settled",
            source_refs=source_refs,
            supersedes_ref=accepted.get("last_turn"),
        )
        next_accepted = {
            **accepted,
            "task_contract": contract_ref,
            "last_turn": turn_ref,
        }
        return next_accepted, turn_ref, state_revision

    def _write_task_contract_revision(
        self, *, run: dict[str, Any], project_id: str, document_markdown: str,
        status: str, prior_ref: dict[str, Any] | None, structured: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        request = dict(run.get("request_json") or {})
        return self.task_documents.write_revision(
            project_id=project_id, task_id=str(run["run_id"]),
            document_kind="task-contract", document_schema="task-contract.v1",
            payload={
                "goal": str(request.get("task_goal") or request.get("goal") or "").strip(),
                "scope": (structured or {}).get("scope") or {"included": [], "excluded": []},
                "requirements": (structured or {}).get("requirements") or [document_markdown],
                "acceptance_criteria": (structured or {}).get("acceptance_criteria") or [],
                "constraints": (structured or {}).get("constraints") or [],
                "document_markdown": document_markdown,
                "source_refs": [],
            },
            status=status, actor_type="pm",
            actor_id=str(run.get("agent_key") or f"project:{project_id}:manager"),
            run_id=str(run["run_id"]), reason="Task contract requirements revision",
            supersedes_ref=prior_ref,
        )

    def _write_solution_design_revision(self, *, run, project_id, envelope, prior_ref=None):
        request = dict(run.get("request_json") or {})
        contract_ref = dict((request.get("accepted_documents") or {}).get("task_contract") or {})
        if not request.get("requirements_approval") or not contract_ref:
            raise ValueError("Design requires approved current requirements")
        payload = dict(envelope.get("payload") or {})
        policy = str(payload.get("release_policy") or "manual")
        if policy not in {"manual", "automatic_low_risk"}:
            raise ValueError("Design release_policy is invalid")
        risk = str(payload.get("risk_class") or "medium")
        if risk not in {"low", "medium", "high"}:
            raise ValueError("Design risk_class is invalid")
        markdown = str(envelope.get("document_markdown") or "").strip()
        if not markdown:
            raise ValueError("Design approval requires a complete document")
        actions = list(payload.get("runbook_actions") or [])
        if any(not isinstance(action, dict) or not action.get("action_id") for action in actions):
            raise ValueError("Runbook actions require stable action IDs")
        payload.update({"task_contract_ref": contract_ref, "task_contract_sha256": contract_ref["sha256"],
                        "document_markdown": markdown, "release_policy": policy, "risk_class": risk,
                        "runbook_actions": actions})
        # The user reviews the executable policy as well as narrative design.
        markdown += "\n\n## 发布与运营授权\n" + json.dumps({
            "release_policy": policy, "risk_class": risk,
            "publish_targets": payload.get("publish_targets") or [],
            "runbook_actions": actions, "report_schedule": payload.get("report_schedule"),
        }, ensure_ascii=False, indent=2)
        payload["document_markdown"] = markdown
        envelope["document_markdown"] = markdown
        envelope.setdefault("approval_question", "是否批准这份具体Design并进入实施？")
        return self.task_documents.write_revision(
            project_id=project_id, task_id=str(run["run_id"]), document_kind="solution-design",
            document_schema="solution-design.v1", payload=payload, status="ready",
            actor_type="pm", actor_id=str(run["agent_key"]), run_id=str(run["run_id"]),
            reason="implementation design for the approved requirements", supersedes_ref=prior_ref,
            source_refs=[contract_ref],
        )

    def _write_phase_report(self, *, run, project_id, phase, envelope, source_refs):
        names = {"build": "implementation-report", "audit": "review-report", "operate": "operation-report"}
        if phase not in names:
            return None
        payload = dict(envelope.get("payload") or {})
        markdown = str(payload.get("report_markdown") or envelope.get("report_markdown") or envelope.get("summary") or "")
        return self.task_documents.write_revision(
            project_id=project_id, task_id=str(run["run_id"]), document_kind=names[phase],
            document_schema=names[phase] + ".v1",
            payload={**payload, "report_markdown": markdown, "outcome": envelope["outcome"],
                     "evidence_refs": list(envelope.get("evidence_refs") or []),
                     "checkpoint": dict(envelope.get("task_checkpoint") or {})},
            status="submitted", actor_type="pm", actor_id=str(run["agent_key"]),
            run_id=str(run["run_id"]), reason="phase result; actual validation and effects remain separate receipts",
            source_refs=source_refs,
        )

    def _automatic_low_risk_release(self, run, bundle=None):
        request = dict(run.get("request_json") or {})
        if str(run["project_id"]) in {"kernel", "butler-maintenance"} or request.get("capability_change") or request.get("review_requires_final_approval"):
            return False
        ref = dict((request.get("accepted_documents") or {}).get("solution_design") or {})
        approval = dict(request.get("design_approval") or {})
        if not ref or approval.get("approved_document_sha256") != ref.get("sha256"):
            return False
        design = self.task_documents.read(str(run["project_id"]), ref)["payload"]
        if design.get("release_policy") != "automatic_low_risk" or design.get("risk_class") != "low":
            return False
        targets = {value["target_uri"] if isinstance(value,dict) else str(value) for value in design.get("publish_targets") or []}
        if not targets:
            return False
        if bundle is None:
            bundle = self.candidate_bundles.seal(str(run["project_id"]), str(run["run_id"]))
            expected = str((request.get("sealed_candidate_bundle") or {}).get("bundle_sha256") or "")
            if bundle.get("bundle_sha256") != expected:
                raise ValueError("Candidate changed after Validation or during Review")
        if bundle:
            for file in bundle.get("files") or []:
                uri = str(file.get("target_uri") or "")
                if uri not in targets or any(part in uri for part in ("/agents/", "/.agents/", "/capabilities/", "/mcp/", "project.yaml")):
                    return False
            from assistant_kernel.services.agent_results import publication_requires_final_approval
            roots = self.plans.workspace_scopes.resolve(str(run["project_id"]), str(run["run_id"]), "implementation")
            if publication_requires_final_approval(self.settings, str(run["project_id"]), bundle.get("files") or [], roots):
                return False
        return True

    def _release_approval_markdown(self, run, review_markdown):
        """Show the reviewed bytes and evidence at the final approval gate."""
        project_id = str(run["project_id"])
        request = dict(run.get("request_json") or {})
        ref = dict(request.get("sealed_candidate_bundle") or {})
        prefix = f"vault://{project_id}/"
        if not str(ref.get("uri") or "").startswith(prefix):
            raise ValueError("release requires the reviewed candidate bundle")
        raw = self.runtime.resolve_vault_file(project_id, ref["uri"][len(prefix):]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != ref.get("sha256"):
            raise ValueError("reviewed bundle file changed")
        bundle = json.loads(raw)
        roots = self.plans.workspace_scopes.resolve(project_id, str(run["run_id"]), "implementation")
        lines = ["# 发布批准", "", f"- Task: {run['run_id']}", f"- Bundle SHA: {bundle['bundle_sha256']}",
                 f"- Validation receipt: {json.dumps(request.get('validation_artifact'), ensure_ascii=False, default=str)}",
                 f"- Review report: {json.dumps((request.get('accepted_documents') or {}).get('review_report'), ensure_ascii=False)}",
                 "", "批准仅应用这份密封候选，不另授权交易、外部动作或服务重启。", "", str(review_markdown or ""), "", "## 最终文件差异", ""]
        for item in bundle.get("files") or []:
            uri = item["target_uri"]
            _, target, _ = self.plans.workspace_scopes.resolve_uri(roots, uri)
            old = target.read_bytes() if target.is_file() else b""
            actual = hashlib.sha256(old).hexdigest() if target.is_file() else None
            if actual != item.get("original_sha256"):
                raise ValueError("formal baseline changed before final release approval")
            candidate_uri = item.get("artifact_uri")
            new = self.runtime.resolve_vault_file(project_id, candidate_uri[len(prefix):]).read_bytes() if candidate_uri else b""
            if candidate_uri and hashlib.sha256(new).hexdigest() != item.get("candidate_sha256"):
                raise ValueError("reviewed candidate bytes changed")
            lines.extend([f"### {item['operation']} · {uri}", f"旧 SHA: {actual}; 新 SHA: {item.get('candidate_sha256')}", ""])
            sensitive = target.name in {".env", "auth.json", "credentials.json"} or target.suffix in {".pem", ".key"}
            try:
                if sensitive or b"\x00" in old or b"\x00" in new:
                    raise UnicodeError()
                patch = "".join(difflib.unified_diff(old.decode("utf-8").splitlines(True), new.decode("utf-8").splitlines(True),
                                                    fromfile="a/" + uri, tofile="b/" + uri))
                lines.extend(["~~~diff", patch, "~~~", ""])
            except UnicodeError:
                lines.append(f"二进制或敏感内容请依据固定 SHA 在受限 Vault 审阅：{candidate_uri}")
        return "\n".join(lines)

    def _settle_native_delegations(
        self, *, run: dict[str, Any], project_id: str,
        envelope: dict[str, Any], usage_snapshot: dict[str, Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Accept structured native-subagent Results into the parent Task.

        Native transcripts remain in the invocation trace.  Only an explicit
        Result carrying the stable delegation_id can retire an active request.
        """
        request = dict(run.get("request_json") or {})
        accepted = dict(request.get("accepted_documents") or {})
        active_refs = list(accepted.get("active_delegations") or [])
        if not active_refs:
            if envelope.get("delegation_results"):
                raise ValueError("PM returned delegation Results without active requests")
            return accepted, []
        native_requests: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        for ref in active_refs:
            if not isinstance(ref, dict):
                continue
            document = self.task_documents.read(project_id, ref)
            payload = dict(document.get("payload") or {})
            if payload.get("executor") != "native_subagent":
                continue
            delegation_id = str(payload.get("delegation_id") or "")
            if not delegation_id:
                raise ValueError("native Delegation Request has no delegation_id")
            native_requests[delegation_id] = (ref, payload)
        raw_results = envelope.get("delegation_results") or []
        if not isinstance(raw_results, list):
            raise ValueError("delegation_results must be a list")
        results_by_id: dict[str, dict[str, Any]] = {}
        for raw in raw_results:
            if not isinstance(raw, dict):
                raise ValueError("each native Delegation Result must be an object")
            delegation_id = str(raw.get("delegation_id") or "")
            if delegation_id not in native_requests or delegation_id in results_by_id:
                raise ValueError("native Delegation Result is unknown or duplicated")
            if str(raw.get("status") or "") not in {"completed", "blocked", "failed"}:
                raise ValueError("native Delegation Result status is unsupported")
            if not str(raw.get("summary") or "").strip():
                raise ValueError("native Delegation Result requires summary")
            results_by_id[delegation_id] = raw
        missing_required = [
            delegation_id for delegation_id, (_, payload) in native_requests.items()
            if bool(payload.get("required", True)) and delegation_id not in results_by_id
        ]
        if missing_required:
            raise ValueError(
                "required native Delegation Results are missing: "
                + ", ".join(sorted(missing_required))
            )
        settled_refs: list[dict[str, Any]] = []
        remaining = list(active_refs)
        child_results = list(accepted.get("child_results") or [])
        native_evidence = dict(usage_snapshot.get("native_delegations") or {})
        for delegation_id, raw in results_by_id.items():
            request_ref, _ = native_requests[delegation_id]
            suffix = hashlib.sha256(delegation_id.encode("utf-8")).hexdigest()[:20]
            result_ref = self.task_documents.write_revision(
                project_id=project_id, task_id=str(run["run_id"]),
                document_kind=f"delegation-result-{suffix}",
                document_schema="delegation-result.v1",
                payload={
                    "delegation_id": delegation_id,
                    "status": str(raw["status"]),
                    "summary": str(raw["summary"]),
                    "findings": list(raw.get("findings") or []),
                    "changed_files": list(raw.get("changed_files") or []),
                    "test_results": list(raw.get("test_results") or []),
                    "external_effect_refs": list(raw.get("external_effect_refs") or []),
                    "evidence_refs": list(raw.get("evidence_refs") or []),
                    "artifacts": list(raw.get("artifacts") or []),
                    "uncertainties": list(raw.get("uncertainties") or []),
                    "residual_risks": list(raw.get("residual_risks") or []),
                    "recommended_next_action": str(
                        raw.get("recommended_next_action") or ""
                    ),
                    "native_event_evidence": native_evidence,
                },
                status=str(raw["status"]), actor_type="native_subagent",
                actor_id=delegation_id, run_id=str(run["run_id"]),
                reason="PM accepted native subagent Result",
                source_refs=[request_ref],
            )
            remaining = [item for item in remaining if item != request_ref]
            if result_ref not in child_results:
                child_results.append(result_ref)
            settled_refs.append(result_ref)
        accepted["active_delegations"] = remaining
        accepted["child_results"] = child_results
        return accepted, settled_refs

    def _project_configuration(self, project_id: str, task: dict[str, Any] | None = None) -> dict[str, Any]:
        from assistant_kernel.services.published_agents import PublishedAgentService
        return PublishedAgentService(self.settings).project_policy(project_id,task=task)

    def _project_task_protocol(self, project_id: str, task: dict[str, Any] | None = None) -> str | None:
        value = str(
            self._project_configuration(project_id,task).get("task_protocol") or ""
        ).strip()
        return value or None

    def _five_stage_task(self, project_id: str, task: dict[str, Any] | None = None) -> bool:
        request = (task or {}).get("request_json") or {}
        if request.get("design_required") or request.get("phase_protocol") == "five-stage.v1":
            return True
        project = self._project_configuration(project_id,task)
        return bool(project.get("phase_protocol") == "five-stage.v1"
                    or (project.get("single_agent") or {}).get("design_required")
                    or (project.get("work_modes") or {}).get("design"))

    def _work_mode_bundle(self, project_id: str, mode: str, task: dict[str, Any] | None = None) -> dict[str, Any]:
        from assistant_kernel.services.agent_results import normalize_phase
        original_mode = mode
        mode = normalize_phase(mode)
        if mode not in {"requirements", "design", "build", "audit", "operate"}:
            raise ValueError(f"unknown Project Work Mode: {mode}")
        if task and (task.get('request_json') or {}).get('protocol_revision')=='1.2':
            config,uri,digest=self._load_pinned_config(task)
            if config.foundation_revision!='1.2' or config.phase_mode!=mode:
                raise PermissionError('Confirmed Task lacks its fixed phase configuration')
            bundle={'schema_version':'work-mode-bundle.v1','mode_id':mode,'profile':mode,'context_profile':mode,
                'permission_profile':'candidate-write' if mode=='build' else 'read-only',
                'validation_policy':dict((task.get('request_json') or {}).get('validation_policy') or
                    {'required_scopes':['structure','component']}),
                'phase_profile':{'mode_id':mode,'config_ref':{'uri':uri,'sha256':digest},
                    'skill_bindings':list(config.skill_bindings),'source':'confirmed published configuration',
                    'file_agreement':'project_shared'}}
            bundle['sha256']=hashlib.sha256(json.dumps(bundle,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
            return bundle
        project = self._project_configuration(project_id,task)
        configured = dict((project.get("work_modes") or {}).get(mode) or (project.get("work_modes") or {}).get(original_mode) or {})
        if not configured:
            raise ValueError(f"Project has no published Work Mode bundle: {mode}")
        profile = str(configured.get("profile") or mode).strip()
        context_profile = str(
            configured.get("context_profile") or mode
        ).strip()
        permission_profile = str(
            configured.get("permission_profile") or ""
        ).strip()
        if permission_profile not in {"read-only", "candidate-write"}:
            raise ValueError("Work Mode permission_profile is invalid")
        validation_policy = configured.get("validation_policy")
        if validation_policy is None:
            validation_policy = (project.get("single_agent") or {}).get(
                "validation_policy"
            ) or {"required_scopes": ["structure", "component"]}
        bundle = {
            "schema_version": "work-mode-bundle.v1",
            "mode_id": mode,
            "profile": profile,
            "context_profile": context_profile,
            "permission_profile": permission_profile,
            "validation_policy": dict(validation_policy),
            "phase_profile": self._stage_profile(project_id, profile),
        }
        bundle["sha256"] = hashlib.sha256(json.dumps(
            bundle, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        return bundle

    def _stage_profile(self, project_id: str, stage: str) -> dict[str, Any]:
        path = (
            self.runtime.describe(project_id).vault_root / "agents" / "pm"
            / "profiles" / f"{stage}.yaml"
        )
        if not path.is_file():
            if self._five_stage_task(project_id):
                raise FileNotFoundError(f"required published phase profile is missing: {stage}")
            return {
                "schema_version": "project-agent-profile.v1", "mode_id": stage,
                "role_append": f"# Active phase · {stage}\nFollow the Kernel's {stage} phase and its approval gates.",
                "disabled_tools": [] if stage in {"build", "repair"} else ["write_candidate_file", "delete_candidate_file"],
            }
        value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(value, dict) or value.get("mode_id") != stage:
            raise ValueError(f"invalid Project Work Mode profile: {stage}")
        return value

    def _prior_attempt_trace_state(
        self, run: dict[str, Any], executor: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Inspect the previous invocation trace before a retry resumes a native Session.

        The Kernel-owned ``codex exec --json`` trace is a more stable recovery
        boundary than parsing Codex's internal rollout format.  A first attempt
        has no prior trace.  Missing or malformed evidence fails open to the
        existing reconciliation behavior instead of rotating a healthy Session.
        """
        if int(run.get("attempt") or 0) <= 1:
            return None
        project_id = str(run["project_id"])
        trace_uri = str(executor.get("trace_uri") or "")
        prefix = f"vault://{project_id}/"
        if not trace_uri.startswith(prefix):
            return None
        try:
            runtime = self.runtime.describe(project_id)
            trace_root = self.runtime.resolve_below(
                runtime.vault_root, trace_uri[len(prefix):]
            )
        except ValueError:
            return None
        trace = trace_root / "stdout.jsonl" if trace_root.is_dir() else trace_root
        if not trace.is_file():
            return None
        thread_id = ""
        turn_started = False
        turn_completed = False
        turn_failed = False
        with trace.open("r", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                event_type = str(event.get("type") or "")
                if event_type == "thread.started":
                    thread_id = str(
                        event.get("thread_id")
                        or (event.get("thread") or {}).get("id")
                        or thread_id
                    )
                elif event_type == "turn.started":
                    turn_started = True
                elif event_type == "turn.completed":
                    turn_completed = True
                elif event_type == "turn.failed":
                    turn_failed = True
        state = (
            "completed" if turn_completed
            else "incomplete" if turn_started or turn_failed
            else "not_started"
        )
        return {
            "state": state,
            "thread_id": thread_id or None,
            "trace_uri": trace_uri,
            "turn_started": turn_started,
            "turn_completed": turn_completed,
            "turn_failed": turn_failed,
        }

    async def _build_native_boundary_checkpoint(
        self, session: dict[str, Any], run: dict[str, Any], *, reason: str,
    ) -> dict[str, Any]:
        """A deterministic boundary record, never a generated conversation summary.

        The old handoff column remains a compatibility storage slot. New content
        refers to accepted artifacts and original traces; prior chats and old
        cumulative summaries are not copied into a fresh physical session.
        """
        from assistant_kernel.services.document_runtime import resource_ref
        project_id = str(run["project_id"])
        request = dict(run.get("request_json") or {})
        documents = dict(request.get("accepted_documents") or {})
        for value in documents.values():
            # Legacy accepted state includes collections (delegations/results)
            # and nullable single references. Preserve the original shape.
            for ref in value if isinstance(value, list) else [value]:
                if ref is not None:
                    self.task_documents.read(project_id, ref)
        source_refs = []
        if session.get("archive_uri"):
            source_refs.append(resource_ref(kind="trace", identity=str(session["session_id"]),
                uri=str(session["archive_uri"]), sha256=session.get("archive_sha256"),
                purpose="prior native execution evidence; read only when relevant"))
        force = request.get("force_session_rotation") or {}
        original_reason = str(force.get("reason") or reason) if isinstance(force, dict) else reason
        normalized = {
            "public_background_changed": "loaded_source_visibility_changed",
            "config_changed": "configuration_incompatible",
            "config_compatibility_unknown": "configuration_incompatible",
            "interrupted_native_turn": "provider_recovery_requested",
            "requirements_revision": "phase_changed", "design_revision": "phase_changed",
            "supervisor_requested": "user_requested",
            "supervisor_action_plan": "legacy_supervisor_action_plan",
        }.get(original_reason, original_reason)
        allowed = {"user_requested", "new_task", "independent_review", "configuration_incompatible",
                   "loaded_source_visibility_changed", "provider_recovery_requested", "phase_changed",
                   "legacy_supervisor_action_plan"}
        if normalized not in allowed:
            raise ValueError("unsupported explicit native session boundary")
        if normalized == 'legacy_supervisor_action_plan':
            ref=force.get('source_action_plan_ref')
            if not ref:raise ValueError('Legacy explicit boundary needs its accepted Action Plan source')
            self.task_documents.read(project_id,ref)
            source_refs.append(ref)
        packet = {
            "schema_version": "kernel-native-boundary-state.v1",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "reason": normalized, "origin_reason": original_reason,
            "identity": {"project_id": project_id, "agent_key": str(run["agent_key"])},
            "lineage": {"from_session_id": str(session["session_id"]),
                        "from_native_session_id": session.get("native_session_id")},
            "source_run_id": str(run["run_id"]), "current_documents": documents,
            "source_refs": source_refs, "prior_stage_chat_loaded": False,
            "coverage": {"to_message_id": None},
            "recovery_rule": "Check persisted results and original traces before continuing; never replay unknown effects.",
        }
        packet["content_sha256"] = hashlib.sha256(json.dumps(packet,ensure_ascii=False,
            sort_keys=True,separators=(",",":"),default=str).encode()).hexdigest()
        return packet

    @staticmethod
    def _usage_snapshot(
        invocation_id: str, result: Any, context_window: int, ratio: float,
    ) -> dict[str, Any]:
        known = result.metadata.get("context_usage_known") is not False
        return {
            "invocation_id": invocation_id,
            "native_session_id": result.physical_session_id,
            "input_tokens": result.usage.input_tokens,
            "cached_input_tokens": result.usage.cached_input_tokens,
            "output_tokens": result.usage.output_tokens,
            "reasoning_output_tokens": result.usage.reasoning_output_tokens,
            "context_window_tokens": context_window,
            "context_ratio": ratio,
            "context_usage_known": known,
            "usage_source": result.metadata.get("usage_source", "harness_usage"),
            "trace_uri": result.archive_uri,
            "trace_sha256": result.archive_sha256,
            "measured_at": datetime.now(timezone.utc).isoformat(),
        }

    def _load_pinned_config(
        self, run: dict[str, Any]
    ) -> tuple[FileAgentConfig, str, str]:
        project_id = str(run["project_id"])
        executor = dict(run["executor_json"] or {})
        uri = executor.get("config_snapshot_uri") or executor.get("agent_config_snapshot_uri")
        file_digest = executor.get("config_bundle_hash") or executor.get("agent_snapshot_sha256")
        if uri and file_digest:
            config = self.agents.load_snapshot(project_id, str(uri), str(file_digest))
            return config, str(uri), str(file_digest)
        request = dict(run.get("request_json") or {})
        from assistant_kernel.services.published_agents import PublishedAgentService
        config = PublishedAgentService(self.settings).load(
            project_id, str(run["agent_key"]),
            task_id=str(request.get('task_root_run_id') or request.get('task_id') or run['run_id']),
            phase=str(request.get('current_stage') or request.get('work_mode') or '') or None,
            task_kind=str(request.get("task_kind") or "project_task"),
            requested_skills=[str(item) for item in request.get("requested_skills") or []],
        )
        uri, file_digest = self.agents.snapshot(project_id, config)
        return self.agents.load_snapshot(project_id, uri, file_digest), uri, file_digest

    def _normalize_artifacts(self, project_id: str, artifacts: list[Any]) -> list[dict[str, Any]]:
        prefix = f"vault://{project_id}/"
        normalized: list[dict[str, Any]] = []
        for item in artifacts:
            uri = str(item.get("uri") if isinstance(item, dict) else item)
            if not uri.startswith(prefix):
                raise ValueError("artifact URI must belong to the current Project Vault")
            target = self.runtime.resolve_vault_file(project_id, uri[len(prefix):])
            if not target.is_file():
                raise ValueError(f"artifact does not exist: {uri}")
            data = target.read_bytes()
            metadata = dict(item) if isinstance(item, dict) else {}
            metadata.pop("sha256", None)
            metadata.pop("size", None)
            normalized.append({
                **metadata,
                "uri": uri, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
            })
        return normalized

    def _materialize_submission(
        self, run: dict[str, Any], document_markdown: str
    ) -> dict[str, Any]:
        project_id = str(run["project_id"])
        data = document_markdown.encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        relative = (
            Path("runs") / str(run["run_id"]) / "submissions" /
            f"attempt-{int(run['attempt'])}-{digest[:16]}.md"
        )
        target = self.runtime.ensure(project_id).vault_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.read_bytes() != data:
            raise RuntimeError("submission artifact hash collision")
        if not target.exists():
            temporary = target.with_suffix(".tmp")
            temporary.write_bytes(data)
            temporary.replace(target)
        return {
            "uri": f"vault://{project_id}/{relative.as_posix()}",
            "sha256": digest,
            "size": len(data),
        }

    def _add_candidate_manifest(
        self, run: dict[str, Any], project_id: str, artifacts: list[Any],
    ) -> list[Any]:
        request = dict(run.get("request_json") or {})
        if (
            run.get("stage") != "implementation"
            and request.get("task_protocol") != "single-agent.v2"
        ):
            return artifacts
        manifest = (
            self.runtime.ensure(project_id).vault_root / "runs" /
            str(run["run_id"]) / "candidate" / "manifest.json"
        )
        if not manifest.is_file():
            if request.get("task_kind") == "skill_change":
                raise ValueError("Skill implementation must produce a candidate manifest")
            return artifacts
        uri = (
            f"vault://{project_id}/"
            f"{manifest.relative_to(self.runtime.ensure(project_id).vault_root).as_posix()}"
        )
        if not any(
            str(item.get("uri") if isinstance(item, dict) else item) == uri
            for item in artifacts
        ):
            artifacts.append({
                "uri": uri, "kind": "candidate_manifest", "required": True,
                "producer_run_id": str(run["run_id"]),
                "producer_stage": str(run.get("stage") or "task"),
            })
        return artifacts

    async def _queue_turn_evaluation(
        self, *, project_id: str, task_id: str, turn_number: int,
        trigger: dict[str, Any], reply_message_id: str,
        outcome: str, summary: str, usage: dict[str, Any],
        artifacts: list[Any] | None = None,
    ) -> None:
        """Pin the settled Turn, then queue its Supervisor consultation.

        Idempotent per task+turn. A modern Task stays behind its durable
        supervision barrier if dispatch or handoff archiving fails.
        """
        five_stage = True
        try:
            task_run = await self.repository.get_run(task_id)
            if task_run is None:
                raise RuntimeError("settled Task disappeared before Supervisor handoff")
            five_stage = self._five_stage_task(project_id, task_run)
            if not self.goal_supervisor.configuration(project_id)["enabled"]:
                if five_stage:
                    await self.repository.pause_supervision(
                        task_id=task_id, turn_number=turn_number, reason="Supervisor is disabled",
                    )
                    return
                resume_degraded = getattr(
                    self.repository, "resume_supervision_degraded", None
                )
                if callable(resume_degraded):
                    await resume_degraded(
                        task_id=task_id, turn_number=turn_number,
                        reason="Supervisor is disabled",
                    )
                return
            task_request = dict(task_run.get("request_json") or {})
            source_context_epoch = int(
                task_request.get("supervisor_context_epoch") or 0
            )
            reply = await self.repository.get_message(reply_message_id)
            accepted_documents = dict(task_request.get("accepted_documents") or {})
            current_refs = {
                str(kind): dict(ref) for kind, ref in accepted_documents.items()
                if isinstance(ref, dict)
            }
            pm_evidence = dict(usage.get("pm_evidence") or {})
            pm_evidence["artifact_refs"] = _bounded_pm_artifact_refs(artifacts)
            usage["pm_evidence"] = pm_evidence
            turn_handoff = {
                "schema_version": "supervisor-turn-handoff.v1",
                "project_id": project_id,
                "task_id": task_id,
                "turn_number": turn_number,
                "source_context_epoch": source_context_epoch,
                "pm_run_id": task_id,
                "pm_reply_message_id": reply_message_id,
                "source_message_id": str(trigger.get("message_id") or ""),
                "outcome": outcome,
                "summary": str(summary or "")[:2000],
                "usage": json_compatible(usage),
                "pm_evidence": json_compatible(pm_evidence),
                "task_lifecycle": task_lifecycle_view(task_run),
                "accepted_documents": accepted_documents,
                "current_refs": current_refs,
                "created_at": str((reply or {}).get("created_at") or ""),
            }
            encoded = json.dumps(
                turn_handoff, ensure_ascii=False, sort_keys=True, indent=2,
                default=str,
            ).encode("utf-8")
            relative = (
                Path("runs") / task_id / "supervisor" / "turn-handoffs"
                / f"turn-{turn_number}.json"
            )
            target = self.runtime.ensure(project_id).vault_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() and target.read_bytes() != encoded:
                raise RuntimeError(
                    f"immutable Supervisor Turn handoff conflict: turn {turn_number}"
                )
            if not target.exists():
                temporary = target.with_suffix(target.suffix + ".tmp")
                temporary.write_bytes(encoded)
                temporary.replace(target)
            handoff_ref = {
                "uri": f"vault://{project_id}/{relative.as_posix()}",
                "sha256": hashlib.sha256(encoded).hexdigest(),
                "size": len(encoded),
            }
            await self._enqueue_supervisor_call(
                project_id=project_id,
                agent_key=f"project:{project_id}:supervisor",
                run_type="chat",
                trigger_message_id=reply_message_id,
                request={
                    "interaction_kind": "supervisor_consultation",
                    "schema_version": "supervisor-consultation.v3",
                    "task_id": task_id,
                    "trigger": "after_agent_turn",
                    "turn_number": turn_number,
                    "source_context_epoch": source_context_epoch,
                    "source_turn_number": turn_number,
                    "turn_handoff_ref": handoff_ref,
                    "turn_handoff_sha256": handoff_ref["sha256"],
                    "source_message_id": str(trigger.get("message_id") or ""),
                    "interaction_head": {
                        "context_epoch": source_context_epoch,
                        "source_message_id": str(trigger.get("message_id") or ""),
                        "pm_reply_message_id": reply_message_id,
                    },
                    "source_actor_type": "pm",
                    "source_agent_key": f"project:{project_id}:manager",
                    "source_run_id": task_id,
                    "source_interface_role": "pm",
                    "source_channel": str(trigger["channel"]),
                    "source_chat_id": str(trigger["chat_id"]),
                    "channel": str(trigger["channel"]),
                    "chat_id": str(trigger["chat_id"]),
                    "user_id": trigger["user_id"],
                    "agent_turn_report": {
                        "outcome": outcome,
                        "summary": str(summary or "")[:2000],
                        "usage": usage,
                    },
                },
                idempotency_key=f"supervisor-eval:{task_id}:{turn_number}",
            )
        except Exception:
            logging.exception(
                "failed to queue supervisor evaluation for task %s turn %s",
                task_id, turn_number,
            )
            if five_stage:
                await self.repository.pause_supervision(
                    task_id=task_id, turn_number=turn_number,
                    reason="Supervisor evaluation could not be queued",
                )
            else:
                await self.repository.resume_supervision_degraded(
                    task_id=task_id, turn_number=turn_number,
                    reason="Supervisor evaluation could not be queued",
                )

    async def _enqueue_supervisor_call(self, **kwargs):
        project_id = str(kwargs["project_id"])
        request = dict(kwargs["request"])
        task = await self.repository.get_run(str(request["task_id"]))
        if self._five_stage_task(project_id, task):
            return await self.repository.attach_supervision_handoff(str(request["task_id"]), request)
        return await self.repository.create_run(**kwargs)

    async def _record_turn_file_changes(
        self, *, project_id: str, run: dict[str, Any], invocation_id: str,
        trigger: dict[str, Any], roots: list[Any], file_effects: dict[str, Any] | None,
    ) -> None:
        """Archive observed effects once; a navigation failure never repeats a Turn."""
        from assistant_kernel.services.project_changes import ProjectChangeService
        changes = ProjectChangeService(self.settings)
        source_refs = [{"kind": "message", "id": str(trigger["message_id"])}]
        reason = "PM 回合的文件结算；本轮输入：" + str((trigger.get("content_json") or {}).get("text") or "")[:2000]
        try:
            if file_effects and file_effects.get("changes"):
                file_effects["project_change_batch"] = await changes.capture_batch(
                    project_id, batch_id=f"turn:{run['run_id']}:{invocation_id}", roots=roots,
                    changes=file_effects["changes"], task_id=str(run["run_id"]), run_id=str(run["run_id"]),
                    actor="pm", reason=reason, reason_source_refs=source_refs,
                    exact_time=False,
                )
            manifest = self.runtime.describe(project_id).vault_root / "runs" / str(run["run_id"]) / "candidate" / "manifest.json"
            if manifest.is_file():
                value = json.loads(manifest.read_text(encoding="utf-8"))
                await changes.record_candidate_manifest(
                    project_id, str(run["run_id"]), str(run["run_id"]), value,
                    reason=reason, reason_source_refs=source_refs, roots=roots,
                )
        except Exception:
            logging.exception("file change navigation update pending for %s", run["run_id"])
            changes.maps.mark_stale(project_id, reason="Turn file change navigation pending")

    async def _record_document_commit(self, *, project_id, run, revision, refs, previous, roots, source_refs):
        from assistant_kernel.services.project_changes import ProjectChangeService
        changes = ProjectChangeService(self.settings)
        try:
            navigation = changes.maps.load(project_id)
            previous_heads = {item.get("document_id"): item.get("document_ref")
                              for item in navigation.get("files") or [] if item.get("kind") == "document"}
            receipts = []
            for name, ref in refs.items():
                if not isinstance(ref, dict) or ref.get("kind") != "document":
                    continue
                prior = previous.get(name) or previous_heads.get(ref.get("id"))
                receipt = self.task_documents.change_receipt(
                    project_id, ref, previous_ref=prior, purpose=name, state_revision=revision,
                    context_epoch=int((run.get("request_json") or {}).get("supervisor_context_epoch") or 0),
                )
                if receipt:
                    receipts.append(receipt)
            if receipts:
                return await changes.capture_batch(
                    project_id, batch_id=f"documents:{run['run_id']}:{revision}", roots=roots,
                    changes=receipts, task_id=str(run["run_id"]), run_id=str(run["run_id"]),
                    actor="kernel", scope_kind="artifact", reason="Committed PM phase documents and state",
                    reason_source_refs=source_refs,
                )
        except Exception:
            logging.exception("Kernel document Map update pending for %s", run["run_id"])
            changes.maps.mark_stale(project_id, reason="Kernel document navigation update pending")

    async def process(
        self, run: dict[str, Any], worker_instance_id: str,
        cancel_event: asyncio.Event | None = None,
    ) -> dict[str, Any]:
        from assistant_kernel.services.kernel_management import require_active_identity
        require_active_identity(str(run["project_id"]), str(run["agent_key"]))
        if run["run_type"] in {"python", "publish"}:
            raise ValueError(f"{run['run_type']} runs use a deterministic processor")
        claim_token = str(run.get("claim_token") or "")
        if not claim_token:
            raise RuntimeError("claimed run has no claim token")
        validation=(run.get('executor_json') or {}).get('validation_process') or {}
        if validation.get('original_pm_result_ref') and not validation.get('settled_turn_ref') and (run.get('request_json') or {}).get('protocol_revision')=='1.2':
            if validation.get('cleanup',{}).get('status')!='verified' or validation.get('outcome')!='succeeded' or not validation.get('original_turn_evidence_ref'):
                raise PermissionError('Preserved validation is unresolved; do not start another model or validator')
            from assistant_kernel.services.prompt_protocol import PromptProtocol
            from assistant_kernel.services.pm_turns_v12 import PMTurnsV12
            preserved=PromptProtocol(self.settings,self.repository)
            original=preserved.read_blob(run['project_id'],validation['original_pm_result_ref'])
            evidence=preserved.read_blob(run['project_id'],validation['original_turn_evidence_ref'])
            return await PMTurnsV12(self.settings,self.repository).settle(run=run,worker_instance_id=worker_instance_id,
                public_result=original,usage_snapshot=evidence['usage_snapshot'],file_effects=evidence['file_effects'],cancel_event=cancel_event)
        if (run.get("request_json") or {}).get("interaction_kind") == "child_task" or str(run["agent_key"]).endswith((":child", ":operator")):
            raise PermissionError("child Run and Operator Agent are retired")
        if run["run_type"] == "stage":
            raise PermissionError("Stage execution is retired; use single-agent.v2")
        trigger = await self.repository.get_message(str(run["trigger_message_id"]))
        if trigger is None:
            raise ValueError("run trigger message was not found")
        project_id = str(run["project_id"])
        agent_key = str(run["agent_key"])
        single_agent_task = bool(
            run["run_type"] == "chat"
            and agent_key.endswith(":manager")
            and self._project_task_protocol(project_id,run) == "single-agent.v2"
        )
        turn_inputs = await self.repository.pin_turn_inputs(str(run["run_id"]),worker_instance_id,claim_token) if single_agent_task else []
        run = {**run, "executor_json": {**dict(run.get("executor_json") or {}), "turn_input_message_ids": [str(m["message_id"]) for m in turn_inputs]}}
        single_agent_audit = bool(
            single_agent_task and ((run.get("request_json") or {}).get("audit_mode") or (run.get("request_json") or {}).get("current_stage") == "audit")
        )
        if single_agent_task:
            current_request = dict(run.get("request_json") or {})
            if current_request.get("task_protocol") != "single-agent.v2":
                trigger_goal = str((trigger.get("content_json") or {}).get("text") or "").strip()
                single_agent_config = (
                    self._project_configuration(project_id,run).get("single_agent") or {}
                )
                multi_agent_config = dict(
                    self._project_configuration(project_id,run).get("multi_agent") or {}
                )
                source_ref = resource_ref(
                    kind="message", identity=str(trigger["message_id"]),
                    purpose="initial_user_request",
                )
                contract_ref = self.task_documents.write_revision(
                    project_id=project_id, task_id=str(run["run_id"]),
                    document_kind="task-contract", document_schema="task-contract.v1",
                    payload={
                        "goal": trigger_goal,
                        "scope": {"included": [], "excluded": []},
                        "requirements": [], "acceptance_criteria": [],
                        "constraints": [], "source_refs": [source_ref],
                    },
                    status="draft", actor_type="kernel", actor_id="kernel",
                    run_id=str(run["run_id"]), reason="bootstrap Task contract",
                    source_refs=[source_ref],
                )
                initialized = {
                    "task_protocol": "single-agent.v2",
                    "task_root_run_id": str(run["run_id"]),
                    "task_goal": trigger_goal,
                    "task_turn_count": 0,
                    "task_phase": "working",
                    "current_stage": "requirements",
                    "work_mode": "requirements",
                    "state_revision": 1,
                    "accepted_documents": {"task_contract": contract_ref},
                    "candidate_only": True,
                    "network_allowed": True,
                    "max_task_turns": 12,
                    "multi_agent_enabled": bool(
                        multi_agent_config.get("enabled", False)
                    ),
                    "max_concurrent_subagents": min(max(int(
                        multi_agent_config.get("max_concurrent_subagents") or 3
                    ), 1), 3),
                    "audit_required": bool(single_agent_config.get("audit_required", False) or self._five_stage_task(project_id, run)),
                    "design_required": self._five_stage_task(project_id, run),
                    "validation_policy": dict(
                        single_agent_config.get("validation_policy") or {
                            "required_scopes": ["structure", "component"]
                        }
                    ),
                }
                run = await self.repository.patch_claimed_run_request(
                    run_id=str(run["run_id"]), worker_instance_id=worker_instance_id,
                    claim_token=claim_token, request_patch=initialized,
                )
                self.task_documents.update_current(
                    project_id=project_id, task_id=str(run["run_id"]),
                    document_kind="task-contract", ref=contract_ref,
                    commit_id=f"task-bootstrap:{run['run_id']}", state_revision=1,
                )
        audit_run = bool(single_agent_audit or is_audit_schema(
            str((trigger.get("content_json") or {}).get("schema_version") or "")
        ))
        current_stage = str((run.get("request_json") or {}).get("current_stage") or "requirements")
        # v1.2 has one authoritative admission path below: the confirmed package.
        # Reapplying the legacy engineering gate wrongly requires Design even
        # for an approved research-only Audit.
        if single_agent_task and (run.get('request_json') or {}).get('protocol_revision')!='1.2' and self._five_stage_task(project_id, run):
            state = dict(run.get("request_json") or {})
            approved_kinds = ["task_contract"] if current_stage == "design" else (
                ["task_contract", "solution_design"] if current_stage in {"build", "audit"} else []
            )
            for kind in approved_kinds:
                ref = dict((state.get("accepted_documents") or {}).get(kind) or {})
                approval = state.get("requirements_approval" if kind == "task_contract" else "design_approval") or {}
                if not ref or ref.get("sha256") != approval.get("approved_document_sha256"):
                    raise PermissionError(f"Current {current_stage} requires approval of the effective {kind}")
                self.task_documents.read(project_id, ref)
        work_mode_bundle = (
            self._work_mode_bundle(
                project_id, "audit" if single_agent_audit else current_stage, run
            )
            if single_agent_task else None
        )
        if single_agent_task:
            plan_variant = "audit" if audit_run else current_stage
            run = {**run, "request_json": {
                **dict(run.get("request_json") or {}),
                "execution_plan_variant": plan_variant,
                "work_mode_bundle": work_mode_bundle,
                "work_mode_bundle_sha256": work_mode_bundle["sha256"],
                "context_profile": work_mode_bundle["context_profile"],
                "permission_profile_name": work_mode_bundle["permission_profile"],
                "validation_policy": work_mode_bundle["validation_policy"],
            }}
        handoff = None
        handoff_uri = None
        handoff_sha256 = None
        config, snapshot_uri, config_bundle_hash = self._load_pinned_config(run)
        confirmed_business_draft=None;confirmed_capability=None;confirmed_package=None
        if single_agent_task and config.foundation_revision=='1.2':
            from assistant_kernel.services.prompt_protocol import PromptProtocol
            async with self.repository.database.connection() as admission:
                current=await (await admission.execute('SELECT * FROM assistant.runs WHERE run_id=%s FOR UPDATE',(run['run_id'],))).fetchone()
                if (not current or current['status']!='running' or str(current.get('worker_instance_id'))!=worker_instance_id
                        or str(current.get('claim_token'))!=claim_token or current.get('cancel_requested_at')):
                    raise PermissionError('PM lease or stop fence changed before model preparation')
                confirmed_package,confirmed_business_draft=await PromptProtocol(self.settings,self.repository).validate_dispatch_locked(admission,current)
                confirmed_capability=PromptProtocol(self.settings,self.repository).read_blob(project_id,confirmed_package['capability_ref'])
                if confirmed_capability.get('schema_version')!='kernel-pm-capability-profile.v1' or confirmed_capability.get('target_actor')!=agent_key:
                    raise PermissionError('Confirmed package has no actual target PM capability preflight')
        if single_agent_task and config.foundation_revision != '1.2':
            phase_profile = self._stage_profile(project_id, str(work_mode_bundle["profile"]))
            config = self.agents.apply_phase_profile(
                config, phase_profile,
                requested_skills=list((run.get("request_json") or {}).get("selected_skills") or []),
            )
            snapshot_uri, config_bundle_hash = self.agents.snapshot(project_id, config)
        elif audit_run and config.foundation_revision != '1.2':
            config = self.agents.apply_phase_profile(config, self._stage_profile(project_id, "audit"))
            snapshot_uri, config_bundle_hash = self.agents.snapshot(project_id, config)
        if config.agent_key != agent_key:
            raise ValueError("run agent_key does not match the selected Agent configuration")
        requested_executor = dict(run["executor_json"] or {})
        invocation_configuration=None;invocation_values={};resolved_config_ref=None;role_budget=None
        if config.foundation_revision=='1.2':
            from assistant_kernel.services.invocation_config import InvocationConfiguration
            invocation_configuration=InvocationConfiguration(self.settings)
            config,invocation_values,resolved_config_ref=await invocation_configuration.resolve(config,
                project_id=project_id,run_id=str(run['run_id']),
                invocation_id=f"{run['run_id']}-a{run['attempt']}-{claim_token}",
                pinned_ref=requested_executor.get('resolved_config_snapshot_ref'))
            role_budget=invocation_configuration.check_role_budget(config,invocation_values)
            if confirmed_capability is not None:
                preflight=self.task_documents.read(project_id,confirmed_capability['resolved_config_ref'])['payload']
                actual_defaults=self.task_documents.read(project_id,resolved_config_ref)['payload']
                def declarations(snapshot):
                    return [{key:item.get(key) for key in ('path','mode','override_value')}
                        for item in sorted(snapshot['bindings'],key=lambda entry:entry['path'])]
                if declarations(preflight)!=declarations(actual_defaults):
                    raise PermissionError('Accepted configuration inheritance/override declarations changed after confirmation')
            catalog=getattr(self.harness,'model_catalog',None)
            if catalog is not None:catalog.cache_ttl_seconds=invocation_values['model_catalog_cache_seconds']
            snapshot_uri,config_bundle_hash=self.agents.snapshot(project_id,config)
        current_scope_roots = self.plans.workspace_scopes.resolve(
            project_id, str(run["run_id"]), str(run.get("stage") or "") or None,
            shared_agreement=project_id != 'system',
        )
        if single_agent_task:
            from assistant_kernel.services.project_changes import ProjectChangeService
            try:
                await ProjectChangeService(self.settings).refresh_map(
                    project_id, roots=list(current_scope_roots), task_id=str(run["run_id"]),
                    run_id=str(run["run_id"]),
                )
            except Exception:
                logging.exception("Project Map reconciliation pending for %s", project_id)
        # Every Project role uses the same file agreement. Analysis/Review
        # remain behaviorally read-only; result-protocol adoption is separate.
        effective_sandbox = 'danger-full-access' if project_id != 'system' else config.sandbox
        provider = requested_executor.get("provider") or config.provider or self.settings.default_codex_provider
        requested_model = (
            requested_executor.get("model") or config.model or self.settings.default_codex_model
        )
        requested_reasoning_effort = (
            requested_executor.get("reasoning_effort") or config.reasoning_effort
            or self.settings.default_codex_reasoning_effort
        )
        context_policy = str(
            requested_executor.get("context_policy") or config.context_policy
            or self.settings.default_codex_context_policy
        )
        custom_context_window = (
            requested_executor.get("model_context_window_tokens")
            if requested_executor.get("model_context_window_tokens") is not None
            else config.model_context_window_tokens
        )
        custom_auto_compact = (
            requested_executor.get("model_auto_compact_token_limit")
            if requested_executor.get("model_auto_compact_token_limit") is not None
            else config.model_auto_compact_token_limit
        )
        pinned_profile = requested_executor.get("model_profile")
        if requested_executor.get("resolved_plan_uri") and isinstance(pinned_profile, dict) and confirmed_capability is None:
            profile_data = dict(pinned_profile)
            provider = str(profile_data["provider"])
            model = str(profile_data["model"]) if profile_data.get("model") else None
            reasoning_effort = (
                str(profile_data["reasoning_effort"])
                if profile_data.get("reasoning_effort") else None
            )
            context_policy = str(profile_data["context_policy"])
            model_context_window_tokens = profile_data.get("model_context_window_tokens")
            model_auto_compact_token_limit = profile_data.get(
                "model_auto_compact_token_limit"
            )
            effective_context_window_tokens = profile_data.get(
                "effective_context_window_tokens"
            )
            catalog_context_window_tokens = profile_data.get(
                "catalog_context_window_tokens"
            )
            catalog_max_context_window_tokens = profile_data.get(
                "catalog_max_context_window_tokens"
            )
            effective_context_window_percent = profile_data.get(
                "effective_context_window_percent"
            )
        elif callable(resolve_profile := getattr(self.harness, "resolve_model_profile", None)):
            model_profile = await resolve_profile(
                project_id=project_id,
                provider=str(provider),
                model=str(requested_model) if requested_model else None,
                reasoning_effort=str(requested_reasoning_effort) if requested_reasoning_effort else None,
                context_policy=context_policy,
                custom_context_window_tokens=(
                    int(custom_context_window) if custom_context_window is not None else None
                ),
                custom_auto_compact_token_limit=(
                    int(custom_auto_compact) if custom_auto_compact is not None else None
                ),
            )
            profile_data = model_profile.model_dump()
            provider = model_profile.provider
            model = model_profile.model
            reasoning_effort = model_profile.reasoning_effort
            context_policy = model_profile.context_policy
            model_context_window_tokens = model_profile.model_context_window_tokens
            model_auto_compact_token_limit = model_profile.model_auto_compact_token_limit
            effective_context_window_tokens = model_profile.effective_context_window_tokens
            catalog_context_window_tokens = model_profile.catalog_context_window_tokens
            catalog_max_context_window_tokens = model_profile.catalog_max_context_window_tokens
            effective_context_window_percent = model_profile.effective_context_window_percent
        else:
            model = str(requested_model) if requested_model else None
            reasoning_effort = str(requested_reasoning_effort) if requested_reasoning_effort else None
            model_context_window_tokens = (
                int(custom_context_window) if custom_context_window is not None
                else self.settings.default_context_window_tokens
            )
            model_auto_compact_token_limit = (
                int(custom_auto_compact) if custom_auto_compact is not None
                else model_context_window_tokens * 90 // 100
            )
            effective_context_window_tokens = model_context_window_tokens
            catalog_context_window_tokens = None
            catalog_max_context_window_tokens = None
            effective_context_window_percent = None
            profile_data = {
                "provider": provider,
                "model": model,
                "reasoning_effort": reasoning_effort,
                "context_policy": context_policy,
                "model_context_window_tokens": model_context_window_tokens,
                "model_auto_compact_token_limit": model_auto_compact_token_limit,
                "effective_context_window_tokens": effective_context_window_tokens,
                "catalog_context_window_tokens": None,
                "catalog_max_context_window_tokens": None,
                "effective_context_window_percent": None,
            }
        for label, value in (("provider", provider), ("model", model)):
            if value and not EXECUTOR_NAME.fullmatch(str(value)):
                raise ValueError(f"unsupported {label} name")
        if reasoning_effort and reasoning_effort not in {
            "none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"
        }:
            raise ValueError("unsupported reasoning_effort")
        timeout_seconds = min(
            max(int(requested_executor.get("timeout_seconds") or config.timeout_seconds), 1), 3600
        )
        if model_context_window_tokens is not None and model_context_window_tokens <= 0:
            raise ValueError("resolved model context window must be positive")
        if (
            model_auto_compact_token_limit is not None
            and model_context_window_tokens is not None
            and not 0 < model_auto_compact_token_limit < model_context_window_tokens
        ):
            raise ValueError(
                "model_auto_compact_token_limit must be positive and below the context window"
            )
        invocation_id = f"{run['run_id']}-a{run['attempt']}-{claim_token}"
        source_message_id = str(
            (run["request_json"] or {}).get("answer_message_id") or trigger["message_id"]
        )
        current_input = await self.repository.get_message(source_message_id)
        if current_input is None:
            raise ValueError("current Run input message was not found")
        channel = str(trigger["channel"])
        chat_id = str(trigger["chat_id"])
        request_data = dict(run.get("request_json") or {})
        session_chat_id = chat_id
        if (
            (request_data.get("task_protocol") == "task.v1" and run.get("stage"))
            or request_data.get("task_protocol") == "single-agent.v2"
        ):
            task_root = str(request_data.get("task_root_run_id") or run["run_id"])
            session_chat_id = f"{chat_id}:task:{task_root}"
        current_session = await self.repository.current_session(
            project_id, agent_key, channel, session_chat_id
        )
        prior_attempt_trace = self._prior_attempt_trace_state(run, requested_executor)
        rotation_checkpoint = None
        public_context = {}
        if single_agent_task:
            from assistant_kernel.services.public_background import PublicBackgroundService
            public_context = PublicBackgroundService(self.settings.data_root,self.settings).read_project_context(
                project_id, fields=self._project_configuration(project_id,run).get("background_fields", []),
            )
        public_background_sha256 = public_context.get("selection_sha256") or hashlib.sha256(json.dumps(
            [(doc["uri"], doc["sha256"]) for doc in public_context.get("documents") or []],
            sort_keys=True,
        ).encode("utf-8")).hexdigest()
        scope_compatibility_hash = self.plans.workspace_scopes.configuration_hash(project_id)
        compatibility_hash = self.plans.compatibility_hash(
            config,
            provider=str(provider) if provider else None,
            model=str(model) if model else None,
            reasoning_effort=reasoning_effort,
            context_policy=context_policy,
            model_context_window_tokens=model_context_window_tokens,
            model_auto_compact_token_limit=model_auto_compact_token_limit,
            scope_compatibility_hash=scope_compatibility_hash,
            sandbox=effective_sandbox,
        )
        if public_context.get("documents"):
            compatibility_hash = hashlib.sha256(json.dumps([
                compatibility_hash, public_background_sha256,
            ], sort_keys=True).encode("utf-8")).hexdigest()
        rotation_reason = None
        if current_session and requested_executor.get("public_background_sha256") and requested_executor["public_background_sha256"] != public_background_sha256:
            rotation_reason = "public_background_changed"
        elif current_session and request_data.get("force_session_rotation"):
            rotation_reason = "supervisor_requested"
        elif (
            prior_attempt_trace
            and prior_attempt_trace["state"] == "incomplete"
            and current_session
            and current_session.get("native_session_id")
            and str(current_session["native_session_id"])
                == str(prior_attempt_trace.get("thread_id") or "")
        ):
            rotation_reason = "interrupted_native_turn"
        elif current_session and current_session.get("native_session_id"):
            previous_compatibility = await self.repository.latest_session_compatibility_hash(
                str(current_session["session_id"])
            )
            if previous_compatibility and previous_compatibility != compatibility_hash:
                rotation_reason = "config_changed"
            elif previous_compatibility is None:
                logging.info("Legacy session %s lacks a compatibility hash; retain native continuation unless an explicit boundary applies", current_session['session_id'])
        if rotation_reason:
            rotation_checkpoint = await self._build_native_boundary_checkpoint(
                current_session, run, reason=rotation_reason,
            )
        session = await self.repository.ensure_session(
            project_id, agent_key, channel, session_chat_id,
            rotation_checkpoint=rotation_checkpoint,
        )
        if request_data.get("force_session_rotation"):
            run = await self.repository.patch_claimed_run_request(
                run_id=str(run["run_id"]), worker_instance_id=worker_instance_id,
                claim_token=claim_token,
                request_patch={"force_session_rotation": None},
            )
        plan_uri = str(requested_executor.get("resolved_plan_uri") or "")
        plan_file_sha256 = str(requested_executor.get("resolved_plan_sha256") or "")
        if plan_uri and plan_file_sha256:
            plan, project_mcp_servers = self.plans.load_materialized(
                project_id, plan_uri, plan_file_sha256,
            )
            if str(plan.get("run_id")) != str(run["run_id"]):
                raise ValueError("ResolvedExecutionPlan belongs to another Run")
            if str(plan.get("agent_key")) != agent_key:
                raise ValueError("ResolvedExecutionPlan belongs to another Agent")
        else:
            plan, project_mcp_servers, plan_uri, plan_file_sha256 = self.plans.materialize(
                run=run,
                config=config,
                config_snapshot_uri=snapshot_uri,
                config_snapshot_sha256=config_bundle_hash,
                provider=str(provider) if provider else None,
                model=str(model) if model else None,
                reasoning_effort=reasoning_effort,
                timeout_seconds=timeout_seconds,
                session=session,
                context_policy=context_policy,
                model_context_window_tokens=model_context_window_tokens,
                model_auto_compact_token_limit=model_auto_compact_token_limit,
                effective_context_window_tokens=effective_context_window_tokens,
                catalog_context_window_tokens=catalog_context_window_tokens,
                catalog_max_context_window_tokens=catalog_max_context_window_tokens,
                effective_context_window_percent=effective_context_window_percent,
                scope_compatibility_hash=scope_compatibility_hash,
                sandbox=effective_sandbox,
            )
        if project_id != 'system':
            plan, plan_uri, plan_file_sha256 = self.plans.bind_current_file_agreement(
                plan, invocation_id=invocation_id,
            )
        effective_sandbox = str(
            (plan.get("runtime") or {}).get("sandbox") or effective_sandbox
        )
        permission_profile = str(
            (plan.get("bindings") or {}).get("native_permission_profile") or ""
        ) or None
        permission_profile_sha256 = str(
            (plan.get("bindings") or {}).get("native_permission_profile_sha256") or ""
        ) or None
        may_write_files = bool((plan.get("runtime") or {}).get("may_write_files"))
        may_write_shared = bool((plan.get("runtime") or {}).get("may_write_shared"))
        multi_agent_config = dict(
            self._project_configuration(project_id,run).get("multi_agent") or {}
        )
        multi_agent_enabled = bool(
            single_agent_task and multi_agent_config.get("enabled", False)
        )
        max_concurrent_subagents = min(max(int(
            multi_agent_config.get("max_concurrent_subagents") or 3
        ), 1), 3)
        if invocation_values:
            max_concurrent_subagents=min(max_concurrent_subagents,invocation_values['max_native_subagents'])
            multi_agent_enabled=multi_agent_enabled and max_concurrent_subagents>0
        auth_mode_resolver = getattr(self.harness, "authentication_mode", None)
        auth_mode = (
            str(auth_mode_resolver(project_id))
            if callable(auth_mode_resolver) else "unknown"
        )
        executor_patch = {
            "harness": str(getattr(self.harness, "name", "unknown")),
            "config_snapshot_uri": confirmed_package['config_ref']['uri'] if confirmed_package else snapshot_uri,
            "config_bundle_hash": confirmed_package['config_ref']['sha256'] if confirmed_package else config_bundle_hash,
            "resolved_actor_snapshot_uri": snapshot_uri,
            "resolved_actor_snapshot_sha256": config_bundle_hash,
            "resolved_config_snapshot_ref": resolved_config_ref,
            "role_budget": role_budget,
            "source_files": list(config.source_files),
            "skill_bindings": list(config.skill_bindings),
            "handoff_uri": handoff_uri,
            "handoff_sha256": handoff_sha256,
            "provider": provider, "model": model,
            "reasoning_effort": reasoning_effort, "sandbox": effective_sandbox,
            "auth_mode": auth_mode,
            "may_write_files": may_write_files,
            "may_write_shared": may_write_shared,
            "native_permission_profile": permission_profile,
            "native_permission_profile_sha256": permission_profile_sha256,
            "active_file_grant_ids": list(
                (plan.get("bindings") or {}).get("active_file_grant_ids") or []
            ),
            "context_policy": context_policy,
            "model_profile": profile_data,
            "model_context_window_tokens": model_context_window_tokens,
            "model_auto_compact_token_limit": model_auto_compact_token_limit,
            "effective_context_window_tokens": effective_context_window_tokens,
            "timeout_seconds": timeout_seconds, "scoped_tools": list(config.allowed_tools),
            "scope_profile": config.scope_profile,
            "stage_permissions": (plan.get("bindings") or {}).get("stage_permissions") or {},
            "resolved_plan_uri": plan_uri,
            "resolved_plan_sha256": plan_file_sha256,
            "session_compatibility_hash": compatibility_hash,
            "public_background_sha256": public_background_sha256,
            "invocation_id": invocation_id,
            "trace_uri": (
                f"vault://{project_id}/runs/agent-traces/invocations/{invocation_id}/"
            ),
            "prior_attempt_trace": prior_attempt_trace,
            "audit_schema_version": AUDIT_SCHEMA_VERSION if audit_run else None,
            "multi_agent_enabled": multi_agent_enabled,
            "max_concurrent_subagents": max_concurrent_subagents,
        }
        history = await self.repository.list_messages_through(
            project_id, str(trigger["channel"]), str(trigger["chat_id"]),
            source_message_id, limit=21,
        )
        conversation_goal = await self.repository.current_conversation_goal(
            project_id, str(trigger["channel"]), str(trigger["chat_id"]),
        )
        history = [
            item for item in history
            if str(item["message_id"]) != source_message_id
            and message_context_visibility(item) == "semantic"
            and not (
                item["message_type"] == "action"
                and (item["content_json"] or {}).get("schema_version") == "choice-answer.v1"
            )
        ][:20]
        if single_agent_task and not session.get("native_session_id"):
            history = []  # Fresh phase/task context comes from accepted artifacts, not previous-stage chats.
        history.reverse()
        history_text = "\n".join(
            f"[{item['direction']}/{item['message_type']}] "
            f"{json.dumps(prompt_message_content(item), ensure_ascii=False)}"
            for item in history
        )
        answer = (run["request_json"] or {}).get("answer")
        checkpoint = session["handoff_checkpoint_json"] or {}
        accepted_context: dict[str, Any] = {}
        if single_agent_task:
            request_state = dict(run.get("request_json") or {})
            accepted_documents = dict(request_state.get("accepted_documents") or {})
            context_names = ["task_contract", "last_turn", "current_action_plan", "supervisor_brief", "failed_turn"]
            if current_stage in {"design", "build", "audit", "operate"}:
                context_names.append("solution_design")
            if current_stage == "audit":
                context_names.append("implementation_report")
            if current_stage == "operate":
                context_names.extend(["operation_request", "operation_report", "publication"])
            for name in context_names:
                ref = accepted_documents.get(name)
                if isinstance(ref, dict):
                    accepted_context[name] = self.task_documents.read(
                        project_id, ref
                    )
        response_contract = (
            """# Required final response · cold release audit
Return exactly one JSON object:
{"outcome":"ready_for_release","summary":"audit passed","task_checkpoint":{"goal":"...","completed":[],"pending":[],"decisions":[],"constraints":[],"external_effects":[],"artifacts":[],"open_questions":[],"next_action":"release approval"},"artifacts":[]}
{"outcome":"request_changes","summary":"candidate defects","issues":[{"issue_id":"stable-id","evidence":[],"required_fix":"..."}],"task_checkpoint":{"goal":"...","completed":[],"pending":[],"decisions":[],"constraints":[],"external_effects":[],"artifacts":[],"open_questions":[],"next_action":"repair candidate"},"artifacts":[]}
{"outcome":"blocked","summary":"Kernel evidence or validation infrastructure is incomplete","blocker_owner":"kernel","task_checkpoint":{"goal":"...","completed":[],"pending":[],"decisions":[],"constraints":[],"external_effects":[],"artifacts":[],"open_questions":[],"next_action":"repair Kernel evidence"},"artifacts":[]}
If the accepted Action Plan contains native_subagent Delegation Requests, return the same required `delegation_results` contract described for ordinary PM Turns.
This is a new read-only physical Session. Do not modify candidate or formal files. Treat author conclusions as claims; verify the accepted Task Contract, sealed bundle, validation evidence, permissions, side effects, release and rollback."""
            if single_agent_audit else (
            """# Required final response
Return exactly one JSON object. Valid outcomes are:
{"outcome":"continue","summary":"progress","task_checkpoint":{"goal":"...","completed":[],"pending":[],"decisions":[],"constraints":[],"external_effects":[],"artifacts":[],"open_questions":[],"next_action":"..."},"artifacts":[]}
{"outcome":"needs_input","summary":"analysis before question","questions":[{"question_id":"stable-id","question_text":"complete question","required":true}],"task_checkpoint":{"goal":"...","completed":[],"pending":[],"decisions":[],"constraints":[],"external_effects":[],"artifacts":[],"open_questions":[],"next_action":"wait for answer"},"artifacts":[]}
{"outcome":"needs_approval","summary":"requirements ready","document_markdown":"# Requirements\n...","approval_question":"是否批准当前Requirements并进入Build？","task_checkpoint":{"goal":"...","completed":[],"pending":[],"decisions":[],"constraints":[],"external_effects":[],"artifacts":[],"open_questions":[],"next_action":"wait for requirements approval"},"artifacts":[]}
{"outcome":"return_to_requirements","summary":"approved requirements must change","document_markdown":"# Requirements\n...","task_checkpoint":{"goal":"...","completed":[],"pending":["requirements revision"],"decisions":[],"constraints":[],"external_effects":[],"artifacts":[],"open_questions":[],"next_action":"revise and re-approve requirements"},"artifacts":[]}
{"outcome":"ready_for_validation","summary":"candidate ready","task_checkpoint":{"goal":"...","completed":[],"pending":[],"decisions":[],"constraints":[],"external_effects":[],"artifacts":[],"open_questions":[],"next_action":"Kernel validation"},"artifacts":[]}
{"outcome":"blocked","summary":"...","task_checkpoint":{"goal":"...","completed":[],"pending":[],"decisions":[],"constraints":[],"external_effects":[],"artifacts":[],"open_questions":[],"next_action":"..."},"artifacts":[]}
{"outcome":"failed","summary":"...","task_checkpoint":{"goal":"...","completed":[],"pending":[],"decisions":[],"constraints":[],"external_effects":[],"artifacts":[],"open_questions":[],"next_action":"..."},"artifacts":[]}
If the accepted Action Plan contains native_subagent Delegation Requests, include `delegation_results`. Each completed request must return: {"delegation_id":"stable id","status":"completed|blocked|failed","summary":"...","findings":[],"changed_files":[],"test_results":[],"external_effect_refs":[],"evidence_refs":[],"artifacts":[],"uncertainties":[],"residual_risks":[],"recommended_next_action":"..."}. A required Delegation cannot be omitted.
One Task may span several short Turns. Use continue before expanding beyond the current bounded Turn. Formal source changes still use Task candidate and Publisher. Internet, configured data, account, database, service and trading interfaces are available; record every actual action and result. Do not claim validation, release, external effects, or trading unless receipts prove them."""
            if single_agent_task else
            """# Required final response
Chat Runs may return normal text. Stage Runs must return exactly one JSON object with one of these outcomes:
{"outcome":"completed","summary":"...","artifacts":["vault://project/path"]}
{"outcome":"needs_input","summary":"...","questions":[{"question_id":"stable-id","question_text":"complete question","required":true,"options":[{"id":"a","label":"short choice","description":"optional detail"},{"id":"b","label":"other choice","description":"optional detail"}],"input_schema":{"kind":"project_credential","request_id":"Kernel-returned id","credential_key":"name","fields":[]}}]}
{"outcome":"needs_approval","summary":"...","document_markdown":"complete deliverable","approval_question":"complete approval question","artifacts":[]}
{"outcome":"request_changes","summary":"...","target_stage":"implementation","issues":[{"issue_id":"stable-id","evidence":[],"required_fix":"..."}],"artifacts":[]}
{"outcome":"blocked","summary":"...","artifacts":[]}
For `needs_input`, prefer 1-3 related questions that can be decided together. Every question must include its complete original text. Include `options` only when there are two or three real, mutually exclusive choices; open-ended questions stay free text. Artifact paths must exist in the current Project Vault. Do not claim a tool, file change, test, approval, or external effect that did not happen."""
            )
        )
        response_contract += (
            "\nFor needs_input, group 1-3 related decisions when useful. Each question has "
            "a short topic and complete question_text. For finite options, provide "
            "recommended_option_id matching a real option and a concise recommendation_reason "
            "when you have a supported recommendation; never invent one. Kernel supplies "
            "a free-text alternative and final additional comments. Do not add a fake 'Other' option."
        )
        if single_agent_task and (run.get("request_json") or {}).get("design_required"):
            response_contract = """# Required final response
Return one agent-result.v1 JSON object:
{"schema_version":"agent-result.v1","outcome":"continue","summary":"user-readable result","questions":[],"approval_requests":[],"artifact_refs":[],"evidence_refs":[],"checkpoint":{"goal":"...","completed":[],"pending":[],"decisions":[],"constraints":[],"external_effects":[],"open_questions":[],"next_action":"..."},"payload":{"stage":"CURRENT_KERNEL_PHASE"}}
Use the Kernel's actual phase in payload.stage. Requirements and Design return needs_approval
with payload.document_markdown and payload.approval_question only after critical questions
are closed. Design also specifies risk_class, release_policy (manual by default),
exact publish_targets and runbook_actions; only the user can approve these definitions.
Build returns ready_for_validation with payload.report_markdown and actual test evidence.
Review returns ready_for_release or request_changes with payload.report_markdown and
the requirements/design/implementation/evidence alignment. Operation returns completed
with payload.report_markdown after the requested operating work and evidence are complete.
Any phase may ask complete questions; use 1-3 related questions with real options and
free text. Required Delegations must include delegation_results. Unknown facts and
unverified effects remain explicit. Do not self-report approval, grants or publication.
Return return_to_requirements or return_to_design when the approved content must change;
retain the useful existing work. The Kernel performs phase switching and publishing."""
            response_contract = response_contract.replace("CURRENT_KERNEL_PHASE", "audit" if single_agent_audit else current_stage)
        if single_agent_task:
            current_request = dict(run.get("request_json") or {})
            response_contract += "\n\n# Kernel authorization facts (not model declarations)\n" + json.dumps({
                "phase": "audit" if single_agent_audit else current_stage,
                "requirements_approval": current_request.get("requirements_approval"),
                "design_approval": current_request.get("design_approval"),
                "approved_actions": current_request.get("approved_actions") or {},
                "publication_state": current_request.get("publication_state"),
                "operational_baseline_ref": current_request.get("operational_baseline_ref"),
                "public_background": public_context,
            }, ensure_ascii=False, default=str)
        if single_agent_task and config.foundation_revision=='1.2':
            from assistant_kernel.services.contracts import contracts
            from assistant_kernel.services.pm_turns_v12 import REPORTS
            registry=contracts();report_definition=registry.document_definition(REPORTS[current_stage][1])
            producer_schema=registry.agent_result_schema(current_stage)
            response_contract=('# Required final response\nReturn one agent-result.v1 envelope with protocol_revision 1.2. '
                'Use the actual phase '+current_stage+'. Envelope fields are outcome, summary, questions, approval_requests, '
                'artifact_refs, evidence_refs, proposed_actions, delegation_results, checkpoint and payload. '
                'Checkpoint contains completed, pending, constraints, decisions, artifact_refs, external_effect_refs, '
                'unresolved_question_ids and next_action. Ref arrays contain resource-ref.v1 objects. '
                'PM payload contains stage, report_ref, report_payload, task_completion_proposed, missing_capabilities, '
                'observation_refs, monitor_policy_refs, notification_suggestions, improvement_candidates, proposal_refs, '
                'python_artifacts, schedule_handoff, observation_summary, runtime_handoffs and runtime_status_refs. '
                'Use null report_ref with a complete typed report_payload, or a single pinned report_ref; never competing versions. '
                'Use empty arrays/null for inapplicable optional contents. Never return legacy document_markdown, report_markdown, '
                'Kernel controls or approval claims. Self-checks are claims, not acceptance. '
                'The current typed phase report definition is '+report_definition+'.\n\n'+
                json.dumps(producer_schema,ensure_ascii=False))
        prompt = "\n\n".join(
            part for part in (
                SCHEDULING_NOTICE,
                "# Role\n" + config.role_text,
                "# Skills\n" + "\n\n".join(config.skill_texts) if config.skill_texts else "",
                "# Skill bindings\n" + json.dumps(
                    list(config.skill_bindings), ensure_ascii=False
                ) if config.skill_bindings else "",
                "# Durable context\n" + json.dumps(checkpoint, ensure_ascii=False) if checkpoint else "",
                "# Accepted Task documents\n" + json.dumps(
                    accepted_context, ensure_ascii=False, default=str
                ) if accepted_context else "",
                (
                    "# Semantic context manifest\n"
                    + json.dumps(
                        (plan.get("bindings") or {}).get("semantic_context"),
                        ensure_ascii=False, default=str,
                    )
                    if (plan.get("bindings") or {}).get("semantic_context") else ""
                ),
                (
                    ("# Audit mode\nCold, read-only release audit for the current Task. "
                    "The sealed candidate bundle and Validation receipts in Current run "
                    "are the only releasable inputs." if confirmed_business_draft is None else
                    "# Audit mode\nUse a cold read-only review of the final accepted requirements, all applicable fixed phase documents, actual deliverables and validation evidence. Research needs no invented code bundle or operation stage. "
                    "For semantic_validation without an executable candidate, Kernel has validated structure and references; your independent Review verifies the actual content. Do not demand an invented code/component test receipt for inline text. "
                    "Report each final acceptance criterion explicitly; additional checks cannot substitute for missing final criteria. A passed Review can return completed or ready_for_release for Kernel admission; this does not itself publish or activate anything.")
                    if single_agent_audit else ""
                ),
                "# Conversation goal\n" + json.dumps(
                    conversation_goal, ensure_ascii=False, default=str
                ) if conversation_goal and confirmed_business_draft is None else "",
                "# Prior conversation\n" + history_text if confirmed_business_draft is None else "",
                "# Current input\n" + json.dumps({
                    "message_id": source_message_id,
                    "message_type": str(current_input["message_type"]),
                    "content": prompt_message_content(current_input),
                }, ensure_ascii=False) if confirmed_business_draft is None else "",
                "# Causal boundary\nHandle only Current input in this Run. Messages that arrived later belong to other Runs and must not influence this response." if confirmed_business_draft is None else "# Causal boundary\nExecute only the exact confirmed business draft. Prior answers, older Task instructions and later inputs are evidence, never an expansion of this Turn's authority.",
                "# Current run\n" + json.dumps(
                    {
                        "run_id": str(run["run_id"]), "run_type": str(run["run_type"]),
                        "stage": run.get("stage"), "request": ({key:value for key,value in (run['request_json'] or {}).items()
                            if key in {'protocol_revision','task_protocol','phase_protocol','work_mode','current_stage','task_turn_count',
                                'accepted_documents','state_revision','supervisor_context_epoch','requirements_approval','design_approval',
                                'validation_policy','validation_artifact','sealed_candidate_bundle','publication_state','rework_class',
                                'approved_actions','prompt_confirmation_ref','task_activation'}} if confirmed_business_draft is not None else run['request_json'] or {}),
                    }, ensure_ascii=False,
                ),
                "# Stage handoff\n" + json.dumps(handoff, ensure_ascii=False) if handoff else "",
                "# Execution plan\n" + json.dumps({
                    "uri": plan_uri,
                    "sha256": plan_file_sha256,
                    "session_compatibility_hash": compatibility_hash,
                }, ensure_ascii=False),
                "# MCP bindings\n" + self.plans.mcp_prompt_summary(
                    list((plan.get("bindings") or {}).get("mcp_servers") or [])
                ) if (plan.get("bindings") or {}).get("mcp_servers") else "",
                "# Pending user inputs (data)\n" + json.dumps([{ "message_id": str(m["message_id"]), "channel": m["channel"], "chat_id": m["chat_id"], "content": m["content_json"]} for m in turn_inputs], ensure_ascii=False) if turn_inputs and confirmed_business_draft is None else "",
                "# Current answer\n" + json.dumps(answer, ensure_ascii=False) if answer is not None and confirmed_business_draft is None else "",
                ("# Project file agreement\nYour first tool call must be get_authorized_workspace. The full-access CLI follows the shared Project directory agreement; the list is not OS isolation. Use actual loaded tools and native file/execution capabilities within the current goal. Legacy limited roots remain limited. Formal changes, external effects and control actions retain their separate approval gates."
                 if project_id != 'system' else
                 "# Workspace scope\nYour first tool call must be get_authorized_workspace. Use only the returned workspace URIs and scoped file tools. Do not discover files through an unrestricted shell."),
                response_contract,
            ) if part
        )
        if config.foundation_revision == '1.2':
            from assistant_kernel.services.invocation_context import write_invocation_context
            context_bindings = plan['bindings']
            context_ref = write_invocation_context(self.settings, project_id=project_id,
                run_id=str(run['run_id']), invocation_id=invocation_id,
                config_ref=resource_ref(kind='source', identity='agent-config:' + config.config_sha256,
                    uri=snapshot_uri, sha256=config_bundle_hash, purpose='actually loaded fixed Role/Skill/profile'),
                resolved_config_ref=resolved_config_ref,
                scope_ref=resource_ref(kind='source', identity='file-agreement:' + invocation_id,
                    uri=context_bindings['workspace_scope_snapshot_uri'],
                    sha256=context_bindings['workspace_scope_snapshot_sha256'], purpose='current Project directory agreement'),
                source_message_ids=[source_message_id, *[str(m['message_id']) for m in turn_inputs]],
                accepted_documents=dict((run.get('request_json') or {}).get('accepted_documents') or {}),
                skill_refs=[b['published_ref'] for b in config.skill_bindings if b.get('published_ref')],
                public_context=public_context, semantic_context=context_bindings.get('semantic_context'))
            executor_patch['context_manifest_ref'] = context_ref
            prompt += '\n\n# Actual context manifest\n' + json.dumps(context_ref, ensure_ascii=False)
        if confirmed_business_draft is not None:
            prompt += '\n\n# Confirmed business Prompt (the only executable business scope)\n'+json.dumps(confirmed_business_draft,ensure_ascii=False)
        prompt_bundle_uri, prompt_bundle_sha256 = self.plans.write_prompt_bundle(
            project_id, str(run["run_id"]), invocation_id, prompt,
        )
        prompt_history = list(requested_executor.get("prompt_history") or [])
        if not any(
            str(item.get("invocation_id") or "") == invocation_id
            for item in prompt_history if isinstance(item, dict)
        ):
            prompt_history.append({
                "invocation_id": invocation_id,
                "uri": prompt_bundle_uri,
                "sha256": prompt_bundle_sha256,
            })
        executor_patch.update({
            "prompt_bundle_uri": prompt_bundle_uri,
            "prompt_bundle_sha256": prompt_bundle_sha256,
            "prompt_history": prompt_history,
        })
        if not await self.repository.bind_run_execution(
            str(run["run_id"]), worker_instance_id, claim_token,
            str(session["session_id"]), source_message_id, executor_patch,
        ):
            raise asyncio.CancelledError("run lease was lost before executor launch")
        actor_role = (
            "project_agent" if single_agent_task else str(config.kind or "agent")
        )
        await self.repository.start_model_invocation(
            invocation_id=invocation_id, project_id=project_id,
            run_id=str(run["run_id"]), session_id=str(session["session_id"]),
            actor_role=actor_role,
            harness=str(getattr(self.harness, "name", "unknown")),
            provider=str(provider), model=str(model),
            reasoning_effort=reasoning_effort, auth_mode=auth_mode,
            metadata={
                "run_type": str(run.get("run_type") or ""),
                "stage": str(run.get("stage") or ""),
                "task_stage": current_stage if single_agent_task else None,
                "confirmed_package_sha256": (request_data.get('confirmed_turn_package_ref') or {}).get('sha256'),
                "native_continuation_requested": bool(session.get('native_session_id')),
            },
        )
        async def process_started(pid: int, start_ticks: int | None) -> None:
            if not await self.repository.record_process_identity(
                str(run["run_id"]), worker_instance_id, claim_token, pid, start_ticks
            ):
                raise asyncio.CancelledError("run lease was lost as the executor started")

        scope_context = {
            "ASSISTANT_DATA_ROOT": str(self.settings.data_root),
            "POSTGRES_DB": self.settings.postgres_database,
            "KERNEL_SCOPE_PROJECT_ID": project_id,
            "KERNEL_SCOPE_RUN_ID": str(run["run_id"]),
            "KERNEL_SCOPE_CLAIM_TOKEN": claim_token,
            "KERNEL_SCOPE_WORKER_INSTANCE_ID": worker_instance_id,
            "KERNEL_SCOPE_ALLOWED_TOOLS": ",".join(config.allowed_tools),
            "KERNEL_SCOPE_STAGE": str(run.get("stage") or ""),
            "KERNEL_SCOPE_SNAPSHOT_URI": str(
                (plan.get("bindings") or {}).get("workspace_scope_snapshot_uri") or ""
            ),
            "KERNEL_SCOPE_SNAPSHOT_SHA256": str(
                (plan.get("bindings") or {}).get("workspace_scope_snapshot_sha256") or ""
            ),
            "KERNEL_SCOPE_EFFECTIVE_PERMISSIONS_JSON": json.dumps(
                (plan.get("bindings") or {}).get("stage_permissions") or {},
                ensure_ascii=False, sort_keys=True,
            ),
            "KERNEL_SCOPE_CONTEXT_VIEW_JSON": json.dumps(
                (plan.get("bindings") or {}).get("context_view") or {},
                ensure_ascii=False, sort_keys=True,
            ),
        }
        try:
            project_credential_environment = (
                {} if project_id == "system" else self.credentials.environment(project_id)
            )
            shared_model_environment = SharedVolcengineConfig.load(
                self.settings.shared_volcengine_path
            ).environment_for_provider(str(provider) if provider else None)
            credential_environment = {
                **shared_model_environment,
                **project_credential_environment,
            }
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            blocked = await self.repository.finalize_agent_blocked(
                run_id=str(run["run_id"]), worker_instance_id=worker_instance_id,
                claim_token=claim_token,
                summary=f"Project凭据文件无效，未启动Codex：{exc}",
                artifact_refs=[],
            )
            return {
                "outcome": "blocked", "status": "blocked",
                "reply_message_id": str(blocked["reply"]["message_id"]),
            }
        scope_bindings = plan.get("bindings") or {}
        scope_snapshot_uri = str(scope_bindings.get("workspace_scope_snapshot_uri") or "")
        scope_snapshot_sha256 = str(
            scope_bindings.get("workspace_scope_snapshot_sha256") or ""
        )
        pinned_roots = (
            self.plans.workspace_scopes.load_snapshot(
                project_id, str(run["run_id"]), str(run.get("stage") or "") or None,
                scope_snapshot_uri, scope_snapshot_sha256,
            )
            if scope_snapshot_uri and scope_snapshot_sha256 else []
        )
        writable_roots = (() if permission_profile else tuple(
            item.path for item in pinned_roots
            if item.access == "write" and item.key != "candidate"
        ))
        effects = FileEffectRecorder(
            self.runtime.ensure(project_id).vault_root, str(run["run_id"]),
            invocation_id, list(pinned_roots),
            protected_backup_root=(
                self.settings.data_root / "backups" / "run-file-effects" /
                project_id / str(run["run_id"]) / invocation_id
            ),
        )
        try:
            effects.capture_before()
        except (ValueError, PermissionError) as exc:
            blocked = await self.repository.finalize_agent_blocked(
                run_id=str(run["run_id"]), worker_instance_id=worker_instance_id,
                claim_token=claim_token,
                summary=f"文件授权预检未通过，未启动Codex：{exc}",
                artifact_refs=[],
            )
            return {
                "outcome": "blocked", "status": "blocked",
                "reply_message_id": str(blocked["reply"]["message_id"]),
            }
        file_effects = None
        try:
            if config.foundation_revision == '1.2':
                from assistant_kernel.services.mcp_runtime import declared_mcp as discover_mcp
                import sys
                environment = {**os.environ, **credential_environment, **scope_context,
                    'ASSISTANT_DATA_ROOT': str(self.settings.data_root),
                    'POSTGRES_DB': self.settings.postgres_database}
                discoveries = []
                if config.allowed_tools:
                    discoveries.append(await discover_mcp({
                        'server_id': 'kernel_scoped', 'transport': 'stdio', 'command': sys.executable,
                        'args': ['-m', 'assistant_kernel.mcp_server.scoped_tools'],
                        'enabled_tools': list(config.allowed_tools), 'required': True,
                        'startup_timeout_sec': 30,
                    }, environment=environment, cancel_event=cancel_event))
                for binding in project_mcp_servers:
                    discoveries.append(await discover_mcp(binding,
                        environment={**os.environ, **credential_environment}, cancel_event=cancel_event))
                current = await self.repository.get_run(str(run['run_id']))
                if (not current or current['status'] != 'running' or str(current['claim_token']) != str(claim_token)
                        or current.get('cancel_requested_at') is not None or not current.get('lease_until')
                        or current['lease_until'] <= datetime.now(timezone.utc)):
                    raise asyncio.CancelledError('Run claim changed during capability discovery')
                if confirmed_capability is not None:
                    from assistant_kernel.services.prompt_protocol import PromptProtocol
                    PromptProtocol(self.settings,self.repository).recheck_capabilities(project_id,confirmed_capability,
                        model_profile=profile_data,mcp_discovery=discoveries,
                        authority_ref=resource_ref(kind='source',identity='actual-file-authority',uri=scope_snapshot_uri,sha256=scope_snapshot_sha256,purpose='actual invocation file authority'))
                assembly = {'schema_version': 'agent-assembly.v1', 'invocation_id': invocation_id,
                    'config_sha256': config.config_sha256, 'skill_bindings': list(config.skill_bindings),
                    'mcp_discovery': discoveries, 'resolved_config_snapshot_ref': resolved_config_ref,
                    'workspace_scope_ref': {'uri': scope_snapshot_uri, 'sha256': scope_snapshot_sha256},
                    'plan_ref': {'uri': plan_uri, 'sha256': plan_file_sha256}, 'role_budget': role_budget}
                path = self.runtime.ensure(project_id).vault_root / 'runtime/invocation-assemblies' / (invocation_id + '.json')
                path.parent.mkdir(parents=True, exist_ok=True)
                encoded = json.dumps(assembly, ensure_ascii=False, sort_keys=True).encode('utf-8')
                if path.exists() and path.read_bytes() != encoded:
                    raise ValueError('Invocation assembly receipt is immutable')
                path.write_bytes(encoded)
            result = await self.harness.run_turn(AgentTurnRequest(
                project_id=project_id, prompt=prompt,
                cwd=self.runtime.ensure(project_id).vault_root,
                physical_session_id=(
                    str(session["native_session_id"]) if session["native_session_id"] else None
                ),
                provider=str(provider) if provider else None, model=str(model) if model else None,
                reasoning_effort=reasoning_effort, sandbox=effective_sandbox,
                permission_profile=permission_profile,
                permission_profile_sha256=permission_profile_sha256,
                context_policy=context_policy,
                model_context_window_tokens=model_context_window_tokens,
                model_auto_compact_token_limit=model_auto_compact_token_limit,
                timeout_seconds=timeout_seconds, invocation_id=invocation_id,
                cancel_event=cancel_event, scoped_tools=config.allowed_tools,
                scope_context=scope_context, extra_environment=credential_environment,
                writable_roots=writable_roots,
                mcp_servers=tuple(project_mcp_servers),
                mcp_expected=tuple((confirmed_capability or {}).get('mcp_discovery') or discoveries if config.foundation_revision=='1.2' else ()),
                multi_agent_enabled=multi_agent_enabled,
                max_concurrent_subagents=max_concurrent_subagents,
                process_started_callback=process_started,
            ))
            if invocation_configuration is not None:
                await invocation_configuration.record_use(snapshot_ref=resolved_config_ref,receipt_payload={
                    'invocation_id':invocation_id,'native_session_id':result.physical_session_id,
                    'provider':provider,'model':model,'reasoning_effort':reasoning_effort,
                    'timeout_seconds':timeout_seconds,'context_policy':context_policy,
                    'max_native_subagents':max_concurrent_subagents,'role_budget':role_budget,
                    'model_catalog_cache_seconds':invocation_values['model_catalog_cache_seconds']})
        except asyncio.CancelledError:
            await asyncio.shield(self.repository.finish_model_invocation(
                invocation_id=invocation_id, status="cancelled",
                error_type="CancelledError", error_message="model invocation cancelled",
            ))
            raise
        except Exception as exc:
            await self.repository.finish_model_invocation(
                invocation_id=invocation_id, status="failed",
                error_type=type(exc).__name__, error_message=str(exc),
            )
            raise
        finally:
            file_effects = effects.capture_after()
            await self._record_turn_file_changes(
                project_id=project_id, run=run, invocation_id=invocation_id,
                trigger=trigger, roots=list(pinned_roots or current_scope_roots), file_effects=file_effects,
            )
        context_window = int(
            result.metadata.get("context_window_tokens")
            or effective_context_window_tokens
            or requested_executor.get("context_window_tokens")
            or self.settings.default_context_window_tokens
        )
        decision = ContextBudgetManager(
            self.settings.context_soft_ratio, self.settings.context_hard_ratio
        ).evaluate(result.usage, context_window)
        usage_snapshot = self._usage_snapshot(
            invocation_id, result, context_window, decision.ratio
        )
        usage_snapshot["pm_evidence"] = summarize_pm_tool_evidence(
            invocation_id=invocation_id,
            events=result.events,
            trace_uri=result.archive_uri,
            trace_sha256=result.archive_sha256,
            usage=usage_snapshot,
        )
        usage_snapshot["native_delegations"] = summarize_native_delegations(
            result.events
        )
        if file_effects:
            usage_snapshot["file_effects"] = file_effects
        if not await self.repository.record_turn_usage(
            run_id=str(run["run_id"]), worker_instance_id=worker_instance_id,
            claim_token=claim_token, session_id=str(session["session_id"]),
            native_session_id=result.physical_session_id,
            input_tokens=result.usage.input_tokens,
            context_window_tokens=context_window,
            archive_uri=result.archive_uri, archive_sha256=result.archive_sha256,
            usage_snapshot=usage_snapshot,
        ):
            raise asyncio.CancelledError("run lease was lost while recording turn usage")
        await self.repository.finish_model_invocation(
            invocation_id=invocation_id, status="succeeded",
            native_session_id=result.physical_session_id,
            input_tokens=result.usage.input_tokens,
            cached_input_tokens=result.usage.cached_input_tokens,
            output_tokens=result.usage.output_tokens,
            reasoning_tokens=result.usage.reasoning_output_tokens,
            trace_uri=result.archive_uri, trace_sha256=result.archive_sha256,
            metadata={"context_window_tokens": context_window},
        )
        if "get_authorized_workspace" in config.allowed_tools:
            if not _has_successful_mcp_tool_call(
                result.events, "get_authorized_workspace"
            ):
                raise RuntimeError("required workspace scope handshake did not complete")
        envelope = parse_agent_envelope(
            result.final_text,
            require_object=(run["run_type"] == "stage" or single_agent_task),
            phase="audit" if single_agent_audit else current_stage,
            expected_revision='1.2' if single_agent_task and config.foundation_revision=='1.2' else None,
        )
        if single_agent_task and config.foundation_revision=='1.2':
            from assistant_kernel.services.pm_turns_v12 import PMTurnsV12
            return await PMTurnsV12(self.settings,self.repository).settle(run=run,worker_instance_id=worker_instance_id,
                public_result=envelope['public_result'],usage_snapshot=usage_snapshot,file_effects=file_effects,cancel_event=cancel_event)
        late_turn_inputs = single_agent_task and await self.repository.has_late_turn_inputs(str(run["run_id"]))
        if late_turn_inputs and envelope.get("outcome") in {"completed", "needs_approval", "ready_for_validation", "ready_for_release", "return_to_requirements"}:
            envelope = {**envelope, "outcome": "continue", "summary": str(envelope.get("summary") or "") + "\n有新增补充，下一轮先处理。"}
        if file_effects:
            envelope["artifacts"] = [
                *list(envelope.get("artifacts") or []),
                {"uri": file_effects["uri"], "kind": "file_effects", "required": False},
                ]
        if single_agent_task:
            settled_accepted, native_result_refs = self._settle_native_delegations(
                run=run, project_id=project_id, envelope=envelope,
                usage_snapshot=usage_snapshot,
            )
            if settled_accepted != dict(
                (run.get("request_json") or {}).get("accepted_documents") or {}
            ):
                run = {**run, "request_json": {
                    **dict(run.get("request_json") or {}),
                    "accepted_documents": settled_accepted,
                }}
            envelope["delegations"] = native_result_refs
        if single_agent_task:
            allowed_task_outcomes = (
                ({"ready_for_release", "request_changes", "blocked", "needs_input", "return_to_requirements", "return_to_design", "failed"} | ({"continue"} if late_turn_inputs else set()))
                if single_agent_audit else
                {"continue", "needs_input", "needs_approval", "return_to_requirements", "return_to_design",
                 "ready_for_validation", "blocked", "failed"} | (
                     {"completed"} if current_stage == "operate" else set()
                 )
            )
            if envelope["outcome"] not in allowed_task_outcomes:
                raise ValueError(
                    f"single-agent Task cannot return {envelope['outcome']}"
                )
            request_state = dict(run.get("request_json") or {})
            current_stage = str(request_state.get("current_stage") or "requirements")
            if envelope["outcome"] == "needs_approval" and current_stage not in {"requirements", "design", "operate"}:
                raise ValueError("Content approval is only allowed in Requirements, Design or a registered Operation")
            if envelope["outcome"] == "return_to_requirements":
                if current_stage not in {"design", "build", "audit", "operate"}:
                    raise ValueError("This phase cannot return to Requirements")
                if not str(envelope.get("document_markdown") or "").strip():
                    raise ValueError("return_to_requirements needs document_markdown")
            if envelope["outcome"] == "ready_for_validation":
                contract_ref = dict(
                    (request_state.get("accepted_documents") or {}).get(
                        "task_contract"
                    ) or {}
                )
                contract = (
                    self.task_documents.read(project_id, contract_ref)
                    if contract_ref else None
                )
                if (
                    current_stage != "build"
                    or not request_state.get("requirements_approval")
                    or contract is None
                ):
                    raise ValueError(
                        "Build validation requires an approved Task Contract"
                    )
                if request_state.get("design_required"):
                    design_ref = dict((request_state.get("accepted_documents") or {}).get("solution_design") or {})
                    if (request_state.get("design_approval") or {}).get("approved_document_sha256") != design_ref.get("sha256") or not design_ref:
                        raise ValueError("Implementation requires the current user-approved Design")
            if envelope["outcome"] == "return_to_design" and not request_state.get("requirements_approval"):
                raise ValueError("Return to Design requires approved requirements")
            turn_number = int(request_state.get("task_turn_count") or 0) + 1
            max_turns = int(request_state.get("max_task_turns") or 12)
            if turn_number > max_turns:
                raise RuntimeError("single-agent Task exceeded its bounded Turn count")
            # v6.1: the Supervisor evaluation is a queued consultation Run,
            # no longer an inline model call inside this Turn's lease.
            # _queue_turn_evaluation() is invoked before every return below.
            phase = {
                "continue": "working", "needs_input": "waiting_user",
                "needs_approval": "waiting_requirements",
                "return_to_requirements": "working",
                "ready_for_validation": "validating", "blocked": "blocked",
                "failed": "failed", "ready_for_release": "waiting_release",
                "request_changes": "working",
                "return_to_design": "working", "completed": "working",
            }[envelope["outcome"]]
            task_request_patch = {
                "task_turn_count": turn_number,
                "task_phase": phase,
                "execution_plan_variant": "audit" if single_agent_audit else current_stage,
            }
            accepted_documents, turn_record_ref, state_revision = (
                self._write_turn_documents(
                    run=run, project_id=project_id, trigger=trigger,
                    turn_number=turn_number, envelope=envelope,
                    usage_snapshot=usage_snapshot,
                    artifacts=list(envelope.get("artifacts") or []),
                )
            )
            task_request_patch.update({
                "state_revision": state_revision,
                "accepted_documents": accepted_documents,
            })
            report_ref = self._write_phase_report(
                run=run, project_id=project_id, phase="audit" if single_agent_audit else current_stage,
                envelope=envelope, source_refs=[ref for ref in (
                    accepted_documents.get("task_contract"), accepted_documents.get("solution_design"),
                ) if ref],
            ) if request_state.get("design_required") else None
            if report_ref:
                report_key = {"build": "implementation_report", "audit": "review_report", "operate": "operation_report"}["audit" if single_agent_audit else current_stage]
                accepted_documents[report_key] = report_ref
                envelope["artifacts"] = [*list(envelope.get("artifacts") or []), {**report_ref, "kind": report_key, "required": True}]
                task_request_patch["accepted_documents"] = accepted_documents
                proposals = (envelope.get("payload") or {}).get("improvement_proposals")
                if single_agent_audit and proposals:
                    if not isinstance(proposals, list):
                        raise ValueError("Review improvement_proposals must be a list")
                    improvement_ref = self.task_documents.write_revision(
                        project_id=project_id, task_id=str(run["run_id"]),
                        document_kind="review-improvement-proposal", document_schema="review-improvement-proposal.v1",
                        payload={"proposals": proposals, "effective_for_current_acceptance": False},
                        status="proposed", actor_type="pm", actor_id=str(run["agent_key"]),
                        run_id=str(run["run_id"]), reason="Separate future rule proposals from current acceptance",
                        source_refs=[report_ref],
                    )
                    accepted_documents["review_improvement_proposal"] = improvement_ref
                    envelope["artifacts"].append({**improvement_ref, "kind": "review_improvement_proposal"})
            if envelope["outcome"] in {"return_to_requirements", "return_to_design"} or (
                envelope["outcome"] == "needs_approval" and current_stage == "design"
            ):
                task_request_patch.update({
                    "audit_complete": False, "audit_mode": False, "pending_auto_publish": None,
                    "pending_validation_supervisor_turn": None, "validation_supervisor_approved_turn": None,
                    "validation_artifact": None, "validation_design_sha256": None,
                    "sealed_candidate_bundle": None, "reviewed_bundle_sha256": None, "reviewed_design_sha256": None,
                })
            if envelope["outcome"] == "needs_approval" and current_stage == "design":
                design_ref = self._write_solution_design_revision(
                    run=run, project_id=project_id, envelope=envelope,
                    prior_ref=accepted_documents.get("solution_design"),
                )
                accepted_documents["solution_design"] = design_ref
                task_request_patch.update({
                    "accepted_documents": accepted_documents, "solution_design_ref": design_ref,
                    "solution_design_sha256": design_ref["sha256"], "design_approval": None,
                    "approval_kind": "design", "task_phase": "waiting_design",
                })
                envelope["artifacts"] = [*list(envelope.get("artifacts") or []), {**design_ref, "kind": "solution_design", "required": True}]
            elif envelope["outcome"] == "needs_approval" and current_stage == "operate":
                from assistant_kernel.services.operations import ProjectOperations
                approvals = list(envelope.get("approval_requests") or [])
                if len(approvals) != 1 or not isinstance(approvals[0], dict):
                    raise ValueError("Operation requires one concrete registered action approval")
                prepared = await ProjectOperations(self.settings).prepare_approval(
                    project_id, str(run["run_id"]), str(approvals[0].get("action_id") or ""),
                    dict(approvals[0].get("inputs") or {}),
                )
                task_request_patch.update({"approval_kind": "operation", "operation_approval_request": prepared})
                envelope["document_markdown"] = prepared["document_markdown"]
                envelope["approval_question"] = "是否批准这项具体运营动作及输入？"
            elif envelope["outcome"] == "needs_approval":
                requirements_document = str(envelope.get("document_markdown") or "").strip()
                if not requirements_document:
                    raise ValueError("Requirements approval needs document_markdown")
                contract_ref = self._write_task_contract_revision(
                    run=run, project_id=project_id,
                    document_markdown=requirements_document,
                    status="ready",
                    prior_ref=accepted_documents.get("task_contract"),
                    structured=dict(envelope.get("payload") or {}),
                )
                accepted_documents["task_contract"] = contract_ref
                task_request_patch["accepted_documents"] = accepted_documents
                task_request_patch["task_contract_ref"] = contract_ref
                task_request_patch["task_contract_sha256"] = contract_ref["sha256"]
                requirements_artifact = {
                    **contract_ref, "kind": "task_contract", "required": True,
                    "producer_run_id": str(run["run_id"]),
                    "producer_stage": "requirements",
                }
                envelope["artifacts"] = [
                    *list(envelope.get("artifacts") or []), requirements_artifact,
                ]
                task_request_patch.update({
                    "approval_kind": "requirements",
                    "requirements_sha256": contract_ref["sha256"],
                })
            elif envelope["outcome"] == "return_to_requirements":
                requirements_document = str(envelope["document_markdown"]).strip()
                if not request_state.get("requirements_approval"):
                    raise ValueError(
                        "return_to_requirements requires an approved Task Contract"
                    )
                task_request_patch.update({
                    "current_stage": "requirements",
                    "work_mode": "requirements",
                    "task_phase": "working",
                    "execution_plan_variant": "requirements",
                    "approval_kind": None,
                    "requirements_approval": None,
                    "design_approval": None,
                    "requirements_return_reason": str(envelope.get("summary") or ""),
                    "force_session_rotation": {"reason": "requirements_revision"},
                })
                contract_ref = self._write_task_contract_revision(
                    run=run, project_id=project_id,
                    document_markdown=requirements_document,
                    status="draft",
                    prior_ref=accepted_documents.get("task_contract"),
                    structured=dict(envelope.get("payload") or {}),
                )
                accepted_documents["task_contract"] = contract_ref
                task_request_patch["accepted_documents"] = accepted_documents
                requirements_artifact = {
                    **contract_ref, "kind": "task_contract", "required": True,
                    "producer_run_id": str(run["run_id"]),
                    "producer_stage": current_stage,
                }
                envelope["artifacts"] = [
                    *list(envelope.get("artifacts") or []), requirements_artifact,
                ]
                task_request_patch["requirements_sha256"] = contract_ref["sha256"]
            elif envelope["outcome"] == "return_to_design":
                task_request_patch.update({
                    "current_stage": "design", "work_mode": "design", "task_phase": "working",
                    "execution_plan_variant": "design", "design_approval": None, "audit_mode": False,
                    "approval_kind": None, "force_session_rotation": {"reason": "design_revision"},
                })
            accepted_row = await self.repository.patch_claimed_run_request(
                run_id=str(run["run_id"]), worker_instance_id=worker_instance_id,
                claim_token=claim_token, request_patch=task_request_patch,
            )
            if report_ref:
                report = self.task_documents.read(project_id, report_ref)["payload"]
                await self.repository.create_outbound(
                    project_id=project_id, session_id=str(session["session_id"]), run_id=str(run["run_id"]),
                    channel=str(trigger["channel"]), chat_id=str(trigger["chat_id"]), user_id=trigger.get("user_id"),
                    message_type="phase_report", content={
                        "text": f"{report_ref['document_schema']} · {envelope.get('summary') or '本轮报告'}",
                        "report_markdown": str(report.get("report_markdown") or ""),
                        "report_ref": report_ref, "origin": "kernel", "context_visibility": "ui_only",
                    }, reply_to_message_id=source_message_id,
                    idempotency_key=f"phase-report:{run['run_id']}:{turn_number}:{report_ref['sha256']}",
                )
            commit_id = f"task-state:{run['run_id']}:{state_revision}"
            self.task_documents.update_current(
                project_id=project_id, task_id=str(run["run_id"]),
                document_kind="turn-record", ref=turn_record_ref,
                commit_id=commit_id, state_revision=state_revision,
            )
            self.task_documents.update_current(
                project_id=project_id, task_id=str(run["run_id"]),
                document_kind="task-contract",
                ref=accepted_documents["task_contract"],
                commit_id=commit_id, state_revision=state_revision,
            )
            state_snapshot_ref = self.task_documents.write_revision(
                project_id=project_id, task_id=str(run["run_id"]),
                document_kind="task-state-snapshot",
                document_schema="task-state-snapshot.v1",
                payload={
                    "state_revision": state_revision,
                    "work_mode": str(task_request_patch.get("current_stage") or current_stage),
                    "lifecycle": phase,
                    "run_status": str(accepted_row.get("status") or "running"),
                    "context_epoch": int(
                        request_state.get("supervisor_context_epoch") or 0
                    ),
                    "approved_task_contract_ref": (
                        accepted_documents.get("task_contract")
                        if task_request_patch.get("requirements_approval") else None
                    ),
                    "last_turn_ref": turn_record_ref,
                    "current_action_plan_ref": accepted_documents.get(
                        "current_action_plan"
                    ),
                    "active_delegations": list(
                        accepted_documents.get("active_delegations") or []
                    ),
                    "candidate_ref": accepted_documents.get("candidate"),
                    "validation_ref": accepted_documents.get("validation"),
                    "publication_ref": accepted_documents.get("publication"),
                    "open_questions": list(
                        (envelope.get("task_checkpoint") or {}).get("open_questions") or []
                    ),
                    "next_action": str(
                        (envelope.get("task_checkpoint") or {}).get("next_action") or ""
                    ),
                    "degraded_supervision": bool(
                        request_state.get("degraded_supervision")
                    ),
                    "generated_from_run": str(run["run_id"]),
                },
                status="current", actor_type="kernel", actor_id="kernel",
                run_id=str(run["run_id"]), reason="project accepted Task state",
                source_refs=[turn_record_ref, accepted_documents["task_contract"]],
            )
            self.task_documents.update_current(
                project_id=project_id, task_id=str(run["run_id"]),
                document_kind="task-state-snapshot", ref=state_snapshot_ref,
                commit_id=commit_id, state_revision=state_revision,
            )
            await self._record_document_commit(
                project_id=project_id, run=run, revision=state_revision,
                refs={**accepted_documents, "task_state": state_snapshot_ref},
                previous=dict(request_state.get("accepted_documents") or {}),
                roots=list(current_scope_roots), source_refs=[turn_record_ref],
            )
            run = {**run, "request_json": {
                **request_state, **task_request_patch,
            }}
            if single_agent_audit and envelope["outcome"] == "request_changes":
                retry_patch = {
                    **task_request_patch,
                    "task_phase": "working", "audit_mode": False,
                    "execution_plan_variant": "build",
                    "audit_feedback": list(envelope.get("issues") or []),
                    "audit_complete": False,
                }
                artifacts = self._normalize_artifacts(
                    project_id, list(envelope.get("artifacts") or [])
                )
                continued = await self.repository.finalize_agent_continue(
                    run_id=str(run["run_id"]), worker_instance_id=worker_instance_id,
                    claim_token=claim_token, project_id=project_id,
                    channel=str(trigger["channel"]), chat_id=str(trigger["chat_id"]),
                    user_id=trigger["user_id"], source_message_id=source_message_id,
                    summary=str(envelope.get("summary") or "Audit requested changes"),
                    request_patch=retry_patch, artifact_refs=artifacts,
                    usage_snapshot=usage_snapshot, reset_execution_plan=True,
                    wait_for_supervisor=True,
                )
                await self._queue_turn_evaluation(
                    project_id=project_id, task_id=str(run["run_id"]),
                    turn_number=turn_number, trigger=trigger,
                    reply_message_id=str(continued["reply"]["message_id"]),
                    outcome="request_changes",
                    summary=str(envelope.get("summary") or ""),
                    usage=usage_snapshot, artifacts=artifacts,
                )
                return {
                    "outcome": "continue", "status": "queued",
                    "run_id": str(continued["run"]["run_id"]),
                    "audit_feedback": retry_patch["audit_feedback"],
                    "token_usage": usage_snapshot,
                }
            if single_agent_audit and envelope["outcome"] == "ready_for_release":
                inherited_artifacts = [
                    *list(run.get("artifact_refs") or []),
                    *list(envelope.get("artifacts") or []),
                ]
                deduplicated: dict[str, Any] = {}
                for item in inherited_artifacts:
                    key = str(item.get("uri") if isinstance(item, dict) else item)
                    if key:
                        deduplicated[key] = item
                envelope["artifacts"] = list(deduplicated.values())
                release_patch = {
                    **task_request_patch,
                    "task_phase": "waiting_release", "audit_mode": False,
                    "audit_complete": True, "approval_kind": "release",
                    "execution_plan_variant": "audit",
                    "reviewed_bundle_sha256": (request_state.get("sealed_candidate_bundle") or {}).get("bundle_sha256"),
                    "reviewed_design_sha256": (request_state.get("design_approval") or {}).get("approved_document_sha256"),
                    "review_requires_final_approval": bool(
                        (envelope.get("payload") or {}).get("requires_final_approval")
                        or (envelope.get("payload") or {}).get("risk_class") == "high"
                    ),
                }
                await self.repository.patch_claimed_run_request(
                    run_id=str(run["run_id"]), worker_instance_id=worker_instance_id,
                    claim_token=claim_token, request_patch=release_patch,
                )
                run = {**run, "request_json": {
                    **dict(run.get("request_json") or {}), **release_patch,
                }}
                if self._automatic_low_risk_release(run):
                    envelope["outcome"] = "continue"
                    task_request_patch = {**release_patch, "pending_auto_publish": True,
                                          "approval_kind": None, "task_phase": "waiting_supervisor"}
                else:
                    envelope["outcome"] = "needs_approval"
                envelope["document_markdown"] = (
                    "# High-risk candidate release\n\n"
                    f"- Task: `{run['run_id']}`\n"
                    f"- Candidate bundle: `{request_state.get('sealed_candidate_bundle', {}).get('bundle_sha256', '')}`\n"
                    "- Kernel Validation: pass\n"
                    "- Cold audit: pass\n\n"
                    "批准只允许Publisher应用已密封candidate；不授权任何外部动作。"
                )
                if envelope["outcome"] == "needs_approval":
                    envelope["document_markdown"] = self._release_approval_markdown(
                        run, str((envelope.get("payload") or {}).get("report_markdown") or envelope.get("summary") or ""),
                    )
                envelope["approval_question"] = "是否批准发布这份Validation与冷审计均通过的candidate？"
            if envelope["outcome"] in {"continue", "return_to_design", "completed"}:
                if envelope["outcome"] == "completed":
                    task_request_patch["pending_task_completion"] = True
                submitted = self._add_candidate_manifest(
                    run, project_id, list(envelope.get("artifacts") or [])
                )
                artifacts = self._normalize_artifacts(project_id, submitted)
                continued = await self.repository.finalize_agent_continue(
                    run_id=str(run["run_id"]), worker_instance_id=worker_instance_id,
                    claim_token=claim_token, project_id=project_id,
                    channel=str(trigger["channel"]), chat_id=str(trigger["chat_id"]),
                    user_id=trigger["user_id"], source_message_id=source_message_id,
                    summary=str(envelope.get("summary") or "Task Turn completed; continuing."),
                    request_patch=task_request_patch, artifact_refs=artifacts,
                    usage_snapshot=usage_snapshot,
                    wait_for_supervisor=True,
                    reset_execution_plan=envelope["outcome"] == "return_to_design",
                )
                await self._queue_turn_evaluation(
                    project_id=project_id, task_id=str(run["run_id"]),
                    turn_number=turn_number, trigger=trigger,
                    reply_message_id=str(continued["reply"]["message_id"]),
                    outcome=envelope["outcome"],
                    summary=str(envelope.get("summary") or ""),
                    usage=usage_snapshot, artifacts=artifacts,
                )
                return {
                    "outcome": "continue", "status": str(continued["run"]["status"]),
                    "run_id": str(continued["run"]["run_id"]),
                    "reply_message_id": str(continued["reply"]["message_id"]),
                    "token_usage": usage_snapshot,
                }
            if envelope["outcome"] == "return_to_requirements":
                submitted = self._add_candidate_manifest(
                    run, project_id, list(envelope.get("artifacts") or [])
                )
                artifacts = self._normalize_artifacts(project_id, submitted)
                continued = await self.repository.finalize_agent_continue(
                    run_id=str(run["run_id"]), worker_instance_id=worker_instance_id,
                    claim_token=claim_token, project_id=project_id,
                    channel=str(trigger["channel"]), chat_id=str(trigger["chat_id"]),
                    user_id=trigger["user_id"], source_message_id=source_message_id,
                    summary=str(envelope.get("summary") or "Returning to Requirements."),
                    request_patch=task_request_patch, artifact_refs=artifacts,
                    usage_snapshot=usage_snapshot, reset_execution_plan=True,
                    wait_for_supervisor=True,
                )
                await self._queue_turn_evaluation(
                    project_id=project_id, task_id=str(run["run_id"]),
                    turn_number=turn_number, trigger=trigger,
                    reply_message_id=str(continued["reply"]["message_id"]),
                    outcome="return_to_requirements",
                    summary=str(envelope.get("summary") or ""),
                    usage=usage_snapshot, artifacts=artifacts,
                )
                return {
                    "outcome": "return_to_requirements", "status": "queued",
                    "run_id": str(continued["run"]["run_id"]),
                    "reply_message_id": str(continued["reply"]["message_id"]),
                    "requirements": requirements_artifact,
                    "token_usage": usage_snapshot,
                }
            if envelope["outcome"] == "ready_for_validation":
                pending_turn = int(
                    request_state.get("pending_validation_supervisor_turn") or 0
                )
                approved_turn = int(
                    request_state.get("validation_supervisor_approved_turn") or 0
                )
                if not pending_turn or approved_turn != pending_turn:
                    submitted = self._add_candidate_manifest(
                        run, project_id, list(envelope.get("artifacts") or [])
                    )
                    artifacts = self._normalize_artifacts(project_id, submitted)
                    barrier_patch = {
                        **task_request_patch,
                        "task_phase": "waiting_validation_supervisor",
                        "pending_validation_supervisor_turn": turn_number,
                        "validation_supervisor_approved_turn": None,
                    }
                    continued = await self.repository.finalize_agent_continue(
                        run_id=str(run["run_id"]), worker_instance_id=worker_instance_id,
                        claim_token=claim_token, project_id=project_id,
                        channel=str(trigger["channel"]), chat_id=str(trigger["chat_id"]),
                        user_id=trigger["user_id"], source_message_id=source_message_id,
                        summary=(
                            str(envelope.get("summary") or "Candidate ready")
                            + "\n等待Supervisor在Validation前评估。"
                        ),
                        request_patch=barrier_patch, artifact_refs=artifacts,
                        usage_snapshot=usage_snapshot, wait_for_supervisor=True,
                    )
                    await self._queue_turn_evaluation(
                        project_id=project_id, task_id=str(run["run_id"]),
                        turn_number=turn_number, trigger=trigger,
                        reply_message_id=str(continued["reply"]["message_id"]),
                        outcome="ready_for_validation",
                        summary=str(envelope.get("summary") or "candidate ready"),
                        usage=usage_snapshot, artifacts=artifacts,
                    )
                    return {
                        "outcome": "continue", "status": "waiting_supervisor",
                        "run_id": str(continued["run"]["run_id"]),
                        "task_phase": "waiting_validation_supervisor",
                        "token_usage": usage_snapshot,
                    }
                bundle = self.candidate_bundles.seal(
                    project_id, str(run["run_id"])
                )
                validation_artifact = self.validation.validate(
                    project_id, str(run["run_id"]),
                    validation_policy=dict(
                        request_state.get("validation_policy")
                        or {"required_scopes": ["structure", "component"]}
                    ),
                    sealed_bundle=bundle,
                )
                bundle_artifact = {
                    "uri": bundle["bundle_uri"],
                    "sha256": bundle["bundle_file_sha256"],
                    "size": bundle["bundle_size"],
                    "kind": "candidate_bundle", "required": True,
                    "bundle_sha256": bundle["bundle_sha256"],
                }
                envelope["artifacts"] = [
                    *self._add_candidate_manifest(
                        run, project_id, list(envelope.get("artifacts") or [])
                    ),
                    bundle_artifact,
                    *bundle["file_artifacts"],
                    validation_artifact,
                ]
                if validation_artifact.get("release_gate") != "pass":
                    retry_patch = {
                        **task_request_patch,
                        "task_phase": "working",
                        "pending_validation_supervisor_turn": None,
                        "validation_supervisor_approved_turn": None,
                        "validation_feedback": validation_artifact,
                        "sealed_candidate_bundle": bundle_artifact,
                    }
                    artifacts = self._normalize_artifacts(
                        project_id, list(envelope["artifacts"])
                    )
                    continued = await self.repository.finalize_agent_continue(
                        run_id=str(run["run_id"]), worker_instance_id=worker_instance_id,
                        claim_token=claim_token, project_id=project_id,
                        channel=str(trigger["channel"]), chat_id=str(trigger["chat_id"]),
                        user_id=trigger["user_id"], source_message_id=source_message_id,
                        summary=str(validation_artifact.get("summary") or "Validation failed"),
                        request_patch=retry_patch, artifact_refs=artifacts,
                        usage_snapshot=usage_snapshot,
                        wait_for_supervisor=True,
                    )
                    await self._queue_turn_evaluation(
                        project_id=project_id, task_id=str(run["run_id"]),
                        turn_number=turn_number, trigger=trigger,
                        reply_message_id=str(continued["reply"]["message_id"]),
                        outcome="ready_for_validation",
                        summary=str(validation_artifact.get("summary") or ""),
                        usage=usage_snapshot, artifacts=artifacts,
                    )
                    return {
                        "outcome": "continue", "status": "queued",
                        "run_id": str(continued["run"]["run_id"]),
                        "validation": validation_artifact,
                        "token_usage": usage_snapshot,
                    }
                if bool(request_state.get("audit_required")):
                    audit_patch = {
                        **task_request_patch,
                        "task_phase": "audit", "audit_mode": True,
                        "pending_validation_supervisor_turn": None,
                        "validation_supervisor_approved_turn": None,
                        "execution_plan_variant": "audit",
                        "sealed_candidate_bundle": bundle_artifact,
                        "validation_artifact": validation_artifact,
                        "validation_design_sha256": (request_state.get("design_approval") or {}).get("approved_document_sha256"),
                    }
                    artifacts = self._normalize_artifacts(
                        project_id, list(envelope["artifacts"])
                    )
                    continued = await self.repository.finalize_agent_continue(
                        run_id=str(run["run_id"]), worker_instance_id=worker_instance_id,
                        claim_token=claim_token, project_id=project_id,
                        channel=str(trigger["channel"]), chat_id=str(trigger["chat_id"]),
                        user_id=trigger["user_id"], source_message_id=source_message_id,
                        summary="Kernel Validation通过；切换到高风险只读冷审计。",
                        request_patch=audit_patch, artifact_refs=artifacts,
                        usage_snapshot=usage_snapshot, reset_execution_plan=True,
                        wait_for_supervisor=True,
                    )
                    await self._queue_turn_evaluation(
                        project_id=project_id, task_id=str(run["run_id"]),
                        turn_number=turn_number, trigger=trigger,
                        reply_message_id=str(continued["reply"]["message_id"]),
                        outcome="ready_for_validation",
                        summary=str(envelope.get("summary") or "validation passed"),
                        usage=usage_snapshot, artifacts=artifacts,
                    )
                    return {
                        "outcome": "continue", "status": "queued",
                        "run_id": str(continued["run"]["run_id"]),
                        "task_phase": "audit", "validation": validation_artifact,
                        "token_usage": usage_snapshot,
                    }
                release_patch = {
                    **task_request_patch,
                    "task_phase": "waiting_release",
                    "pending_validation_supervisor_turn": None,
                    "validation_supervisor_approved_turn": None,
                    "approval_kind": "release",
                    "sealed_candidate_bundle": bundle_artifact,
                    "validation_artifact": validation_artifact,
                }
                await self.repository.patch_claimed_run_request(
                    run_id=str(run["run_id"]), worker_instance_id=worker_instance_id,
                    claim_token=claim_token, request_patch=release_patch,
                )
                run = {**run, "request_json": {
                    **dict(run.get("request_json") or {}), **release_patch,
                }}
                envelope["outcome"] = "needs_approval"
                envelope["summary"] = (
                    str(envelope.get("summary") or "Candidate validated")
                    + "\nKernel Validation通过，等待发布批准。"
                )
                envelope["document_markdown"] = (
                    "# Candidate release\n\n"
                    f"- Task: `{run['run_id']}`\n"
                    f"- Candidate bundle: `{bundle['bundle_sha256']}`\n"
                    f"- Files: {len(bundle['files'])}\n"
                    f"- Validation: {validation_artifact.get('status')}\n\n"
                    "批准只允许Publisher按已密封bundle和正式基线SHA原子应用；"
                    "不授权网络、凭据、服务重启、账户连接或交易。"
                )
                envelope["approval_question"] = "是否批准发布这份已验证candidate？"


        if envelope["outcome"] in {"needs_input", "needs_approval"}:
            submitted_artifacts = list(envelope.get("artifacts") or [])
            request_payload = dict(run.get("request_json") or {})
            artifacts = self._normalize_artifacts(
                project_id, self._add_candidate_manifest(
                    run, project_id, submitted_artifacts
                )
            )
            message_type = "question"
            if envelope["outcome"] == "needs_input":
                questions = [dict(item) for item in (envelope.get("questions") or [])]
                if not questions or any(
                    not str(item.get("question_id") or "").strip()
                    or not str(item.get("question_text") or "").strip()
                    for item in questions
                ):
                    raise ValueError("needs_input requires stable IDs and complete question text")
                for item in questions:
                    try:
                        options = normalize_question_options(item.get("options"))
                    except ValueError:
                        # An optional card hint cannot suppress a valid question.
                        item.pop("options", None)
                        continue
                    if options is not None:
                        item["options"] = options
                    input_schema = item.get("input_schema")
                    if input_schema is not None:
                        if not isinstance(input_schema, dict):
                            raise ValueError("question input_schema must be an object")
                        if input_schema.get("kind") != "project_credential":
                            raise ValueError("unsupported question input_schema kind")
                        request_id = str(input_schema.get("request_id") or "")
                        expected = self.credentials.request_root(project_id) / f"{request_id}.json"
                        if not request_id or not expected.is_file():
                            raise ValueError("credential question is not backed by a Kernel request")
                text = "\n\n".join(str(item["question_text"]) for item in questions)
                interaction = {
                    "text": text, "questions": questions,
                    "summary": envelope.get("summary", ""),
                    "stage": run.get("stage") or "PM",
                }
            else:
                document = str(envelope.get("document_markdown") or "").strip()
                approval_question = str(envelope.get("approval_question") or "").strip()
                if not document or not approval_question:
                    raise ValueError("needs_approval requires a complete document and approval question")
                artifacts.append(self._materialize_submission(run, document))
                message_type = "approval"
                interaction = {
                    "text": "\n\n".join(
                        part for part in (
                            str(envelope.get("summary") or "").strip(), document,
                            approval_question,
                            (
                                "请选择审批卡片操作；如卡片不可用，可回复“批准”、"
                                "直接说明修改意见或回复“拒绝”。"
                            ),
                        ) if part
                    ),
                    "document_markdown": document,
                    "approval_question": approval_question,
                    "summary": envelope.get("summary", ""), "artifact_refs": artifacts,
                    "operation_approval_request": request_payload.get("operation_approval_request"),
                    "stage": run.get("stage"),
                    "decision_options": ["approve", "revise", "reject"],
                }
            message = await self.repository.finalize_agent_waiting(
                run_id=str(run["run_id"]), worker_instance_id=worker_instance_id,
                claim_token=claim_token, session_id=str(session["session_id"]),
                native_session_id=result.physical_session_id,
                input_tokens=result.usage.input_tokens, context_window_tokens=context_window,
                archive_uri=result.archive_uri, archive_sha256=result.archive_sha256,
                project_id=project_id, channel=str(trigger["channel"]),
                chat_id=str(trigger["chat_id"]), user_id=trigger["user_id"],
                source_message_id=source_message_id, question=interaction,
                message_type=message_type, artifact_refs=artifacts,
                usage_snapshot=usage_snapshot,
            )
            if message.get("input_pending"):
                return {"outcome":"continue", "status":"queued", "run_id":str(run["run_id"])}
            if single_agent_task:
                await self._queue_turn_evaluation(
                    project_id=project_id, task_id=str(run["run_id"]),
                    turn_number=turn_number, trigger=trigger,
                    reply_message_id=str(message["message_id"]),
                    outcome=envelope["outcome"],
                    summary=str(envelope.get("summary") or ""),
                    usage=usage_snapshot, artifacts=artifacts,
                )
            return {
                "outcome": envelope["outcome"],
                "question_message_id": str(message["message_id"]),
                "question_message_ids": list(
                    message.get("question_message_ids") or [str(message["message_id"])]
                ),
                "token_usage": usage_snapshot,
            }
        if envelope["outcome"] == "failed":
            raise RuntimeError(str(envelope.get("summary") or "agent reported failure"))
        if envelope["outcome"] == "blocked":
            artifacts = self._normalize_artifacts(
                project_id, list(envelope.get("artifacts") or [])
            )
            permission_request = None
            permission_handoff = None
            if envelope.get("permission_request") is not None:
                if run.get("run_type") != "stage" and not single_agent_task:
                    raise ValueError("only a Stage Run can emit permission_request")
                permission_request = normalize_permission_request(
                    envelope["permission_request"], project_id=project_id,
                    run_id=str(run["run_id"]), agent_key=agent_key,
                    stage=str(run.get("stage") or "") or None,
                )
                if single_agent_task:
                    permission_request = None
                    envelope["summary"] = (
                        str(envelope.get("summary") or "")
                        + "\n当前Project不使用独立动作授权；请核查实际能力、配置或连接错误。"
                    ).strip()
                elif permission_request["action"] in {"workspace", "read"}:
                    permission_handoff = await self.repository.handoff_from_claim(
                        source_run_id=str(run["run_id"]),
                        worker_instance_id=worker_instance_id, claim_token=claim_token,
                        target_project_id=project_id,
                        target_agent_key=f"project:{project_id}:manager",
                        handoff_summary=(
                            "PERMISSION_REQUEST\n"
                            + json.dumps(permission_request, ensure_ascii=False, sort_keys=True)
                        ),
                    )
                elif permission_request["action"] in {
                    "network", "credential_use", "account_connect", "trade",
                    "candidate_write", "candidate_execute", "execute_once",
                    "publish_once",
                }:
                    permission_request = None
                    envelope["summary"] = (
                        str(envelope.get("summary") or "")
                        + "\n当前Project不使用独立动作授权；该授权申请不适用。"
                        "请核查实际能力、配置、连接或脚本错误。"
                    ).strip()
                else:
                    permission_request["capability_status"] = "not_implemented"
                    envelope["summary"] = (
                        str(envelope.get("summary") or "")
                        + "\nKernel目前没有可激活此动作授权的执行器；"
                        "请由Kernel维护任务补齐后恢复，不要重复批准文件Scope。"
                    ).strip()
            blocked = await self.repository.finalize_agent_blocked(
                run_id=str(run["run_id"]), worker_instance_id=worker_instance_id,
                claim_token=claim_token,
                summary=str(envelope.get("summary") or "Stage is blocked"),
                artifact_refs=artifacts,
                permission_request=permission_request,
            )
            if single_agent_task:
                await self._queue_turn_evaluation(
                    project_id=project_id, task_id=str(run["run_id"]),
                    turn_number=turn_number, trigger=trigger,
                    reply_message_id=str(blocked["reply"]["message_id"]),
                    outcome="blocked",
                    summary=str(envelope.get("summary") or ""),
                    usage=usage_snapshot, artifacts=artifacts,
                )
            return {
                "outcome": "blocked", "status": "blocked",
                "reply_message_id": str(blocked["reply"]["message_id"]),
                "permission_pm_run_id": (
                    str(permission_handoff["run"]["run_id"])
                    if permission_handoff else None
                ),
                "token_usage": usage_snapshot,
            }
        change_class = str((run["request_json"] or {}).get("change_class") or "major")
        request_data = dict(run.get("request_json") or {})
        review_has_candidate = any(
            isinstance(item, dict)
            and (
                item.get("kind") == "candidate_manifest"
                or str(item.get("uri") or "").endswith("/candidate/manifest.json")
            )
            for item in request_data.get("prior_artifacts") or []
        )

        submitted_artifacts = self._add_candidate_manifest(
            run, project_id, list(envelope.get("artifacts") or [])
        )
        validation_artifact = None
        artifacts = self._normalize_artifacts(project_id, submitted_artifacts)
        summary = str(envelope.get("summary") or result.final_text).strip()
        completion = await self.repository.finalize_agent_completed(
            run_id=str(run["run_id"]), worker_instance_id=worker_instance_id,
            claim_token=claim_token, session_id=str(session["session_id"]),
            native_session_id=result.physical_session_id,
            input_tokens=result.usage.input_tokens, context_window_tokens=context_window,
            archive_uri=result.archive_uri, archive_sha256=result.archive_sha256,
            project_id=project_id, channel=str(trigger["channel"]),
            chat_id=str(trigger["chat_id"]), user_id=trigger["user_id"],
            source_message_id=source_message_id, summary=summary,
            result={
                "outcome": "completed", "summary": summary,
                "artifact_refs": artifacts, "token_usage": usage_snapshot,
            },
            artifact_refs=artifacts, usage_snapshot=usage_snapshot,
            advance_stage=envelope["outcome"] != "blocked",
        )
        if completion.get("input_pending"):
            return {"outcome":"continue", "status":"queued", "run_id":str(run["run_id"])}
        if single_agent_task:
            await self._queue_turn_evaluation(
                project_id=project_id, task_id=str(run["run_id"]),
                turn_number=turn_number, trigger=trigger,
                reply_message_id=str(completion["reply"]["message_id"]),
                outcome="completed",
                summary=str(envelope.get("summary") or ""),
                usage=usage_snapshot, artifacts=artifacts,
            )
        next_run = completion.get("next_run")
        return {
            "outcome": "completed",
            "reply_message_id": str(completion["reply"]["message_id"]),
            "next_run_id": str(next_run["run_id"]) if next_run else None,
            "token_usage": usage_snapshot,
        }
