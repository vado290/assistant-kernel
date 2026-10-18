"""ROUND5 R5.1: each agent's own skill folder and working directory, Codex's bundled skills off, the folder rule,
and tasks that wait too long for the user."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from assistant_kernel.core import skills
from assistant_kernel.core.executors import common
from assistant_kernel.harness.codex_cli import bundled_skills_off

from .sim import next_step
from .test_scenarios import start_task


def test_templates_go_to_each_agents_own_folder(tmp_path):
    installed = skills.install_templates(tmp_path, frozenset())
    assert "pm/assistant-requirements" in installed and "supervisor/assistant-goal-supervisor" in installed
    assert "supervisor/assistant-requirements" not in installed                 # load.roles decides the agents
    assert "pm/assistant-user-questions" in installed and "supervisor/assistant-user-questions" in installed
    assert not (tmp_path / ".agents").exists()
    names = {s.name for s in skills.project_skills(tmp_path, "supervisor")}
    assert "assistant-goal-supervisor" in names and "assistant-requirements" not in names
    assert skills.layout_problems(tmp_path) == []


def test_folder_rule(tmp_path):
    skills.install_templates(tmp_path, frozenset())
    assert skills.layout_problems(tmp_path, ["agents/pm/.agents/skills/x/SKILL.md",
                                             "product/agents/analyst/.agents/skills/y/SKILL.md"]) == []
    problems = skills.layout_problems(tmp_path, [".agents/skills/x/SKILL.md", "product/AGENTS.md"])
    assert len(problems) == 2
    (tmp_path / ".agents" / "skills" / "x").mkdir(parents=True)
    (tmp_path / "agents" / "AGENTS.md").write_text("x", "utf-8")
    (tmp_path / "archive" / "pre-r5" / ".agents" / "skills").mkdir(parents=True)   # archived: ignored
    found = skills.layout_problems(tmp_path)
    assert any(p.startswith(".agents/skills") for p in found) and any("agents/AGENTS.md" in p for p in found)
    assert len(found) == 2


def test_bundled_skills_are_switched_off(tmp_path):
    assert bundled_skills_off(tmp_path) == []
    for name in ("imagegen", "skill-installer"):
        (tmp_path / "skills" / ".system" / name).mkdir(parents=True)
        (tmp_path / "skills" / ".system" / name / "SKILL.md").write_text("x", "utf-8")
    flag, value = bundled_skills_off(tmp_path)
    assert flag == "-c" and value.startswith("skills.config=[") and value.count("enabled=false") == 2
    assert "imagegen/SKILL.md" in value and "skill-installer/SKILL.md" in value


async def test_an_agent_works_in_its_own_folder(kernel_settings, project_files):
    seen = {}

    class Harness:
        async def run_turn(self, request):
            seen["cwd"] = request.cwd
            return SimpleNamespace(final_text="{}")

    config = SimpleNamespace(agent_id="supervisor", allowed_tools=(), model=None, reasoning_effort=None, provider=None,
                             context_policy=None, model_context_window_tokens=None, model_auto_compact_token_limit=None,
                             timeout_seconds=60)
    ctx = SimpleNamespace(services={"settings": kernel_settings, "harness": Harness()}, cancel=asyncio.Event())
    await common.call_model(ctx, {"project_id": "p1", "run_id": "r1", "attempt": 1}, config, "x", task_id=None,
                            stage=None, session_id=None, tools=frozenset(), sandbox="read-only")
    assert seen["cwd"] == project_files / "agents" / "supervisor"


async def test_a_task_waiting_too_long_ends_on_its_own(kernel, sim):
    task_id = await start_task(kernel, sim)
    sim.script("supervisor:evaluate", next_step("requirements"))
    await sim.drain()
    assert await kernel.expire_idle_tasks() == []                               # still fresh
    async with kernel.db.tx() as c:
        await c.execute("UPDATE core.tasks SET updated_at = now() - interval '8 days' WHERE task_id=%s", (task_id,))
        await c.execute("UPDATE core.messages SET created_at = now() - interval '8 days' WHERE project_id='p1'")
    assert await kernel.expire_idle_tasks() == [str(task_id)]
    task = await sim.task(task_id)
    assert (task["state"], task["close_reason"]) == ("cancelled", "expired_no_response")
    assert await sim.item("p1", task_id) is None
    assert any("超过 7 天" in m["body"]["text"] for m in await sim.outbound("p1"))
