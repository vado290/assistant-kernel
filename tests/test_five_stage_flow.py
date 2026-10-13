"""Real state/approval/file flow with deterministic model replies and no senders."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import uuid

import pytest
import yaml

from test_core4 import core_settings, FakeHarness
from assistant_kernel.repositories import CoreRepository
from assistant_kernel.services.capability_bundle import CapabilityBundleService
from assistant_kernel.services.agent_results import normalize_phase
from assistant_kernel.services.document_runtime import TaskDocumentService
from assistant_kernel.services.project_directory import ProjectDirectoryService
from assistant_kernel.services.run_processor import RunProcessor
from assistant_kernel.services.task_queue import ProjectTaskQueue
from assistant_kernel.services.workspace_scope import WorkspaceScopeService
from assistant_kernel.storage import ProjectRuntimeManager


CODE = "def result():\n    return 42\n\nassert result() == 42\n"
GOAL = "Implement a tested module returning 42 and report its operation"


def checkpoint(completed):
    return {"goal": GOAL, "completed": completed, "pending": [], "decisions": [],
            "constraints": [], "external_effects": [], "artifacts": [], "open_questions": [],
            "next_action": "Kernel-controlled next phase"}


def response(outcome, stage, **payload):
    return json.dumps({"schema_version": "agent-result.v1", "outcome": outcome,
        "summary": f"{stage}: {outcome}", "questions": [], "approval_requests": [],
        "artifact_refs": [], "evidence_refs": [], "checkpoint": checkpoint([stage]),
        "payload": {"stage": stage, **payload}}, ensure_ascii=False)


class FlowHarness(FakeHarness):
    def __init__(self, settings, project_id="demo"):
        self.settings, self.project_id = settings, project_id
        self.native_number = 0
        self.stages = []
        self.selftest_result = None
        super().__init__([])

    async def run_turn(self, request):
        stage = normalize_phase(json.loads(self.responses[0][0])["payload"]["stage"])
        task_id = str(request.scope_context["KERNEL_SCOPE_RUN_ID"])
        self.stages.append(stage)
        events = [{"type": "item.completed", "item": {"type": "mcp_tool_call",
            "tool": "get_authorized_workspace", "status": "completed", "error": None}}]
        if stage == "build":
            scopes = WorkspaceScopeService(self.settings)
            roots = scopes.resolve(self.project_id, task_id, "implementation", candidate_only=True)
            candidate = next(root for root in roots if root.key == "candidate").path / "project" / "main.py"
            if not candidate.exists():
                scopes.write_candidate(roots, "workspace://project/main.py", CODE, None, expected_state="absent")
                result = subprocess.run([sys.executable, str(candidate)], capture_output=True, text=True, check=False)
                assert result.returncode == 0, result.stderr
                self.selftest_result = {"command": [sys.executable, str(candidate)], "exit_code": result.returncode}
                events.append({"type": "item.completed", "item": {"type": "command_execution",
                    "command": "candidate offline assertions", "status": "completed", "exit_code": result.returncode}})
        if stage == "audit":
            assert "write_candidate_file" not in request.scoped_tools
            assert "delete_candidate_file" not in request.scoped_tools
            candidate = Path(request.cwd) / "runs" / task_id / "candidate" / "project" / "main.py"
            assert candidate.read_text(encoding="utf-8") == CODE
            assert not (Path(request.cwd) / "main.py").exists()
        result = await super().run_turn(request)
        self.native_number += 1
        archive = Path(request.cwd) / "runs" / task_id / "work" / f"fake-model-{self.native_number}.jsonl"
        archive.parent.mkdir(parents=True, exist_ok=True)
        encoded = "\n".join(json.dumps(event) for event in events).encode("utf-8")
        archive.write_bytes(encoded)
        return replace(result, events=events,
            archive_uri=f"vault://{self.project_id}/runs/{task_id}/work/{archive.name}",
            archive_sha256=hashlib.sha256(encoded).hexdigest())


async def setup_flow(settings, project_id="demo"):
    directory = ProjectDirectoryService(settings, SimpleNamespace())
    directory._bootstrap(project_id, GOAL, proposal={"source_run_id": str(uuid.uuid4())},
        proposal_uri="vault://system/fixture.yaml", proposal_sha256="fixture-base")
    root = ProjectRuntimeManager(settings.data_root).describe(project_id).vault_root
    project = yaml.safe_load((root / "project.yaml").read_text(encoding="utf-8"))
    project["single_agent"].update({"audit_required": True, "max_task_turns": 24})
    (root / "project.yaml").write_text(yaml.safe_dump(project, allow_unicode=True), encoding="utf-8")
    repository = CoreRepository(settings)
    incoming = await repository.ingest_message(project_id=project_id, agent_key=f"project:{project_id}:manager",
        channel="discord", chat_id="flow-chat", user_id="flow-user", external_event_id="flow:initial",
        external_message_id="initial", content={"text": GOAL})
    harness = FlowHarness(settings, project_id)
    return repository, harness, str(incoming["run"]["run_id"]), root


async def pm_turn(settings, repository, harness, task_id, text):
    worker = str(uuid.uuid4())
    run = await repository.claim_run("flow", worker, 60)
    assert run is not None and str(run["run_id"]) == task_id, run
    harness.responses.append((text, 10, f"flow-native-{len(harness.requests)}"))
    return await RunProcessor(settings, repository, harness).process(run, worker)


async def approve(repository, task_id, result, project_id="demo"):
    accepted = await repository.ingest_message(project_id=project_id, agent_key=f"project:{project_id}:manager",
        channel="discord", chat_id="flow-chat", user_id="flow-user", external_event_id=str(uuid.uuid4()),
        external_message_id="approve", content={"decision": "approve", "text": "批准当前具体文档"},
        message_type="answer", reply_to_message_id=result["question_message_id"])
    assert str(accepted["run"]["run_id"]) == task_id
    assert accepted["run"]["status"] == "waiting_supervisor"
    return accepted


async def settle_governor(settings, repository, task_id, mode, project_id="demo"):
    """Use real leased Governor Runs, accepting only their current epoch."""
    documents = TaskDocumentService(settings)
    for _ in range(12):
        task = await repository.get_run(task_id)
        if task["status"] not in {"waiting_supervisor", "waiting_user"}:
            return task
        worker = str(uuid.uuid4())
        run = await repository.claim_run("flow-governor", worker, 60)
        assert run is not None and run["agent_key"] == f"project:{project_id}:supervisor", run
        request = dict(run["request_json"] or {})
        epoch = int(request.get("source_context_epoch") or 0)
        if epoch != int(task["request_json"].get("supervisor_context_epoch") or 0):
            await repository.finish_run(str(run["run_id"]), worker, str(run["claim_token"]),
                status="succeeded", result={"outcome": "evaluated_stale"})
            continue
        action = documents.write_revision(project_id=project_id, task_id=task_id,
            document_kind="supervisor-action-plan", document_schema="supervisor-action-plan.v1",
            payload={"target_work_mode": mode, "source_context_epoch": epoch, "confirmation_class": "safe",
                     "pm_instruction": "Continue only the approved current work"},
            status="proposed", actor_type="supervisor", actor_id=str(run["agent_key"]),
            run_id=str(run["run_id"]), reason="Deterministic integration Governor")
        result = await repository.commit_supervisor_action(supervisor_run_id=str(run["run_id"]),
            worker_instance_id=worker, claim_token=str(run["claim_token"]), task_id=task_id,
            source_context_epoch=epoch, source_turn_number=int(task["request_json"].get("task_turn_count") or 0),
            action_ref=action, confirmation_class="safe", pm_instruction="Continue approved work",
            target_work_mode=mode, delegations=[], result={"outcome": "evaluated"}, artifact_refs=[action["uri"]])
        return result["task"]
    raise AssertionError("Governor did not settle")


async def through_requirements(settings, repository, harness, task_id, project_id="demo"):
    result = await pm_turn(settings, repository, harness, task_id,
        response("needs_approval", "requirements", document_markdown="# Requirements\n\nR1: main.py returns 42.\nAcceptance: actual offline assertions pass.",
                 approval_question="是否批准当前需求并进入设计？", requirements=[{"requirement_id": "R1", "text": "Return 42"}], acceptance_criteria=["Offline assertions pass"]))
    task = await repository.get_run(task_id)
    assert task["request_json"]["approval_kind"] == "requirements"
    req_ref = task["request_json"]["accepted_documents"]["task_contract"]
    await approve(repository, task_id, result, project_id)
    task = await settle_governor(settings, repository, task_id, "design", project_id)
    assert task["request_json"]["current_stage"] == "design"
    assert task["request_json"]["requirements_approval"]["approved_document_sha256"] == req_ref["sha256"]
    return req_ref


async def through_design(settings, repository, harness, task_id, project_id="demo", release="automatic_low_risk"):
    req_ref = await through_requirements(settings, repository, harness, task_id, project_id)
    result = await pm_turn(settings, repository, harness, task_id,
        response("needs_approval", "design", document_markdown="# Design\n\nImplement main.py in candidate. Test offline. Review before publication. Produce an operations report.",
                 risk_class="low", release_policy=release, publish_targets=["workspace://project/main.py"], runbook_actions=[]))
    task = await repository.get_run(task_id)
    assert task["request_json"]["approval_kind"] == "design"
    design_ref = task["request_json"]["accepted_documents"]["solution_design"]
    await approve(repository, task_id, result, project_id)
    task = await settle_governor(settings, repository, task_id, "build", project_id)
    assert task["request_json"]["current_stage"] == "build"
    assert task["request_json"]["design_approval"]["approved_document_sha256"] == design_ref["sha256"]
    return req_ref, design_ref


async def through_review(settings, repository, harness, task_id, project_id="demo", requires_final_approval=False):
    build = response("ready_for_validation", "implementation", report_markdown="# Implementation\n\nR1 implemented. Offline assertions passed.")
    await pm_turn(settings, repository, harness, task_id, build)
    task = await settle_governor(settings, repository, task_id, "build", project_id)
    assert task["request_json"]["validation_supervisor_approved_turn"]
    await pm_turn(settings, repository, harness, task_id, build)
    task = await settle_governor(settings, repository, task_id, "audit", project_id)
    assert task["request_json"]["audit_mode"] is True
    assert task["request_json"]["validation_artifact"]
    return await pm_turn(settings, repository, harness, task_id,
        response("ready_for_release", "review", requires_final_approval=requires_final_approval,
                 report_markdown="# Review\n\nR1 → main.py → offline assertions: aligned. No blocking defects. No rule changes proposed."))


async def test_actual_five_stage_approvals_publish_and_operating_report(core_settings):
    repository, harness, task_id, root = await setup_flow(core_settings)
    req_ref, design_ref = await through_design(core_settings, repository, harness, task_id)
    review = await through_review(core_settings, repository, harness, task_id)
    assert review["outcome"] == "continue"
    assert harness.selftest_result["exit_code"] == 0
    task = await settle_governor(core_settings, repository, task_id, "audit")
    assert task["request_json"]["publication_state"] == "queued"
    worker = str(uuid.uuid4())
    publication = await repository.claim_run("publisher", worker, 60)
    assert publication["run_type"] == "publish"
    result, artifacts = CapabilityBundleService(core_settings).publish(project_id="demo",
        publish_run_id=str(publication["run_id"]), request=publication["request_json"])
    await repository.finish_publish_run(str(publication["run_id"]), worker, str(publication["claim_token"]),
                                        result=result, artifact_refs=artifacts)
    assert (root / "main.py").read_text(encoding="utf-8") == CODE
    original = await repository.get_run(task_id)
    assert original["status"] == "succeeded"
    assert original["request_json"]["accepted_documents"]["task_contract"] == req_ref
    assert original["request_json"]["accepted_documents"]["solution_design"] == design_ref
    assert original["request_json"]["accepted_documents"]["implementation_report"]
    assert original["request_json"]["accepted_documents"]["review_report"]
    queue = ProjectTaskQueue(core_settings, repository)
    next_work = await queue.activate_next("demo")
    operation = next_work["task"]
    assert operation["run_id"] != task_id
    assert operation["request_json"]["current_stage"] == "operate"
    assert operation["request_json"]["accepted_documents"]["solution_design"] == design_ref
    assert len((await queue.status("demo"))["active"]) == 1
    await pm_turn(core_settings, repository, harness, operation["run_id"],
        response("completed", "operation", report_markdown="# Operation\n\nPublished main.py returns 42. No external services required. No unresolved feedback."))
    ended = await settle_governor(core_settings, repository, operation["run_id"], "operate")
    assert ended["status"] == "succeeded"
    assert ended["request_json"]["accepted_documents"]["operation_report"]
    assert not (await queue.status("demo"))["active"]
    assert harness.stages == ["requirements", "design", "build", "build", "audit", "operate"]
    assert harness.requests[4].physical_session_id is None
    assert harness.requests[5].physical_session_id is None


async def test_design_revision_returns_to_requirements_without_implementation(core_settings):
    repository, harness, task_id, root = await setup_flow(core_settings)
    old_ref = await through_requirements(core_settings, repository, harness, task_id)
    await pm_turn(core_settings, repository, harness, task_id,
        response("return_to_requirements", "design", document_markdown="# Revised Requirements\n\nR1: return 42.\nR2: operate completely offline.",
                 requirements=[{"requirement_id": "R1", "text": "Return 42"}, {"requirement_id": "R2", "text": "Offline"}]))
    task = await settle_governor(core_settings, repository, task_id, "requirements")
    assert task["request_json"]["current_stage"] == "requirements"
    assert task["request_json"]["requirements_approval"] is None
    assert not task["request_json"].get("design_approval")
    assert task["request_json"]["accepted_documents"]["task_contract"]["sha256"] != old_ref["sha256"]
    assert not (root / "main.py").exists()
    assert "build" not in harness.stages


async def test_kernel_release_still_requires_final_user_approval(core_settings):
    repository, harness, task_id, root = await setup_flow(core_settings, "kernel")
    await through_design(core_settings, repository, harness, task_id, "kernel")
    review = await through_review(core_settings, repository, harness, task_id, "kernel")
    assert review["outcome"] == "needs_approval"
    task = await settle_governor(core_settings, repository, task_id, "audit", "kernel")
    assert task["status"] == "waiting_user"
    assert task["request_json"]["approval_kind"] == "release"
    assert task["request_json"]["reviewed_bundle_sha256"]
    card = await repository.get_message(review["question_message_id"])
    assert "+def result():" in card["content_json"]["document_markdown"]
    assert "Validation receipt" in card["content_json"]["document_markdown"]
    assert not any(run["run_type"] == "publish" for run in await repository.list_runs("kernel"))
    assert not (root / "main.py").exists()


async def test_review_can_tighten_a_low_risk_design_to_final_approval(core_settings):
    repository, harness, task_id, root = await setup_flow(core_settings)
    await through_design(core_settings, repository, harness, task_id)
    review = await through_review(core_settings, repository, harness, task_id, requires_final_approval=True)
    assert review["outcome"] == "needs_approval"
    task = await settle_governor(core_settings, repository, task_id, "audit")
    assert task["status"] == "waiting_user"
    assert task["request_json"]["review_requires_final_approval"] is True
    assert not any(run["run_type"] == "publish" for run in await repository.list_runs("demo"))
    card = await repository.get_message(review["question_message_id"])
    assert "+def result():" in card["content_json"]["document_markdown"]
    assert not (root / "main.py").exists()
