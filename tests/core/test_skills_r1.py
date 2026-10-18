"""Round 2 / R1: skills by load rules, charter archive, shared context and its update card."""
from __future__ import annotations

import pytest

from assistant_kernel.core import skills
from assistant_kernel.core.executors.common import load_agent, write_document
from assistant_kernel.core.gateway import render
from assistant_kernel.core.vault_sync import sync_vaults


def test_load_rules():
    m = skills.matches
    assert m({"roles": ["pm"], "stages": ["build"]}, role="pm", stage="build")
    assert not m({"roles": ["pm"], "stages": ["build"]}, role="pm", stage="design")
    assert not m({"roles": ["pm"]}, role="supervisor", purpose="evaluate")
    # stages and purposes: either one is enough
    both = {"roles": ["pm", "supervisor"], "stages": ["design"], "purposes": ["evaluate"]}
    assert m(both, role="pm", stage="design") and m(both, role="supervisor", purpose="evaluate")
    assert not m(both, role="supervisor", purpose="consult")
    # capabilities and task kinds must hold
    assert not m({"capabilities": ["kernel_maintenance"]}, role="pm", stage="build")
    assert m({"capabilities": ["kernel_maintenance"]}, role="pm", stage="build", capabilities=frozenset({"kernel_maintenance"}))
    assert not m({"task_kinds": ["bootstrap"]}, role="pm", stage="requirements", task_kind="delivery")
    assert not m({"disabled": True}, role="pm")


def test_every_template_skill_parses_and_names_its_folder():
    names = [s.name for s in skills.template_skills()]
    assert len(names) == len(set(names)) and "assistant-requirements" in names
    for skill in skills.template_skills():
        assert skill.name == skill.path.parent.name and skill.version and skill.load, skill.name


def test_sections_ignore_numbering():
    text = "# 章程\n\n## 9 背景信息存档\n\n（占位）\n\n## 10 其他\n\nx\n"
    assert skills.section_text(text, "背景信息存档") == "（占位）"
    replaced = skills.replace_section(text, "背景信息存档", "新内容")
    assert "## 9 背景信息存档\n\n新内容\n\n## 10 其他" in replaced
    assert skills.replace_section(text, "新节", "y").endswith("## 新节\n\ny\n")


@pytest.fixture
def shared(kernel_settings):
    folder = kernel_settings.data_root / "vaults" / "kernel" / "knowledge" / "shared"
    folder.mkdir(parents=True)
    for name in skills.SHARED_FILES:
        (folder / name).write_text((skills.Path(skills.__file__).parent / "shared_template" / name).read_text("utf-8"),
                                   "utf-8")
    return folder


def test_each_turn_loads_matching_skills_and_shared_context(kernel_settings, project_files, shared):
    requirements = load_agent(kernel_settings, "p1", "pm", stage="requirements")
    assert "技能 assistant-requirements" in requirements.skills_text
    assert "技能 assistant-implementation" not in requirements.skills_text
    assert "assistant-history-retrieval" not in requirements.skills_text.split("## 公共背景")[0]   # Codex lists it
    assert "你的工作目录是 `agents/pm`" in requirements.skills_text
    in_worktree = load_agent(kernel_settings, "p1", "pm", stage="requirements", native=False)
    assert "assistant-history-retrieval：" in in_worktree.skills_text          # no native discovery: Kernel lists it
    assert "路径说明" not in in_worktree.skills_text
    assert "公共背景 · 组织与用户" in requirements.skills_text
    assert "公共背景 · 技术环境" not in requirements.skills_text               # environment is for design onwards
    build = load_agent(kernel_settings, "p1", "pm", stage="build")
    assert "公共背景 · 技术环境" in build.skills_text and "公共背景 · 工作约定" in build.skills_text
    assert "assistant-shared-context" not in " ".join(build.skill_labels)       # needs the shared_context capability
    assert requirements.config_sha256 != build.config_sha256


async def _approved_requirements(kernel, settings, markdown: str):
    task_id = (await kernel.receive("p1", role="pm", text="做一个日报工具"))["task_id"]
    doc = write_document(settings, "p1", str(task_id), str(task_id), "task_contract", {"markdown": markdown}, "pm")
    async with kernel.db.tx() as c:
        await c.execute("""UPDATE core.tasks SET documents=jsonb_build_object('task_contract', %s::jsonb),
                           approvals=jsonb_build_object('requirements', jsonb_build_object('doc_sha', %s::text))
                           WHERE task_id=%s""", (skills_json(doc), doc["sha256"], task_id))
    return task_id


def skills_json(value) -> str:
    import json
    return json.dumps(value, default=str)


async def test_approved_background_is_archived_into_the_charter_once(kernel, kernel_settings, project_files):
    charter = project_files / "knowledge" / "charter.md"
    charter.parent.mkdir(parents=True, exist_ok=True)
    charter.write_text("# 章程\n\n## 9 背景信息存档\n（需求文档获批时由 Kernel 追加）\n", "utf-8")
    await _approved_requirements(kernel, kernel_settings,
                                 "# 需求\n\n## 2 目标\n日报\n\n## 10 背景信息存档\n- 公司做连锁零售（用户 2026-10-16）\n")
    assert (await sync_vaults(kernel, kernel_settings))["archived"] == 1
    assert (await sync_vaults(kernel, kernel_settings))["archived"] == 0
    text = charter.read_text("utf-8")
    assert text.count("公司做连锁零售") == 1 and "需求文档获批时" not in text and "（任务 " in text


async def test_shared_update_card_then_write(kernel, sim, kernel_settings, shared):
    await kernel.receive("p1", role="supervisor", text="以后开发完的服务都部署到远程主机 prod-1")
    sim.script("supervisor:consult", {
        "reply": "好的，我整理成了公共背景的修改，请在卡片上确认。", "_capabilities": ["shared_context"],
        "propose_shared_update": {"file": "environment.md", "section": "7 部署去向", "before": "（旧）",
                                  "content": "- 其他项目部署到远程主机 prod-1。（用户 2026-10-16）", "reason": "用户在 Supervisor 频道说明"}})
    await sim.drain()
    item = await sim.item("p1")
    assert item["content"]["decision"] == "shared_update"
    shown = render.render_item(item)["text"]
    assert "现在的内容" in shown and "prod-1" in shown and "environment.md" in shown
    await kernel.act("p1", item["item_id"], "approve")
    assert (await sync_vaults(kernel, kernel_settings))["shared"] == 1
    assert (await sync_vaults(kernel, kernel_settings))["shared"] == 0
    text = (shared / "environment.md").read_text("utf-8")
    assert "部署到远程主机 prod-1" in skills.section_text(text, "部署去向")
    record = (await kernel.iterations("p1"))[0]                             # the reason is in the stream
    assert record["kind"] == "shared_context" and record["reason"] == "用户在 Supervisor 频道说明"
    notices = [m["body"]["text"] for m in await sim.outbound("p1")]
    assert any("公共背景已更新" in n for n in notices)


async def test_shared_update_needs_the_capability(kernel, sim, shared):
    await kernel.receive("p1", role="supervisor", text="记一下部署位置")
    sim.script("supervisor:consult", {"reply": "好的", "propose_shared_update": {
        "file": "environment.md", "section": "部署去向", "content": "x", "reason": "y"}})
    await sim.drain()
    assert await sim.item("p1") is None
