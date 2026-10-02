from __future__ import annotations

from collections.abc import Mapping, Set


class InvalidTransition(ValueError):
    pass


JOB_TRANSITIONS: Mapping[str, Set[str]] = {
    "queued": {"running", "cancelled", "dead_letter"},
    "running": {"queued", "succeeded", "failed", "cancelled", "dead_letter"},
    "succeeded": set(),
    "failed": set(),
    "cancelled": set(),
    "dead_letter": set(),
}

RUN_TRANSITIONS: Mapping[str, Set[str]] = {
    "queued": {"running", "cancelled"},
    "running": {"waiting_user", "needs_rework", "succeeded", "failed", "cancelled"},
    "waiting_user": {"running", "cancelled", "failed"},
    "needs_rework": {"queued", "cancelled"},
    "succeeded": set(),
    "failed": set(),
    "cancelled": set(),
}

SESSION_TRANSITIONS: Mapping[str, Set[str]] = {
    "starting": {"active", "failed"},
    "active": {"rotation_required", "sealed", "failed"},
    "rotation_required": {"sealed", "active", "failed"},
    "sealed": set(),
    "failed": set(),
}

WORKFLOW_VERSION_TRANSITIONS: Mapping[str, Set[str]] = {
    "draft": {"candidate", "retired"},
    "candidate": {"active", "draft", "retired"},
    "active": {"retired"},
    "retired": set(),
}


def require_transition(
    transitions: Mapping[str, Set[str]], current: str, target: str
) -> None:
    if current not in transitions:
        raise InvalidTransition(f"unknown current state {current!r}")
    if target not in transitions[current]:
        raise InvalidTransition(f"invalid transition {current!r} -> {target!r}")
