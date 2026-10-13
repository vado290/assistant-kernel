"""One public result envelope; identities and effective state stay Kernel-owned."""
from __future__ import annotations

from typing import Any
from pathlib import Path


RESULT_SCHEMA = "agent-result.v1"
PUBLIC_FIELDS = {
    "schema_version", "outcome", "summary", "questions", "approval_requests",
    "artifact_refs", "evidence_refs", "checkpoint", "payload", "delegation_results",
    "proposed_actions",
}
OUTCOMES = {
    "completed", "continue", "needs_input", "needs_approval", "blocked", "failed",
    "return_to_requirements", "return_to_design", "ready_for_validation",
    "ready_for_release", "request_changes",
}
PHASE_ALIASES = {
    "implementation": "build", "repair": "build", "review": "audit",
    "operation": "operate",
}


def normalize_phase(phase: str) -> str:
    return PHASE_ALIASES.get(str(phase), str(phase))


def publication_requires_final_approval(settings, project_id: str, files, roots) -> bool:
    """Classify the actual targets, so an alternate Grant cannot disguise Kernel code."""
    if project_id in {"kernel", "butler-maintenance"}:
        return True
    from assistant_kernel.services.workspace_scope import WorkspaceScopeService
    scope = WorkspaceScopeService(settings)
    protected = [
        (settings.workspace_root / "assistant-kernel").resolve(),
        (settings.data_root / "config").resolve(),
    ]
    for item in files:
        uri = str(item.get("target_uri") or "")
        if any(part in uri for part in ("/agents/", "/.agents/", "/capabilities/", "/mcp/", "/templates/", "project.yaml")):
            return True
        _, target, _ = scope.resolve_uri(roots, uri)
        target = Path(target).resolve()
        if any(target == directory or directory in target.parents for directory in protected):
            return True
    return False


def normalize_agent_result(raw: dict[str, Any], *, phase: str, expected_revision: str | None = None) -> dict[str, Any]:
    """Validate the common envelope without applying any model-proposed action.

    Legacy PM envelopes remain readable during the one-time migration. Their
    stage-specific fields are data under ``payload``, never a Run request patch.
    Legacy Supervisor decision objects are adapted by its existing decoder.
    """
    if not isinstance(raw, dict):
        raise ValueError("Agent result must be a JSON object")
    if expected_revision not in {None, '1.2'}:
        raise ValueError('Unsupported configured result protocol')
    modern = raw.get("schema_version") == RESULT_SCHEMA
    if expected_revision == '1.2':
        if not modern or raw.get('protocol_revision') != '1.2':
            raise ValueError('The accepted package requires agent-result.v1 protocol_revision 1.2')
        from assistant_kernel.services.contracts import contracts
        contracts().validate('V12_Active_AgentResult', raw)
    if raw.get("schema_version") and not modern:
        raise ValueError("Unsupported agent result schema")
    allowed = PUBLIC_FIELDS | ({'protocol_revision'} if expected_revision == '1.2' else set())
    if modern and set(raw) - allowed:
        raise ValueError("Agent result contains unknown or Kernel-owned fields")
    outcome = raw.get("outcome", "completed" if phase == "supervisor" else None)
    if not isinstance(outcome, str) or outcome not in OUTCOMES:
        raise ValueError("Agent result outcome is invalid")
    summary = raw.get("summary", "")
    if not isinstance(summary, str):
        raise ValueError("Agent result summary must be text")
    payload = raw.get("payload", {})
    if not isinstance(payload, dict):
        raise ValueError("Agent result payload must be an object")
    payload = dict(payload)
    if not modern:
        for key, value in raw.items():
            if key not in PUBLIC_FIELDS and key not in {"task_checkpoint", "artifacts"}:
                payload.setdefault(key, value)
    declared = payload.get("stage")
    if declared is not None and normalize_phase(str(declared)) != normalize_phase(phase):
        raise ValueError("Agent result payload does not match the Kernel phase")
    checkpoint = raw.get("checkpoint", raw.get("task_checkpoint", {}))
    if not isinstance(checkpoint, dict):
        raise ValueError("Agent checkpoint must be an object")
    result = {
        "schema_version": RESULT_SCHEMA, "outcome": outcome, "summary": summary,
        "checkpoint": dict(checkpoint), "payload": payload,
    }
    if expected_revision == '1.2':
        result['protocol_revision'] = '1.2'
    for name in (
        "questions", "approval_requests", "artifact_refs", "evidence_refs",
        "delegation_results", "proposed_actions",
    ):
        value = raw.get(name, raw.get("artifacts", []) if name == "artifact_refs" else [])
        if not isinstance(value, list):
            raise ValueError(f"Agent result {name} must be an array")
        result[name] = list(value)
    for question in result["questions"]:
        if not isinstance(question, dict) or not str(question.get("question_id") or "").strip():
            raise ValueError("Every question requires a stable question_id")
        if not isinstance(question.get("question_text"), str) or not question["question_text"].strip():
            raise ValueError("Every question requires complete original text")
    if modern and outcome == "needs_input" and not result["questions"]:
        raise ValueError("needs_input requires questions")
    return result


def pm_internal_result(result: dict[str, Any]) -> dict[str, Any]:
    """Bridge to existing execution code; only explicitly consumed data is copied."""
    internal = {
        "outcome": result["outcome"], "summary": result["summary"],
        "questions": result["questions"], "artifacts": result["artifact_refs"],
        "task_checkpoint": result["checkpoint"],
        "delegation_results": result["delegation_results"],
        "evidence_refs": result["evidence_refs"], "payload": result["payload"],
        "approval_requests": result["approval_requests"],
        "public_result": result,
    }
    for key in (
        "document_markdown", "approval_question", "issues", "target_stage",
        "blocker_owner", "work_order", "report_markdown",
    ):
        if key in result["payload"]:
            internal[key] = result["payload"][key]
    if result["approval_requests"]:
        approval = result["approval_requests"][0]
        if not isinstance(approval, dict):
            raise ValueError("Approval request must be an object")
        for key in ("document_markdown", "approval_question"):
            if key in approval:
                internal[key] = approval[key]
    return internal
