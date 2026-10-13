from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from assistant_kernel.config import KernelSettings
from assistant_kernel.services.current_artifacts import CurrentArtifactService
from assistant_kernel.services.goal_supervisor import GoalSupervisorService


def settings_for(tmp_path: Path) -> KernelSettings:
    return KernelSettings.from_environment().model_copy(update={
        "data_root": tmp_path,
        "provider_secrets_path": tmp_path / "providers.json",
    })


def test_ui_projection_is_one_cas_updated_file(tmp_path: Path) -> None:
    service = CurrentArtifactService(settings_for(tmp_path))
    first = service.write("demo", "task-1", "supervisor", {
        "schema_version": "supervisor-current.v2", "status": "evaluating",
        "task_id": "task-1",
    })
    same = service.write(
        "demo", "task-1", "supervisor",
        {"schema_version": "supervisor-current.v2", "status": "evaluating",
         "task_id": "task-1"},
        expected_sha256=first["sha256"],
    )
    assert same["sha256"] == first["sha256"]
    updated = service.write(
        "demo", "task-1", "supervisor",
        {"schema_version": "supervisor-current.v2", "status": "ready",
         "task_id": "task-1"},
        expected_sha256=first["sha256"],
    )
    assert updated["sha256"] != first["sha256"]
    with pytest.raises(RuntimeError, match="changed during update"):
        service.write(
            "demo", "task-1", "supervisor", {"status": "ready"},
            expected_sha256=first["sha256"],
        )
    files = list((tmp_path / "vaults" / "demo" / "runs" / "task-1").rglob("*.json"))
    assert [item.name for item in files] == ["current.json"]


def test_supervisor_contract_has_no_artifact_write_surface(tmp_path: Path) -> None:
    """v6.1 pure advisory: artifact_updates/state_update are gone for good."""
    service = GoalSupervisorService(settings_for(tmp_path), SimpleNamespace())
    legacy_payload = json.dumps({
        "decision": "continue_current_stage", "reason": "ok",
        "evidence_refs": [], "missing_evidence": [], "progress_made": True,
        "artifact_updates": {"requirements": {"business_rules": ["new"]}},
        "state_update": {"next_action": "clarify"},
        "next_prompt": {"assembled_prompt": "ask one question"},
    })
    with pytest.raises(ValueError, match="unknown keys"):
        service.parse_decision_v2(legacy_payload)
    assert not hasattr(service, "apply_current_updates")
    assert not hasattr(service, "record_status")
    assert not hasattr(service, "build_view")
    assert not hasattr(service, "evaluate")


def test_parse_decision_v2_normalizes_string_prompt_and_reason(tmp_path: Path) -> None:
    service = GoalSupervisorService(settings_for(tmp_path), SimpleNamespace())
    parsed = service.parse_decision_v2(json.dumps({
        "decision": "consultation_reply", "reason": "ok",
        "next_prompt": "continue safely",
        "user_message": "done",
    }))
    assert parsed["reason"] == ["ok"]
    assert parsed["next_prompt"] == {
        "assembled_prompt": "continue safely",
        "recommended_user_prompt": "continue safely",
    }


def test_parse_decision_v2_strips_code_fence_and_rejects_bad_confidence(
    tmp_path: Path,
) -> None:
    service = GoalSupervisorService(settings_for(tmp_path), SimpleNamespace())
    fenced = "```json\n" + json.dumps({
        "decision": "needs_user_input",
        "intent_hypothesis": [
            {"statement": "define strategy", "confidence": "certain"},
        ],
    }) + "\n```"
    with pytest.raises(ValueError, match="confidence"):
        service.parse_decision_v2(fenced)


def test_parse_decision_v2_accepts_work_mode_and_delegations(tmp_path: Path) -> None:
    service = GoalSupervisorService(settings_for(tmp_path), SimpleNamespace())
    parsed = service.parse_decision_v2(json.dumps({
        "decision": "continue_current_stage", "reason": ["independent review"],
        "target_work_mode": "build", "session_action": "rotate",
        "delegations": [{
            "executor": "native_subagent", "profile": "review",
            "goal": "Review two independent modules",
            "acceptance_criteria": ["Return evidence refs"],
            "declared_write_set": ["candidate/a.py"],
        }],
    }))
    assert parsed["target_work_mode"] == "build"
    assert parsed["session_action"] == "rotate"
    assert parsed["delegations"][0]["delegation_id"].startswith("delegation-")


def test_configuration_defaults_to_sol_medium_with_hard_limits(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    runtime_root = (
        settings.data_root / "vaults" / "demo"
    )
    runtime_root.mkdir(parents=True, exist_ok=True)
    (runtime_root / "project.yaml").write_text(
        "supervisor:\n  enabled: true\n", encoding="utf-8"
    )
    service = GoalSupervisorService(settings, SimpleNamespace())
    config = service.configuration("demo")
    assert config["model"] == "gpt-5.6-sol"
    assert config["reasoning_effort"] == "medium"
    assert config["reasoning_tiers"] == {"default": "medium", "critical": "high"}
    assert "hourly_eval_limit" not in config
    assert config["daily_input_tokens"] == 200_000
    assert config["daily_output_tokens"] == 40_000
    assert "business_state_sources" not in config
