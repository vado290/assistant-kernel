"""0.45.5 memory review: profile observations survive a task that ends unapproved, Supervisor records what the user
states in a conversation, Supervisor sees the task ledger, triage sees the profile, other projects' "公共" facts reach
the kernel as candidates, and each memory write is committed on its own."""
from __future__ import annotations

import json
import subprocess

import pytest

from assistant_kernel.core import projectgit
from assistant_kernel.core.dispatch import Dispatcher
from assistant_kernel.core.executors import build_executors
from assistant_kernel.core.executors.common import write_document
from assistant_kernel.core.executors.digest import shared_candidates
from assistant_kernel.core.executors.triage import execute_triage
from assistant_kernel.core.kernel import Kernel
from assistant_kernel.core.vault_sync import sync_vaults

from .test_dispatch import Scripts, settle

CONTRACT = ("# 需求\n\n## 2 目标\n日报\n\n## 10 背景信息存档\n- 公共：公司在上海，约 20 家门店。[用户]\n\n"
            "## 11 用户画像观察\n- 提问方式 · 喜欢一次只答一个问题 · 回合 1 原话 · [用户]\n")


def log(path):
    return subprocess.run(["git", "log", "--format=%s"], cwd=path, capture_output=True, text=True).stdout.splitlines()


@pytest.fixture
def dispatcher(kernel, kernel_settings, fake_harness, project_files):
    return Dispatcher(kernel, build_executors(), services={"settings": kernel_settings, "harness": fake_harness})


async def test_a_task_ending_unapproved_keeps_its_observations_not_its_facts(kernel, kernel_settings, project_files):
    projectgit.ensure_repo(project_files)
    projectgit.commit_all(project_files, "start")
    task_id = (await kernel.receive("p1", role="pm", text="做一个日报工具", user_id="u-7"))["task_id"]
    docs = {"task_contract": write_document(kernel_settings, "p1", str(task_id), "r1", "task_contract",
                                            {"markdown": CONTRACT}, "pm")}
    async with kernel.db.tx() as c:
        await c.execute("UPDATE core.tasks SET documents=%s::jsonb WHERE task_id=%s", (json.dumps(docs, default=str), task_id))
    await sync_vaults(kernel, kernel_settings)
    assert not (project_files / "knowledge" / "user-profile.md").exists()          # still running: nothing yet
    await kernel.cancel_task(task_id)
    await sync_vaults(kernel, kernel_settings)
    await sync_vaults(kernel, kernel_settings)                                        # once only
    profile = (project_files / "knowledge" / "user-profile.md").read_text("utf-8")
    assert profile.count("喜欢一次只答一个问题") == 1 and "需求未获批" in profile and "用户 u-7" in profile
    charter = project_files / "knowledge" / "charter.md"
    assert not charter.exists() or "约 20 家门店" not in charter.read_text("utf-8")  # unconfirmed facts stay out
    assert log(project_files)[0].startswith("存档：未获批任务的协作观察")              # committed on its own


async def test_supervisor_records_what_the_user_states_and_sees_the_ledger(kernel, sim, dispatcher, fake_harness,
                                                                          kernel_settings, project_files):
    task_id = (await kernel.receive("p1", role="pm", text="帮我做个日报工具", user_id="u-7"))["task_id"]
    fake_harness.script("supervisor", {"decision": "propose_next", "target_stage": "requirements",
                                       "instruction": "梳理需求", "message": "先梳理需求"})
    await settle(dispatcher)
    assert "## 任务状态簿" in fake_harness.calls[-1].prompt
    await kernel.act("p1", (await sim.item("p1", task_id))["item_id"], "execute")
    fake_harness.script("pm", {"outcome": "needs_input", "summary": "要确认读者", "document": None,
                               "questions": [{"id": "q1", "text": "日报给谁看？"}]})
    fake_harness.script("supervisor", {"decision": "relay_pm_questions", "message": "请回答",
                                       "questions": [{"id": "c1", "text": "日报给谁看？", "covers": ["q1"]}]})
    await settle(dispatcher)
    await kernel.act("p1", (await sim.item("p1", task_id))["item_id"], "submit", {"answers": {"c1": "门店店长们"}})
    fake_harness.script("pm", {"outcome": "completed", "summary": "需求完成", "questions": [],
                               "document": {"kind": "task_contract", "markdown": "# 需求\n- 日报"}})
    fake_harness.script("supervisor", {"decision": "ask_user", "message": "还有吗？",
                                       "questions": [{"id": "c2", "text": "还有别的要求吗？"}]})
    await settle(dispatcher)
    assert "问：日报给谁看？ → 答：门店店长们" in fake_harness.calls[-1].prompt   # Supervisor sees earlier answers

    await kernel.receive("p1", role="supervisor", text="以后每次只问我一个问题", user_id="u-7")
    fake_harness.script("supervisor", {"reply": "好的，以后每次只问一个。",
                                       "profile_observations": ["提问方式 · 每次只问一个问题 · 原话“以后每次只问我一个问题” · [用户]"]})
    await settle(dispatcher)
    async with kernel.db.tx() as c:
        texts = [r["text"] for r in await (await c.execute(
            "SELECT body->>'text' AS text FROM core.messages WHERE direction='outbound' ORDER BY created_at")).fetchall()]
    assert any(t.startswith("📝 已记入用户画像：提问方式 · 每次只问一个问题") for t in texts)
    await sync_vaults(kernel, kernel_settings)
    await sync_vaults(kernel, kernel_settings)
    profile = (project_files / "knowledge" / "user-profile.md").read_text("utf-8")
    assert profile.count("每次只问一个问题 · 原话") == 1 and "[Supervisor·对话]" in profile and "用户 u-7" in profile


async def test_triage_sees_the_profile(kernel_settings, project_files, fake_harness):
    kernel = Kernel(kernel_settings, retry_backoff_seconds=0)
    await kernel.ensure_project("p1")
    (project_files / "knowledge").mkdir(exist_ok=True)
    (project_files / "knowledge" / "user-profile.md").write_text("# 用户画像\n\n## 用户 u-7\n- 喜欢选择题\n", "utf-8")
    scripts = Scripts()
    d = Dispatcher(kernel, {**scripts.executors(), "triage": execute_triage},
                   services={"settings": kernel_settings, "harness": fake_harness})
    await kernel.receive("p1", role="pm", text="你好")
    fake_harness.script("triage", {"decision": "chat", "reply": "你好"})
    await settle(d)
    assert "喜欢选择题" in fake_harness.calls[-1].prompt


def test_other_projects_public_facts_become_candidates(kernel_settings):
    vaults = kernel_settings.data_root / "vaults"
    for project, archive in (("p2", "- 公共：公司在上海，约 20 家门店。[用户，2026-10-16]\n- 本项目只做日报。"),
                             ("p3", "- 公共：部署在远程主机 prod-1。")):
        (vaults / project / "knowledge").mkdir(parents=True, exist_ok=True)
        (vaults / project / "knowledge" / "charter.md").write_text(f"# 章程\n\n## 9 背景信息存档\n\n{archive}\n", "utf-8")
    shared = vaults / "kernel" / "knowledge" / "shared"
    shared.mkdir(parents=True, exist_ok=True)
    (shared / "environment.md").write_text("---\nname: environment\n---\n# 环境\n\n部署在远程主机 prod-1。\n", "utf-8")
    found = shared_candidates(kernel_settings, ["p1", "p2", "p3"], exclude="p1")
    assert "p2：公司在上海，约 20 家门店" in found and "只做日报" not in found
    assert "prod-1" not in found                                                       # already in the shared files
