"""Fixed ProjectBuild business transitions. No framework, cursor or saved call stack."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

BUILD_WORKFLOW_ID = "system-project-build"
# A stage is a short-lived Codex execution profile, not a resident Agent process.
# Architecture decisions belong in requirements/implementation and verification
# belongs in implementation/review, so the durable business flow stays small.
STAGES = ("requirements", "implementation", "review")
ProjectBuildState = dict[str, Any]


@dataclass(frozen=True)
class Transition:
    state: ProjectBuildState
    next_stage: str | None = None
    approval: str | None = None
    terminal: str | None = None


def after_stage(stage: str, state: ProjectBuildState, artifact: dict) -> Transition:
    if stage not in STAGES:
        raise ValueError("unknown build stage")
    if not isinstance(artifact, dict):
        raise ValueError("stage result must be an object")
    state = {**state, f"{stage}_artifact": artifact}
    if (artifact.get("interaction_request") or {}).get("kind") == "input":
        state["resume_stage"] = stage
        return Transition(state, approval="stage_input")
    if stage == "requirements":
        return Transition(state, approval="requirements")
    if stage == "implementation":
        return Transition(state, next_stage="review")
    verdict = str(artifact.get("verdict") or "request_changes").lower()
    if verdict not in {"approve", "request_changes", "blocked"}:
        verdict = "request_changes"
    state["review_verdict"] = verdict
    if verdict == "request_changes" and int(state.get("rework_attempt", 0)) < int(state.get("max_rework_attempts", 2)):
        state["rework_attempt"] = int(state.get("rework_attempt", 0)) + 1
        return Transition(state, next_stage="implementation")
    return Transition(state, approval="acceptance")


def allowed_actions(kind: str) -> list[str]:
    if kind == "requirements":
        return ["approve", "edit", "reject", "clarify"]
    if kind == "acceptance":
        return ["approve", "edit", "reject"]
    if kind == "stage_input":
        return ["answer", "reject"]
    raise ValueError("unknown approval kind")


def after_approval(kind: str, state: ProjectBuildState, answer: Any) -> Transition:
    if not isinstance(answer, dict) or answer.get("action") not in allowed_actions(kind):
        raise ValueError("answer must contain an allowed action")
    state = dict(state)
    state["approval_history"] = [
        *state.get("approval_history", []),
        {"kind": kind, "answer": answer},
    ]
    state[f"{kind}_feedback" if kind != "acceptance" else "acceptance_decision"] = answer
    action = answer["action"]
    if action == "reject":
        return Transition(state, terminal="cancelled")
    if kind == "acceptance":
        if action == "approve":
            return Transition(state, terminal="succeeded")
        state["rework_attempt"] = int(state.get("rework_attempt", 0)) + 1
        return Transition(state, next_stage="implementation")
    if action in {"edit", "clarify"}:
        return Transition(state, next_stage=kind)
    return Transition(state, next_stage="implementation")
