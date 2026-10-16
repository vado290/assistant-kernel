"""Phase 5: the kernel Supervisor proposes a project, the user approves, Kernel creates it deterministically."""
from __future__ import annotations

import pytest
import yaml

from assistant_kernel.core import rules
from assistant_kernel.core.dispatch import Dispatcher
from assistant_kernel.core.executors.bootstrap import execute_bootstrap, write_project_files
from assistant_kernel.core.executors.common import load_agent

from .test_dispatch import Scripts, settle

SPEC = {"project_id": "sales-report", "display_name": "销售日报", "purpose": "每天汇总销售数据并生成日报",
        "business_profile": {"industry": "零售", "organization": "某连锁店", "users": "店长"}}


class FakeChannels:
    guild_id = "g1"

    def __init__(self):
        self.calls = []

    async def ensure(self, project_id, display_name, purpose):
        self.calls.append(project_id)
        return {"category_id": "c1", "pm_channel_id": "pm1", "supervisor_channel_id": "sup1"}


@pytest.fixture
def management(kernel_settings):
    config = kernel_settings.data_root / "config" / "core-discord.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text('shared_bot: true\nguild_id: "g1"\nprojects: {}\n', "utf-8")
    return config


@pytest.fixture
async def dispatcher(kernel, kernel_settings, management):
    await kernel.ensure_project("kernel")
    scripts, channels = Scripts(), FakeChannels()
    d = Dispatcher(kernel, {**scripts.executors(), "bootstrap": execute_bootstrap},
                   services={"settings": kernel_settings, "discord_channels": channels})
    d.scripts, d.channels = scripts, channels
    return d


async def test_project_is_created_after_approval(kernel, sim, dispatcher, kernel_settings, management):
    task_id = (await kernel.receive("kernel", role="pm", text="帮我建一个销售日报项目"))["task_id"]
    # the Supervisor executor adds the project's capabilities after the model reply
    dispatcher.scripts.add("supervisor:evaluate", {"decision": "propose_project", "message": "建议这样创建", "spec": SPEC,
                                                    "_capabilities": ["project_management"]})
    await settle(dispatcher)
    item = await sim.item("kernel", task_id)
    assert item["content"]["decision"] == "create_project" and item["content"]["data"]["spec"]["project_id"] == "sales-report"

    await kernel.act("kernel", item["item_id"], "approve")
    await settle(dispatcher)
    assert (await sim.task(task_id))["state"] == "completed"
    vault = kernel_settings.data_root / "vaults" / "sales-report"
    assert "零售" in (vault / "knowledge" / "charter.md").read_text("utf-8")
    copied = (vault / ".agents" / "skills" / "assistant-requirements" / "SKILL.md").read_text("utf-8")
    assert "template: {name: assistant-requirements" in copied           # the project owns a copy of the template
    assert not (vault / ".agents" / "skills" / "assistant-shared-context").exists()   # needs a capability it lacks
    routing = yaml.safe_load(management.read_text("utf-8"))["projects"]["sales-report"]
    assert routing == {"pm_channel_id": "pm1", "supervisor_channel_id": "sup1"}
    notices = [m["body"]["text"] for m in await sim.outbound("kernel")]
    assert any("🆕 项目 sales-report 已创建" in n and "/g1/pm1" in n for n in notices)
    async with kernel.db.tx() as c:
        assert await (await c.execute("SELECT 1 FROM core.projects WHERE project_id='sales-report'")).fetchone()

    # the created project's agent files load in every stage
    for stage in ("requirements", "design", "build", "review", "operate", "evolution"):
        config = load_agent(kernel_settings, "sales-report", "pm", stage=stage)
        assert config.agent_key == "project:sales-report:manager" and any(l.startswith("assistant-project-agent@") for l in config.skill_labels)
    supervisor = load_agent(kernel_settings, "sales-report", "supervisor", purpose="evaluate")
    assert "销售日报" in supervisor.role_text and any(l.startswith("assistant-goal-supervisor@") for l in supervisor.skill_labels)


async def test_only_the_management_project_can_propose_projects(kernel, sim, dispatcher):
    task_id = (await kernel.receive("p1", role="pm", text="建个项目"))["task_id"]
    dispatcher.scripts.add("supervisor:evaluate", {"decision": "propose_project", "spec": SPEC},
                           {"decision": "propose_project", "spec": SPEC}, {"decision": "propose_project", "spec": SPEC})
    await settle(dispatcher)
    assert (await sim.item("p1", task_id))["content"]["decision"] == "run_failed"


def test_spec_validation():
    assert rules.project_spec(SPEC)["business_profile"]["notes"] == "（未提供）"
    for bad in ({**SPEC, "project_id": "Sales Report"}, {**SPEC, "project_id": "x"}, {**SPEC, "purpose": ""}):
        with pytest.raises(ValueError):
            rules.project_spec(bad)


def test_an_existing_vault_is_never_overwritten(kernel_settings):
    (kernel_settings.data_root / "vaults" / "taken" / "agents").mkdir(parents=True)
    with pytest.raises(PermissionError):
        write_project_files(kernel_settings, rules.project_spec({**SPEC, "project_id": "taken"}))
    spec = rules.project_spec({**SPEC, "project_id": "fresh-one"})
    first = write_project_files(kernel_settings, spec)
    assert first[0] == "project.yaml" and write_project_files(kernel_settings, spec) == []   # repeatable
