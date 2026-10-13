"""Deterministic user-facing lifecycle projection for mixed Task state fields."""
from __future__ import annotations

from typing import Any


def task_lifecycle_view(run: dict[str, Any]) -> dict[str, Any]:
    request = dict(run.get("request_json") or {})
    status = str(run.get("status") or "")
    phase = str(request.get("task_phase") or "working")
    current_stage = str(request.get("current_stage") or "requirements")
    audit_mode = bool(request.get("audit_mode"))
    approval_kind = str(request.get("approval_kind") or "") or None

    if request.get("task_activation") == "suspended":
        lifecycle = "Suspended"
    elif request.get("task_activation") == "queued":
        lifecycle = "Queued"
    elif status == "waiting_supervisor":
        lifecycle = "Supervisor"
    elif status == "reconciling" or phase == "publication_reconciling":
        lifecycle = "Reconciliation"
    elif status in {"failed", "dead_letter", "cancelled"}:
        lifecycle = "Failed"
    elif phase == "published" or (
        status == "succeeded" and request.get("publication_state") == "published"
    ):
        lifecycle = "Published"
    elif phase in {"publishing", "waiting_release"} or approval_kind == "release":
        lifecycle = "Release"
    elif audit_mode or phase == "audit":
        lifecycle = "Verify/Audit"
    elif current_stage == "build":
        lifecycle = "Build"
    elif current_stage == "design":
        lifecycle = "Design"
    elif current_stage == "operate":
        lifecycle = "Operation"
    else:
        lifecycle = "Requirements"

    return {
        "schema_version": "task-lifecycle-view.v1",
        "task_id": str(run.get("run_id") or ""),
        "lifecycle": lifecycle,
        "run_status": status,
        "current_stage": current_stage,
        "task_phase": phase,
        "audit_mode": audit_mode,
        "approval_kind": approval_kind,
        "pending_supervision_turn": request.get("pending_supervision_turn"),
        "pending_interaction_ref": request.get("pending_interaction_ref"),
        "task_activation": request.get("task_activation", "active"),
    }
