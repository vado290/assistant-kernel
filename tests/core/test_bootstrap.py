"""R3: a project is created through a task. The PM agrees a charter with the user (the creation task's requirements
document, ending with YAML creation parameters); approving the charter makes Kernel create the project from it."""
from __future__ import annotations

import pytest
import yaml

from assistant_kernel.core import rules, skills
from assistant_kernel.core.cards import StaleItem
from assistant_kernel.core.dispatch import Dispatcher
from assistant_kernel.core.executors.bootstrap import execute_bootstrap, write_project_files
from assistant_kernel.core.executors.common import load_agent, write_document
from assistant_kernel.core.vault_sync import sync_vaults

from .sim import next_step
from .test_dispatch import Scripts, settle

CHARTER = """# 销售日报 · 项目章程

## 1 项目用途
每天汇总门店销售数据并生成日报。

## 2 业务目标与成功标准
店长每天 9 点前收到日报。

## 5 范围
- 负责：日报
- 不负责：财务结算

## 7 路由信息
- 典型请求：今天的日报出来了吗

## 9 背景信息存档
- 20 家门店（用户 2026-10-16）

## 创建参数
```yaml
project_id: sales-report
display_name: 销售日报
purpose: 每天汇总门店销售数据并生成日报
capabilities: []
delivery: vault-files
share_with_kernel: improvements
pm_role: |
  日报数字必须注明数据来源。
```

## 用户画像观察
- 决策风格 · 直接接受推荐 · 两轮都选推荐项 · [推断]

## 修订记录
- 初稿
"""


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


def charter_turn(settings, task_id, markdown=CHARTER):
    """A scripted PM turn that saves the charter the way the PM executor does (with Kernel's parameter check)."""
    report = write_document(settings, "kernel", str(task_id), "r1", "task_contract", {"markdown": markdown}, "pm")
    try:
        report["spec"] = rules.charter_spec(markdown)
    except ValueError as exc:
        report["spec_error"] = str(exc)
    brief = write_document(settings, "kernel", str(task_id), "r1", "domain_brief", {"markdown": "## 1 行业速写\n便利店"}, "pm")
    return {"outcome": "completed", "summary": "章程已写好", "report": report, "free_documents": [brief]}


CAPS = {"_capabilities": ["project_management"]}


async def test_project_is_created_from_the_approved_charter(kernel, sim, dispatcher, kernel_settings, management):
    task = await kernel.create_task("kernel", goal="创建销售日报项目", origin="user", kind="bootstrap")
    dispatcher.scripts.add("supervisor:evaluate", next_step("requirements", "按章程技能与用户商定章程"))
    await settle(dispatcher)
    await kernel.act("kernel", (await sim.item("kernel", task["task_id"]))["item_id"], "execute")
    dispatcher.scripts.add("pm_turn", charter_turn(kernel_settings, task["task_id"]))
    dispatcher.scripts.add("supervisor:evaluate", {"decision": "propose_project", "message": "章程已商定", **CAPS})
    await settle(dispatcher)
    item = await sim.item("kernel", task["task_id"])
    assert item["content"]["decision"] == "create_project"
    assert item["content"]["data"]["spec"]["project_id"] == "sales-report"

    await kernel.act("kernel", item["item_id"], "approve")
    await settle(dispatcher)
    done = await sim.task(task["task_id"])
    assert done["state"] == "completed" and rules.approval_valid(done, "requirements")
    vault = kernel_settings.data_root / "vaults" / "sales-report"
    charter = (vault / "knowledge" / "charter.md").read_text("utf-8")
    assert charter.startswith("# 销售日报 · 项目章程") and "每天汇总门店销售数据" in charter
    assert "创建参数" not in charter and "用户画像观察" not in charter and "修订记录" not in charter
    assert "创建项目（章程经用户批准）" in skills.section_text(charter, "变更记录")
    assert "20 家门店" in skills.section_text(charter, "背景信息存档")
    config = yaml.safe_load((vault / "project.yaml").read_text("utf-8"))
    assert config["delivery"] == "vault-files" and config["capabilities"] == [] and config["created_by"] == "kernel-core-bootstrap"
    assert "日报数字必须注明数据来源" in (vault / "agents" / "pm" / "role.md").read_text("utf-8")
    assert "便利店" in (vault / "knowledge" / "domain-brief.md").read_text("utf-8")
    assert "直接接受推荐" in (vault / "knowledge" / "user-profile.md").read_text("utf-8")
    assert (vault / ".agents" / "skills" / "assistant-requirements" / "SKILL.md").is_file()
    assert yaml.safe_load(management.read_text("utf-8"))["projects"]["sales-report"]["pm_channel_id"] == "pm1"
    notices = [m["body"]["text"] for m in await sim.outbound("kernel")]
    assert any("🆕 项目 sales-report 已创建" in n and "/g1/pm1" in n for n in notices)
    # the charter is not archived into the creating project's own charter
    await sync_vaults(kernel, kernel_settings)
    assert not (kernel_settings.data_root / "vaults" / "kernel" / "knowledge" / "user-profile.md").exists()
    for stage in ("requirements", "design", "build", "review", "operate", "evolution"):
        assert load_agent(kernel_settings, "sales-report", "pm", stage=stage).agent_key == "project:sales-report:manager"


async def test_a_changed_charter_makes_the_card_stale(kernel, sim, dispatcher, kernel_settings):
    task = await kernel.create_task("kernel", goal="创建销售日报项目", origin="user", kind="bootstrap")
    dispatcher.scripts.add("supervisor:evaluate", next_step("requirements"))
    await settle(dispatcher)
    await kernel.act("kernel", (await sim.item("kernel", task["task_id"]))["item_id"], "execute")
    dispatcher.scripts.add("pm_turn", charter_turn(kernel_settings, task["task_id"]))
    dispatcher.scripts.add("supervisor:evaluate", {"decision": "propose_project", **CAPS})
    await settle(dispatcher)
    item = await sim.item("kernel", task["task_id"])
    async with kernel.db.tx() as c:
        await c.execute("""UPDATE core.tasks SET documents = jsonb_set(documents, '{task_contract,sha256}', '"other"')
                           WHERE task_id=%s""", (task["task_id"],))
    with pytest.raises(StaleItem):
        await kernel.act("kernel", item["item_id"], "approve")


async def test_creation_tasks_stay_in_requirements_and_need_valid_parameters(kernel, sim, dispatcher, kernel_settings):
    task = await kernel.create_task("kernel", goal="创建项目", origin="user", kind="bootstrap")
    assert rules.move_error(await sim.task(task["task_id"]), "design")
    dispatcher.scripts.add("supervisor:evaluate", next_step("requirements"))
    await settle(dispatcher)
    await kernel.act("kernel", (await sim.item("kernel", task["task_id"]))["item_id"], "execute")
    broken = CHARTER.replace("project_id: sales-report", "project_id: Sales Report")
    dispatcher.scripts.add("pm_turn", charter_turn(kernel_settings, task["task_id"], broken))
    bad = {"decision": "propose_project", **CAPS}
    dispatcher.scripts.add("supervisor:evaluate", bad, bad, bad)
    await settle(dispatcher)
    assert (await sim.task(task["task_id"]))["documents"]["task_contract"]["spec_error"].startswith("project_id")
    assert (await sim.item("kernel", task["task_id"]))["content"]["decision"] == "run_failed"


async def test_an_ordinary_task_can_turn_into_a_creation_task(kernel, sim, dispatcher):
    task_id = (await kernel.receive("kernel", role="pm", text="帮我建一个销售日报项目"))["task_id"]
    dispatcher.scripts.add("supervisor:evaluate", {"decision": "propose_project", "goal": "创建销售日报项目",
                                                    "reason": "用户要求", **CAPS})
    await settle(dispatcher)
    item = await sim.item("kernel", task_id)
    assert item["content"]["decision"] == "project_task"
    await kernel.act("kernel", item["item_id"], "approve")
    assert (await sim.task(task_id))["close_reason"] == "converted_to_project_task"
    created = await kernel.active_task("kernel")
    assert created["kind"] == "bootstrap" and created["goal"] == "创建销售日报项目"


async def test_only_projects_with_the_capability_create_projects(kernel, sim, dispatcher):
    task_id = (await kernel.receive("p1", role="pm", text="建个项目"))["task_id"]
    no_caps = {"decision": "propose_project", "goal": "创建项目"}
    dispatcher.scripts.add("supervisor:evaluate", no_caps, no_caps, no_caps)
    await settle(dispatcher)
    assert (await sim.item("p1", task_id))["content"]["decision"] == "run_failed"


def test_charter_parameters():
    spec = rules.charter_spec(CHARTER)
    assert spec["project_id"] == "sales-report" and spec["share_with_kernel"] == "improvements"
    assert "注明数据来源" in spec["pm_role"]
    for bad in (CHARTER.replace("project_id: sales-report", "project_id: x"),
                CHARTER.replace("capabilities: []", "capabilities: [kernel_maintenance]"),
                CHARTER.split("## 创建参数")[0]):
        with pytest.raises(ValueError):
            rules.charter_spec(bad)


def test_an_existing_vault_is_never_overwritten(kernel_settings):
    (kernel_settings.data_root / "vaults" / "taken" / "agents").mkdir(parents=True)
    spec = rules.charter_spec(CHARTER)
    with pytest.raises(PermissionError):
        write_project_files(kernel_settings, {**spec, "project_id": "taken"})
    fresh = {**spec, "project_id": "fresh-one"}
    first = write_project_files(kernel_settings, fresh)
    assert first[0] == "project.yaml" and write_project_files(kernel_settings, fresh) == []   # repeatable
