"""Regression coverage for complete handoffs and project-local feedback, without models."""
from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from test_stage_integration import settings, work
from assistant_kernel.api.v2 import build_v2_router
from assistant_kernel.domain.project_build import after_approval
from assistant_kernel.harness import AgentTurnResult
from assistant_kernel.repositories import ControlPlaneRepository
from assistant_kernel.repositories.feedback import FeedbackRepository
from assistant_kernel.services.agent_directory import AgentDirectory
from assistant_kernel.services.context_budget import TurnUsage
from assistant_kernel.services.project_build import CodexStageExecutor, ProjectBuildProcessor
from assistant_kernel.storage import ProjectRuntimeManager

pytestmark = pytest.mark.skipif(os.getenv("KERNEL_STAGE_INTEGRATION") != "1", reason="isolated PostgreSQL opt-in")


def feedback(**overrides):
    return {"text": "日报过长，重复内容太多", "expected_behavior": "只保留变化",
            "user_id": "owner", "channel": "local", "impact": "medium", **overrides}


async def test_agent_directory_and_custom_role_survive_initialization(settings):
    repo = ControlPlaneRepository(settings)
    directory = AgentDirectory(settings, repo)
    p = await repo.ensure_default_agent("p")
    q = await repo.ensure_default_agent("q")
    chief = await repo.ensure_default_agent()
    assert not (settings.data_root / "vaults").exists()
    views = await directory.list()
    assert len({v["home_path"] for v in views}) == 3
    assert not (settings.data_root / "vaults").exists()  # GETs are read-only.
    custom = await repo.ensure_role_version(p, "primary", "P 的定制角色")
    initialized = await directory.initialize(p)
    assert initialized["role_version_id"] == str(custom["role_version_id"])
    assert Path(initialized["role_path"]).read_text() == "P 的定制角色"
    assert Path(initialized["skills_path"]).is_dir()
    Path(initialized["role_path"]).write_text("尚未发布的手工改动")
    again = await directory.initialize(p)
    assert again["role_file_matches_active"] is False
    assert Path(again["role_path"]).read_text() == "尚未发布的手工改动"
    assert (await repo.get_active_role(p))["content"] == "P 的定制角色"
    assert await directory.get("missing") is None


async def test_feedback_concurrent_idempotency_archive_and_project_scope(settings):
    repo = ControlPlaneRepository(settings)
    await repo.ensure_default_agent("p")
    await repo.ensure_default_agent("q")
    inbox = FeedbackRepository(settings)
    first, second = await asyncio.gather(
        inbox.submit("p", feedback(), "one"), inbox.submit("p", feedback(), "one"))
    assert first["feedback_id"] == second["feedback_id"]
    assert sum(item["duplicate"] for item in (first, second)) == 1
    assert len(await inbox.list("p")) == 1
    assert await inbox.list("q") == []
    assert await inbox.get("q", first["feedback_id"]) is None
    assert first["optimizer_started"] is False
    path = settings.data_root / "vaults/p/artifacts/feedback" / f"{first['feedback_id']}.md"
    assert "日报过长" in path.read_text()
    assert hashlib.sha256(path.read_bytes()).hexdigest() == first["content_sha256"]
    with pytest.raises(ValueError, match="different feedback"):
        await inbox.submit("p", feedback(text="不同反馈"), "one")
    path.unlink()
    assert (await inbox.submit("p", feedback(), "one"))["duplicate"]
    assert path.exists()
    path.write_text("手工篡改")
    with pytest.raises(ValueError, match="differs"):
        await inbox.submit("p", feedback(), "one")
    qrun = await repo.start_project_build(project_id="q", request_data={}, idempotency_key="q-build")
    with pytest.raises(ValueError, match="task does not belong"):
        await inbox.submit("p", feedback(task_id=qrun["task_id"]), "foreign-task")
    with pytest.raises(ValueError, match="run does not belong"):
        await inbox.submit("p", feedback(workflow_run_id=qrun["workflow_run_id"]), "foreign-run")
    with pytest.raises(ValueError, match="project not found"):
        await inbox.submit("missing", feedback(), "missing")
    assert not (settings.data_root / "vaults/missing").exists()


async def test_new_api_routes(settings):
    repo = ControlPlaneRepository(settings)
    agent = await repo.ensure_default_agent("p")
    app = FastAPI()
    app.include_router(build_v2_router(settings, repo, lambda: None))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert len((await client.get("/v2/agents?project_id=p")).json()["agents"]) == 1
        assert (await client.post(f"/v2/agents/{agent}/runtime")).status_code == 200
        assert (await client.get("/v2/agents/unknown/runtime")).status_code == 404
        response = await client.post("/v2/projects/p/feedback", json=feedback(), headers={"X-Idempotency-Key": "api"})
        assert response.status_code == 201, response.text
        fid = response.json()["feedback_id"]
        assert (await client.get(f"/v2/projects/p/feedback/{fid}")).status_code == 200
        assert (await client.get(f"/v2/projects/q/feedback/{fid}")).status_code == 404
        assert len((await client.get("/v2/projects/p/feedback")).json()["feedback"]) == 1
        blank = await client.post("/v2/projects/p/feedback", json=feedback(text="  "), headers={"X-Idempotency-Key": "blank"})
        assert blank.status_code == 422


class RecordingHarness:
    name = "fake"

    def __init__(self):
        self.requests = []

    async def run_turn(self, request):
        self.requests.append(request)
        text = "完整需求" + "内容" * 1500 + "尾部必须保留"
        if "<stage>review</stage>" in request.prompt:
            text = "VERDICT: APPROVE"
        return AgentTurnResult(physical_session_id=str(uuid.uuid4()), usage=TurnUsage(input_tokens=100),
                               events=[], final_text=text, provider="fake", model="fake")


async def test_real_stage_executor_receives_full_documents_all_feedback_and_custom_role(settings):
    repo = ControlPlaneRepository(settings)
    run = await repo.start_project_build(project_id="p", request_data={"goal": "日报", "user_id": "owner"}, idempotency_key="build")
    aid = await repo.ensure_stage_agent("p", "requirements")
    role = await repo.ensure_role_version(aid, "primary", "本项目的独立需求方法")
    await repo.ensure_role_version(aid, "secondary", "不能覆盖 primary")
    harness = RecordingHarness()
    processor = ProjectBuildProcessor(settings, repo, harness)
    _, waiting = await work(repo, processor)
    assert "本项目的独立需求方法" in harness.requests[-1].prompt
    for note in ("第一轮补充", "第二轮补充"):
        await repo.decide_approval(approval_id=waiting["approval_id"], decision="clarify", edited_payload={"note": note},
                                  decided_by="owner", idempotency_key=note)
        _, waiting = await work(repo, processor)
    prompt = harness.requests[-1].prompt
    assert all(text in prompt for text in ("第一轮补充", "第二轮补充", "尾部必须保留"))
    assert (await repo.get_active_role(aid))["role_version_id"] == role["role_version_id"]
    await repo.decide_approval(approval_id=waiting["approval_id"], decision="approve", edited_payload=None,
                              decided_by="owner", idempotency_key=str(uuid.uuid4()))
    await work(repo, processor)  # implementation
    _, waiting = await work(repo, processor)  # review -> acceptance
    await repo.decide_approval(approval_id=waiting["approval_id"], decision="edit", edited_payload={"note": "验收要求减少重复"},
                              decided_by="owner", idempotency_key="accept-edit")
    await work(repo, processor)
    assert "验收要求减少重复" in harness.requests[-1].prompt
    assert "<stage>implementation</stage>" in harness.requests[-1].prompt


async def test_document_loader_rejects_foreign_tampered_and_oversized_artifacts(settings):
    executor = CodexStageExecutor(settings, ControlPlaneRepository(settings), None)
    runtime = ProjectRuntimeManager(settings.data_root)
    file = runtime.resolve_vault_file("p", "report.md")
    file.write_text("完整正文")
    artifact = {"storage_uri": "vault://p/report.md", "content_sha256": hashlib.sha256(file.read_bytes()).hexdigest()}
    state = {"project_id": "p", "requirements_artifact": artifact}
    assert executor._artifact_contents(state)["requirements"] == "完整正文"
    file.write_text("changed")
    with pytest.raises(ValueError, match="hash mismatch"):
        executor._artifact_contents(state)
    with pytest.raises(ValueError, match="outside"):
        executor._artifact_contents({"project_id": "q", "requirements_artifact": artifact})
    file.write_bytes(b"x" * 262145)
    with pytest.raises(ValueError, match="document limit"):
        executor._artifact_contents(state)


async def test_runtime_rejects_project_root_and_skill_symlink_escape(settings):
    manager = ProjectRuntimeManager(settings.data_root)
    q = manager.ensure("q")
    (settings.data_root / "vaults/p").symlink_to(q.vault_root, target_is_directory=True)
    with pytest.raises(ValueError, match="redirect"):
        manager.ensure("p")
    own = manager.ensure("own")
    (own.vault_root / "link").symlink_to(q.vault_root, target_is_directory=True)
    with pytest.raises(ValueError, match="escaped"):
        manager.resolve_vault_file("own", "link/report.md")
