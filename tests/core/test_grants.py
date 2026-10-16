"""ROUND4 R4.4: directory grants — checks, card approval, /grants, the change stream."""
from __future__ import annotations

import pytest

from assistant_kernel.core import grants

from .sim import next_step
from .test_gateway import gateway  # noqa: F401  (a fixture)


@pytest.fixture
def root(tmp_path, monkeypatch, kernel_settings):
    monkeypatch.setenv("CORE_GRANT_ROOT", str(tmp_path))
    (tmp_path / "crypto" / "strategies").mkdir(parents=True)
    return tmp_path


def test_directory_checks(root, kernel_settings):
    ok = grants.check_directory({"path": str(root / "crypto"), "access": "write", "purpose": "改策略"}, "p1",
                                kernel_settings.data_root)
    assert ok == {"path": str((root / "crypto").resolve()), "access": "write", "purpose": "改策略", "key": "crypto"}
    bad = [
        {"path": str(root / "nope"), "purpose": "x"},                               # does not exist
        {"path": "/etc", "purpose": "x"},                                           # outside the visible root
        {"path": "D:\\data", "purpose": "x"},                                       # not under H:\AI
        {"path": str(root / "crypto"), "access": "admin", "purpose": "x"},          # unknown access
        {"path": str(root / "crypto")},                                             # no purpose
    ]
    (kernel_settings.data_root / "vaults" / "p2").mkdir(parents=True, exist_ok=True)
    (kernel_settings.data_root / "runtime").mkdir(parents=True, exist_ok=True)
    if kernel_settings.data_root.resolve().is_relative_to(root.resolve()):
        bad += [{"path": str(kernel_settings.data_root / "vaults" / "p2"), "purpose": "x"},   # another project's vault
                {"path": str(kernel_settings.data_root / "runtime"), "purpose": "x"}]          # platform directory
    for raw in bad:
        with pytest.raises(ValueError):
            grants.check_directory(raw, "p1", kernel_settings.data_root)


async def test_grant_card_from_consultation_then_revoke(kernel, sim, root, kernel_settings, gateway):  # noqa: F811
    directory = grants.check_directory({"path": str(root / "crypto"), "access": "read", "purpose": "读策略"}, "p1",
                                       kernel_settings.data_root)
    await kernel.receive("p1", role="supervisor", text="把 crypto 目录开放给项目")
    sim.script("supervisor:consult", {"reply": "好的，请在卡片上确认。", "_grant": directory})
    await sim.drain()
    item = await sim.item("p1")
    assert item["content"]["decision"] == "grant"
    await kernel.act("p1", item["item_id"], "approve")
    assert [g["resource_key"] for g in await kernel.grants("p1")] == ["crypto"]
    record = (await kernel.iterations("p1"))[0]
    assert record["kind"] == "grant" and record["significance"] == "major" and "crypto" in record["title"]

    assert "crypto" in await gateway.grants_command("p1", [])
    assert "确认" in await gateway.grants_command("p1", ["revoke", "crypto"])          # asks first
    assert await kernel.grants("p1")
    assert "已撤销" in await gateway.grants_command("p1", ["revoke", "crypto", "确认"])
    assert await kernel.grants("p1") == []
    assert [r["title"] for r in await kernel.iterations("p1")][0] == "撤销目录授权 crypto"


async def test_supervisor_proposes_a_grant_during_a_task(kernel, sim, root, kernel_settings):
    directory = grants.check_directory({"path": str(root / "crypto"), "access": "read", "purpose": "读策略"}, "p1",
                                       kernel_settings.data_root)
    task_id = (await kernel.receive("p1", role="pm", text="分析策略"))["task_id"]
    sim.script("supervisor:evaluate", {"decision": "propose_grant", "message": "需要读策略目录", "_grant": directory},
               next_step("requirements"))
    await sim.drain()
    item = await sim.item("p1", task_id)
    assert item["content"]["decision"] == "grant"
    await kernel.act("p1", item["item_id"], "approve")
    await sim.drain()
    assert (await sim.task(task_id))["state"] == "awaiting_user"                    # back on track with a next step
    assert [g["resource_key"] for g in await kernel.grants("p1")] == ["crypto"]


async def test_an_unchecked_grant_is_refused(kernel, sim):
    task_id = (await kernel.receive("p1", role="pm", text="分析策略"))["task_id"]
    bad = {"decision": "propose_grant", "_grant_error": "目录不存在"}
    sim.script("supervisor:evaluate", bad, bad, bad)
    await sim.drain()
    assert (await sim.item("p1", task_id))["content"]["decision"] == "run_failed"
