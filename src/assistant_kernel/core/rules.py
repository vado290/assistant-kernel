"""Pure business rules: stages, cursors, stage moves, gates and derived approvals.

Nothing here touches the database, so every rule is unit-testable on plain dicts.
"""
from __future__ import annotations

from typing import Any

ACTIVE_STATES = ("pm", "supervising", "awaiting_user", "validating", "publishing")
TERMINAL_STATES = ("completed", "cancelled", "failed")

# Each active state is waited on by exactly one cursor (plan principle P7).
CURSOR_RUN = {
    "pm": ("pm_turn", "default"),
    "supervising": ("supervisor", "evaluate"),
    "validating": ("validation", "default"),
    "publishing": ("publish", "default"),
}

INITIAL_STAGE = {"delivery": "requirements", "bootstrap": "requirements",
                 "operation": "operate", "evolution": "operate"}

# Stage moves the Supervisor may propose; everything else is rejected by Kernel.
STAGE_MOVES = {
    "requirements": {"requirements", "design"},
    "design": {"design", "build", "requirements"},
    "build": {"build", "review", "design", "requirements"},
    "review": {"review", "release", "build", "design", "requirements"},
    "release": {"release"},
    "operate": {"operate"},
}

# Moves that need a still-valid user approval of the document the stage produced.
APPROVAL_FOR_MOVE = {("requirements", "design"): "requirements", ("design", "build"): "design"}

DOCUMENT_FOR_APPROVAL = {"requirements": "task_contract", "design": "solution_design"}


def doc_sha(task: dict[str, Any], name: str) -> str | None:
    return (task["documents"].get(name) or {}).get("sha256")


def approval_valid(task: dict[str, Any], kind: str) -> bool:
    """Approvals are never cleared by hand: they are valid only while the documents still match."""
    approval = task["approvals"].get(kind)
    if not approval:
        return False
    if kind == "requirements":
        return approval["doc_sha"] == doc_sha(task, "task_contract")
    if kind == "design":
        return (approval["doc_sha"] == doc_sha(task, "solution_design")
                and approval["based_on_requirements_sha"] == doc_sha(task, "task_contract")
                and approval_valid(task, "requirements"))
    if kind == "release":
        return (approval["bundle_sha"] == validated_bundle(task)
                and approval["review_sha"] == doc_sha(task, "review_report"))
    raise ValueError(f"unknown approval {kind}")


def validated_bundle(task: dict[str, Any]) -> str | None:
    """The candidate bundle sha that passed Kernel validation and is still the current implementation."""
    validation = task["documents"].get("validation") or {}
    implementation = task["documents"].get("implementation_report") or {}
    if validation.get("passed") and validation.get("bundle_sha") == implementation.get("bundle_sha"):
        return validation["bundle_sha"]
    return None


def review_passed(task: dict[str, Any]) -> bool:
    review = task["documents"].get("review_report") or {}
    return bool(review.get("verdict") == "pass" and review.get("bundle_sha")
                and review.get("bundle_sha") == validated_bundle(task))


def approvals_needed(task: dict[str, Any], target: str) -> list[str]:
    """Approvals the user must give together with executing a move to `target`."""
    kind = APPROVAL_FOR_MOVE.get((task["stage"], target))
    return [kind] if kind and not approval_valid(task, kind) else []


def move_error(task: dict[str, Any], target: str, *, approving: tuple[str, ...] = ()) -> str | None:
    """Why a stage move is not allowed right now (None = allowed).

    `approving` lists approvals that are being granted in the same user action.
    """
    stage = task["stage"]
    if target not in STAGE_MOVES.get(stage, set()):
        return f"stage move {stage} → {target} is not allowed"
    kind = APPROVAL_FOR_MOVE.get((stage, target))
    if kind:
        if not doc_sha(task, DOCUMENT_FOR_APPROVAL[kind]):
            return f"{kind} document is missing"
        if kind not in approving and not approval_valid(task, kind):
            return f"{kind} approval is missing or outdated"
    if (stage, target) == ("build", "review") and not validated_bundle(task):
        return "the current implementation has not passed validation"
    if (stage, target) == ("review", "release") and not review_passed(task):
        return "review has not passed for the validated bundle"
    return None
