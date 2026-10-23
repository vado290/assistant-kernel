"""Pure business rules: stages, cursors, stage moves, gates and derived approvals.

Nothing here touches the database, so every rule is unit-testable on plain dicts.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from assistant_kernel.core import i18n

ACTIVE_STATES = ("pm", "supervising", "awaiting_user", "validating", "publishing", "bootstrapping")
TERMINAL_STATES = ("completed", "cancelled")

# Each active state is waited on by exactly one cursor (plan principle P7).
CURSOR_RUN = {
    "pm": ("pm_turn", "default"),
    "supervising": ("supervisor", "evaluate"),
    "validating": ("validation", "default"),
    "publishing": ("publish", "default"),
    "bootstrapping": ("bootstrap", "default"),
}

PROJECT_ID = re.compile(r"[a-z][a-z0-9-]{2,39}")


CREATION_SECTION = "creation"     # contract (prompts/core/pm-bootstrap.md): the YAML block Kernel creates a project from
PROJECT_CAPABILITIES = ("routing", "project_management", "shared_context")   # kernel_maintenance: kernel only
DELIVERIES = ("vault-files", "git-repo")   # git-repo: code delivery (R4a); it turns on the git_delivery capability
SHARE_LEVELS = ("metrics", "improvements", "full")


def project_spec(spec: Any) -> dict[str, Any]:
    """Validate and normalise the creation parameters of a new project (ROUND2 R3)."""
    if not isinstance(spec, dict):
        raise ValueError("the creation parameters must be a YAML object")
    project_id = str(spec.get("project_id") or "").strip()
    if not PROJECT_ID.fullmatch(project_id):
        raise ValueError("project_id is 3–40 lowercase letters, digits or hyphens, starting with a letter")
    display_name, purpose = str(spec.get("display_name") or "").strip(), str(spec.get("purpose") or "").strip()
    if not display_name or not purpose:
        raise ValueError("display_name or purpose is missing")
    capabilities = [str(c) for c in spec.get("capabilities") or []]
    unknown = sorted(set(capabilities) - set(PROJECT_CAPABILITIES))
    if unknown:
        raise ValueError(f"unknown or disallowed capabilities {unknown}; choose from {', '.join(PROJECT_CAPABILITIES)}")
    delivery = str(spec.get("delivery") or "vault-files")
    share = str(spec.get("share_with_kernel") or "improvements")
    if delivery not in DELIVERIES or share not in SHARE_LEVELS:
        raise ValueError(f"delivery must be one of {DELIVERIES} and share_with_kernel one of {SHARE_LEVELS}")
    if delivery == "git-repo":   # loads the code-delivery skill
        capabilities.append("git_delivery")
    return {"project_id": project_id, "display_name": display_name[:80], "purpose": purpose[:500],
            "capabilities": sorted(set(capabilities)), "delivery": delivery, "share_with_kernel": share,
            "directories": [d for d in spec.get("directories") or [] if isinstance(d, dict)][:10],
            "pm_role": str(spec.get("pm_role") or "").strip()[:2000],
            "supervisor_role": str(spec.get("supervisor_role") or "").strip()[:2000]}


def charter_spec(markdown: str) -> dict[str, Any]:
    """Creation parameters from a charter draft's creation-parameters section (a YAML block); raises ValueError."""
    import yaml
    from assistant_kernel.core.skills import section_text
    block = section_text(markdown, CREATION_SECTION)
    if not block:
        raise ValueError(f"the charter has no '{i18n.heading(CREATION_SECTION)}' section")
    fenced = re.search(r"```(?:ya?ml)?\s*\n(.*?)```", block, re.S)
    try:
        data = yaml.safe_load(fenced.group(1) if fenced else block)
    except yaml.YAMLError as exc:
        raise ValueError(f"'{i18n.heading(CREATION_SECTION)}' is not valid YAML: {exc}") from exc
    return project_spec(data)

INITIAL_STAGE = {"delivery": "requirements", "bootstrap": "requirements", "evolution": "operate"}

# Stage moves the Supervisor may propose; everything else is rejected by Kernel.
STAGE_MOVES = {
    "requirements": {"requirements", "design"},
    "design": {"design", "build", "requirements"},
    "build": {"build", "review", "design", "requirements"},
    "review": {"review", "release", "build", "design", "requirements"},
    "release": {"release"},
    "operate": {"operate"},
}

# The quick track (R2.3): the requirements document carries a short plan; no design or review stage.
QUICK_MOVES = {
    "requirements": {"requirements", "build"},
    "build": {"build", "release", "requirements"},
    "release": {"release"},
    "operate": {"operate"},
}
TRACKS = ("full", "quick")

# Moves that need a still-valid user approval of the document the stage produced.
APPROVAL_FOR_MOVE = {("requirements", "design"): "requirements", ("design", "build"): "design",
                     ("requirements", "build"): "requirements"}   # the last one only exists on the quick track

DOCUMENT_FOR_APPROVAL = {"requirements": "task_contract", "design": "solution_design"}


def stage_moves(task: dict[str, Any]) -> set[str]:
    if task.get("kind") == "bootstrap":   # a project-creation task only drafts its charter; approval creates the project
        return {"requirements"} if task["stage"] == "requirements" else set()
    return (QUICK_MOVES if task.get("track") == "quick" else STAGE_MOVES).get(task["stage"], set())


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


def validation_fingerprint(task: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(task["documents"].get("validation") or {},
                                    sort_keys=True, default=str).encode("utf-8")).hexdigest()


def review_baseline(task: dict[str, Any]) -> dict[str, str] | None:
    """The exact inputs given to Review; stored in its immutable document payload by the executor."""
    baseline = {kind: doc_sha(task, kind) for kind in
                ("task_contract", "solution_design", "implementation_report")}
    baseline.update(bundle_sha=validated_bundle(task), validation_sha=validation_fingerprint(task))
    return baseline if all(baseline.values()) else None


def review_error(task: dict[str, Any]) -> str | None:
    """Full-delivery release eligibility, not the historical validity of a release approval."""
    if not approval_valid(task, "design"):
        return "the requirements or design approval is missing or void; confirm it again in the current stage"
    baseline = review_baseline(task)
    if not baseline:
        return "the review input is incomplete or the current delivery has not passed validation"
    review = task["documents"].get("review_report") or {}
    if not review.get("review_baseline"):
        return "the review report has no review baseline and must be reviewed again; an old report is never signed afterwards"
    if review["review_baseline"] != baseline or review.get("bundle_sha") != baseline["bundle_sha"]:
        return "the review basis or the delivery changed; review the current version again"
    if review.get("verdict") != "pass":
        return "the current review has not passed"
    return None


def review_passed(task: dict[str, Any]) -> bool:
    return review_error(task) is None


def approvals_needed(task: dict[str, Any], target: str) -> list[str]:
    """Approvals the user must give together with executing a move to `target`."""
    kind = APPROVAL_FOR_MOVE.get((task["stage"], target))
    return [kind] if kind and not approval_valid(task, kind) else []


def move_error(task: dict[str, Any], target: str, *, approving: tuple[str, ...] = ()) -> str | None:
    """Why a stage move is not allowed right now (None = allowed).

    `approving` lists approvals that are being granted in the same user action.
    """
    stage = task["stage"]
    if target not in stage_moves(task):
        return f"stage move {stage} → {target} is not allowed"
    kind = APPROVAL_FOR_MOVE.get((stage, target))
    if kind:
        if not doc_sha(task, DOCUMENT_FOR_APPROVAL[kind]):
            return f"{kind} document is missing"
        if kind not in approving and not approval_valid(task, kind):
            return f"{kind} approval is missing or outdated"
    if (stage, target) == ("build", "review") and not validated_bundle(task):
        return "the current implementation has not passed validation"
    if target == "release" and task.get("kind", "delivery") == "delivery" and task.get("track") != "quick":
        error = review_error(task)   # also covers release → release after a publication failure
        if error:
            return error
    if (stage, target) == ("build", "release") and not validated_bundle(task):   # quick track
        return "the current implementation has not passed validation"
    return None
