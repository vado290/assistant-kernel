"""Round 2 / R2: triage of idle PM-channel messages, routing between projects, and template refresh."""
from __future__ import annotations

import pytest

from assistant_kernel.core import rules, skills
from assistant_kernel.core.dispatch import Dispatcher
from assistant_kernel.core.executors.triage import execute_triage
from assistant_kernel.core.kernel import Kernel

from .sim import Sim
from .test_dispatch import Scripts, settle


@pytest.fixture
async def triaging(kernel_settings):
    k = Kernel(kernel_settings, retry_backoff_seconds=0)          # production default: triage on
    for project in ("p1", "p2"):
        await k.ensure_project(project)
    return k, Sim(k)


async def outbound(kernel, project):
    async with kernel.db.tx() as c:
        return list(await (await c.execute(
            "SELECT * FROM core.messages WHERE project_id=%s AND direction='outbound' ORDER BY created_at",
            (project,))).fetchall())


async def tasks(kernel):
    async with kernel.db.tx() as c:
        return list(await (await c.execute("SELECT * FROM core.tasks ORDER BY created_at")).fetchall())


async def test_chat_is_answered_without_a_task(triaging):
    kernel, sim = triaging
    assert (await kernel.receive("p1", role="pm", text="你好，现在有哪些 skill？"))["action"] == "triage"
    sim.script("triage", {"decision": "chat", "reply": "你好！目前有需求分析、方案设计等技能。"})
    await sim.drain()
    assert await tasks(kernel) == []
    assert (await outbound(kernel, "p1"))[-1]["body"]["text"].startswith("你好！")


async def test_work_starts_a_task_with_goal_and_reason(triaging):
    kernel, sim = triaging
    await kernel.receive("p1", role="pm", text="帮我把日报改成周报吧")
    sim.script("triage", {"decision": "start_task", "goal": "把日报改成周报", "reason": "用户要求按周汇总",
                          "reply": "好的，开始处理。"})
    sim.script("supervisor:evaluate", {"decision": "ask_user", "questions": [{"id": "c1", "text": "周报给谁看？"}]})
    await sim.drain()
    [task] = await tasks(kernel)
    assert (task["goal"], task["reason"], task["origin"], task["track"]) == ("把日报改成周报", "用户要求按周汇总", "user", "full")
    assert task["state"] == "awaiting_user"
    # while a task is active, PM-channel messages are supplements again (decision 12)
    assert (await kernel.receive("p1", role="pm", text="周日晚上发"))["action"] == "attached"


async def test_route_hands_over_to_the_other_supervisor(triaging):
    kernel, sim = triaging
    await kernel.receive("p1", role="pm", text="帮我看看交易机器人的风控规则")
    sim.script("triage", {"decision": "route", "reply": "这是 p2 负责的事，已转交。", "_capabilities": ["routing"],
                          "route": {"project_id": "p2", "handoff": "用户原话：帮我看看交易机器人的风控规则"}})
    sim.script("supervisor:consult", {"reply": "收到转交，我先看一下现有风控规则。"})
    await sim.drain()
    assert await tasks(kernel) == []
    note, answer = await outbound(kernel, "p2")
    assert note["body"]["text"].startswith("📨 Handed over from p1:") and answer["body"]["text"].startswith("收到转交")
    reply = (await outbound(kernel, "p1"))[-1]
    assert reply["body"]["link_message"] == str(note["message_id"])


async def test_route_needs_the_capability_and_a_real_target(triaging):
    kernel, sim = triaging
    await kernel.receive("p1", role="pm", text="转给别人")
    bad = [{"decision": "route", "reply": "转交", "route": {"project_id": "p2", "handoff": "x"}},   # no capability
           {"decision": "route", "reply": "转交", "_capabilities": ["routing"],
            "route": {"project_id": "nope", "handoff": "x"}},
           {"decision": "route", "reply": "转交", "_capabilities": ["routing"],
            "route": {"project_id": "p1", "handoff": "x"}}]
    sim.script("triage", *bad)
    await sim.drain()
    assert await tasks(kernel) == [] and await outbound(kernel, "p2") == []
    assert "cannot be handled right now" in (await outbound(kernel, "p1"))[-1]["body"]["text"]


async def test_consult_can_route_too(triaging):
    kernel, sim = triaging
    await kernel.receive("p1", role="supervisor", text="p2 的部署情况怎么样")
    sim.script("supervisor:consult", {"reply": "这要问 p2，已转交。", "_capabilities": ["routing"],
                                      "route": {"project_id": "p2", "handoff": "用户想了解部署情况"}},
               {"reply": "p2：部署正常。"})
    await sim.drain()
    reply = (await outbound(kernel, "p1"))[-1]
    assert reply["role"] == "supervisor" and reply["body"]["link_message"]


async def test_triage_prompt_has_catalog_and_contract(kernel_settings, project_files, fake_harness):
    kernel = Kernel(kernel_settings, retry_backoff_seconds=0)
    await kernel.ensure_project("p1")
    await kernel.ensure_project("p2")
    (project_files / "project.yaml").write_text(
        "schema_version: project.v2\nproject_id: p1\nlayout_version: 2\ncapabilities: [routing]\n", "utf-8")
    other = kernel_settings.data_root / "vaults" / "p2" / "knowledge"
    other.mkdir(parents=True)
    (other / "charter.md").write_text("# 交易\n\n## 1 项目用途\n交易与风控\n\n## 7 路由信息\n- 别名：交易机器人\n", "utf-8")
    scripts = Scripts()
    d = Dispatcher(kernel, {**scripts.executors(), "triage": execute_triage},
                   services={"settings": kernel_settings, "harness": fake_harness})
    await kernel.receive("p1", role="pm", text="交易机器人那边怎么样了")
    fake_harness.script("triage", {"decision": "chat", "reply": "我去问问"})
    await settle(d)
    prompt = fake_harness.calls[-1].prompt
    assert "PM triage" in prompt and "Project `p2`" in prompt and "交易机器人" in prompt and "route" in prompt
    assert "Skill assistant-triage-routing" in prompt and "Skill assistant-requirements" not in prompt


def test_quick_track_rules():
    task = {"stage": "requirements", "track": "quick", "documents": {"task_contract": {"sha256": "r1"}}, "approvals": {}}
    assert rules.move_error(task, "design") and not rules.move_error(task, "build", approving=("requirements",))
    assert rules.approvals_needed(task, "build") == ["requirements"]
    build = {**task, "stage": "build"}
    assert rules.move_error(build, "release")                       # nothing validated yet
    build["documents"] = {"implementation_report": {"bundle_sha": "b1"}, "validation": {"passed": True, "bundle_sha": "b1"}}
    assert not rules.move_error(build, "release") and rules.move_error({**build, "track": "full"}, "release")


def test_refresh_replaces_only_unmodified_copies(tmp_path, monkeypatch):
    import shutil
    templates = tmp_path / "templates"
    shutil.copytree(skills.TEMPLATE_SKILLS, templates)
    monkeypatch.setattr(skills, "TEMPLATE_SKILLS", templates)
    vault = tmp_path / "vault"
    skills.install_templates(vault, frozenset())
    edited = vault / "agents/pm/.agents/skills/assistant-operation/SKILL.md"
    edited.write_text(edited.read_text("utf-8") + "\n本项目补充：每周一汇报。\n", "utf-8")
    for name in ("assistant-operation", "assistant-requirements"):   # upgrade templates, leaving installed baselines intact
        path = templates / name / "SKILL.md"
        path.write_text(path.read_text("utf-8").replace('\n  version: "', '\n  version: "99.', 1), "utf-8")
    refreshed, kept = skills.refresh_unmodified(vault)
    assert refreshed == ["assistant-requirements"] and kept == ["assistant-operation"]
    assert "每周一汇报" in edited.read_text("utf-8")
    assert skills.refresh_unmodified(vault) == ([], ["assistant-operation"])


@pytest.fixture
def skill_upgrade(tmp_path, monkeypatch):
    templates = tmp_path / "templates"
    source = templates / "example" / "SKILL.md"
    source.parent.mkdir(parents=True)
    source.write_text('---\nname: example\ndescription: original purpose\nmetadata:\n'
                      '  version: "1.0.0"\n  load: {roles: [pm], stages: [build]}\n---\n\nOriginal method.\n', "utf-8")
    monkeypatch.setattr(skills, "TEMPLATE_SKILLS", templates)
    vault = tmp_path / "vault"
    skills.install_templates(vault, frozenset())
    copy = skills.agent_skill_dir(vault, "pm") / "example" / "SKILL.md"
    source.write_text(source.read_text("utf-8").replace('"1.0.0"', '"2.0.0"').replace('Original method.', 'Improved method.'), "utf-8")
    return vault, source, copy


@pytest.mark.parametrize("customization", ["description", "load", "body", "io", "extra_metadata", "none"])
def test_refresh_preserves_custom_metadata_and_body(skill_upgrade, customization):
    vault, source, copy = skill_upgrade
    text = copy.read_text("utf-8")
    if customization == "description":
        text = text.replace("description: original purpose", "description: project-specific purpose")
    elif customization == "load":
        text = text.replace("stages: [build]", "stages: [design], on_demand: true")
    elif customization == "body":
        text += "\nProject-specific method.\n"
    elif customization == "io":
        text = text.replace("metadata:\n", "metadata:\n  io: {reads: [project_data]}\n")
    elif customization == "extra_metadata":
        text = text.replace("metadata:\n", "metadata:\n  local_note: keep this project preference\n")
    copy.write_text(text, "utf-8")
    before = copy.read_bytes()
    result = skills.refresh_unmodified(vault)
    if customization != "none":
        assert result == ([], ["example"])
        assert copy.read_bytes() == before
        assert skills.refresh_unmodified(vault) == ([], ["example"])
    else:
        assert result == (["example"], [])
        assert skills.parse(copy).version == "2.0.0" and skills.parse(copy).body == "Improved method."
        assert skills.refresh_unmodified(vault) == ([], [])
        source.write_text(source.read_text("utf-8").replace('"2.0.0"', '"3.0.0"'), "utf-8")
        assert skills.refresh_unmodified(vault) == (["example"], [])  # replacement saved a usable new baseline
        assert skills.parse(copy).version == "3.0.0"


@pytest.mark.parametrize("baseline", ["body_only", "missing", "legacy_known", "invalid_content_hash"])
def test_refresh_keeps_legacy_copies_without_reliable_metadata_baseline(skill_upgrade, baseline):
    import re
    vault, _, copy = skill_upgrade
    text = copy.read_text("utf-8")
    if baseline == "missing":
        text = re.sub(r"^  template:.*\n", "", text, flags=re.M)
    elif baseline == "invalid_content_hash":
        text = re.sub(r'content_sha: "[0-9a-f]+"', 'content_sha: "unknown"', text)
    else:
        text = re.sub(r', content_sha: "[0-9a-f]+"', '', text)
    copy.write_text(text, "utf-8")
    before = copy.read_bytes()
    known = {"example": skills.body_sha(skills.parse(copy).body)} if baseline == "legacy_known" else None
    assert skills.refresh_unmodified(vault, known) == ([], ["example"])
    assert copy.read_bytes() == before  # do not stamp the current custom metadata as a trusted old baseline
