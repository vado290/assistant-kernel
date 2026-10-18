"""ROUND4 R4.1: the change stream — parsing, fallback records, queries, shared-context rows, effect reviews."""
from __future__ import annotations

import pytest

from assistant_kernel.core import iterlog, machine

GOOD = """# 实施报告

## 迭代记录
```yaml
title: 提问分组
reason: 用户说问题太散（复盘 P2）
significance: minor
changes:
  - {type: skill, target: assistant-requirements, action: modify, version: "3.2.0→3.3.0", note: 第 1 轮分组}
  - {type: map, target: knowledge/map.yaml, action: modify, note: 无}
verification: 模拟一轮
expected_effect: 需求阶段 2 轮以内
measure: 各阶段 PM 回合数
followups: 无
```
"""


def test_parse_record_and_derived_fields():
    record = iterlog.parse_record(GOOD)
    assert record["title"] == "提问分组" and record["changes"][0]["version"] == "3.2.0→3.3.0"
    assert iterlog.artifacts(record["changes"], ["vault://p1/jobs.yaml"]) == ["jobs", "map", "skill:assistant-requirements"]
    assert iterlog.auto_major(["git://main/src/assistant_kernel/core/sql/0008_x.sql"])
    assert not iterlog.auto_major(["vault://p1/docs/a.md"])
    for bad in (GOOD.replace("title: 提问分组\n", ""), GOOD.replace("type: skill", "type: nonsense"),
                "# 实施报告\n没有记录"):
        with pytest.raises(ValueError):
            iterlog.parse_record(bad)


async def test_queries_filter_by_artifact_words_and_significance(kernel):
    async with kernel.db.tx() as c:
        await machine.insert_iteration(c, "p1", None, kind="grant", title="授权 crypto 目录", reason="用户批准",
                                       significance="major", changes=[{"type": "file", "target": "crypto"}])
        await machine.insert_iteration(c, "p1", None, kind="shared_context", title="改技能", reason="提问太散",
                                       changes=[{"type": "skill", "target": "assistant-requirements", "note": "分组"}])
    assert [r["title"] for r in await kernel.iterations("p1", artifact="skill:assistant-requirements")] == ["改技能"]
    assert [r["title"] for r in await kernel.iterations("p1", artifact="skill:*")] == ["改技能"]
    assert [r["title"] for r in await kernel.iterations("p1", major_only=True)] == ["授权 crypto 目录"]
    assert [r["title"] for r in await kernel.iterations("p1", query="分组")] == ["改技能"]
    text = iterlog.render(await kernel.iterations("p1"))
    assert "【重大】授权 crypto 目录" in text and "改动：skill assistant-requirements" in text


async def test_a_cancelled_task_gets_a_plain_record_once(kernel, sim):
    task_id = (await kernel.receive("p1", role="pm", text="做个东西"))["task_id"]
    await kernel.cancel_task(task_id)
    await kernel.cancel_task(task_id)
    records = await kernel.iterations("p1")
    assert len(records) == 1 and records[0]["title"] == "做个东西" and records[0]["complete"]


async def test_effect_reviews_are_written_back(kernel, sim):
    async with kernel.db.tx() as c:
        earlier = await machine.insert_iteration(c, "p1", None, kind="shared_context", title="旧改动", reason="r")
    evolution = await kernel.start_evolution("p1")
    async with kernel.db.tx() as c:   # the review's proposal as the PM executor stores it
        await c.execute("""UPDATE core.tasks SET documents = jsonb_build_object('improvement_proposal',
                           jsonb_build_object('kind', 'improvement_proposal', 'sha256', 'x', 'effect_reviews',
                           jsonb_build_array(jsonb_build_object('iteration', %s::text, 'status', 'effective',
                                                                'note', '回合数降到 2')))) WHERE task_id=%s""",
                        (str(earlier["iteration_id"])[:8], evolution["task_id"]))
    await kernel.cancel_task(evolution["task_id"])
    row = next(r for r in await kernel.iterations("p1") if r["title"] == "旧改动")
    assert row["effect_status"] == "effective" and row["effect_note"] == "回合数降到 2" and row["effect_reviewed_in"]


def test_collaboration_gaps(tmp_path):
    from assistant_kernel.core import skills
    def skill(name, reads, writes):
        folder = tmp_path / "agents" / "pm" / ".agents" / "skills" / name
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: x\nmetadata:\n  version: \"1.0.0\"\n  load: {{roles: [pm]}}\n"
            f"  io:\n    reads: [{', '.join(reads)}]\n    writes: [{', '.join(writes)}]\n---\n# x\n", "utf-8")
    skill("a", ["digest"], ["task_contract"])
    skill("b", ["task_contract", "solution_design"], ["review_report"])
    assert skills.collaboration_gaps(tmp_path) == ["review_report：由 b 写出，但没有技能读取",
                                                   "solution_design：b 要读取，但没有技能写出"]


async def test_review_material_carries_the_stream(kernel, kernel_settings, project_files):
    from assistant_kernel.core.executors.digest import evolution_material
    async with kernel.db.tx() as c:
        await machine.insert_iteration(c, "p1", None, kind="shared_context", title="近期改动", reason="r",
                                       expected_effect="少一轮", measure="各阶段 PM 回合数")
    text = await evolution_material(kernel, kernel_settings, "p1")
    assert "近 4 周的变更流水" in text and "近期改动" in text and "衡量：各阶段 PM 回合数" in text


def test_map_location_follows_delivery(kernel_settings, project_files):
    from assistant_kernel.core.executors.common import project_map
    (project_files / "knowledge").mkdir(exist_ok=True)
    (project_files / "knowledge" / "map.yaml").write_text(
        "schema_version: project-map.v1\nentries:\n  - {path: knowledge/, kind: directory, purpose: 日报脚本说明在这里}\n",
        "utf-8")
    where, text = project_map(kernel_settings, "p1")
    assert "knowledge/map.yaml" in where and "日报脚本" in text and "`knowledge/`" in text
    repo = project_files.parent / "repo-p1"
    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / "map.yaml").write_text("schema_version: project-map.v1\nentries:\n  - {path: docs/, purpose: 代码地图}\n",
                                            "utf-8")
    (project_files / "project.yaml").write_text(
        f"schema_version: project.v2\nproject_id: p1\nlayout_version: 2\ndelivery: git-repo\nrepository: {repo}\n", "utf-8")
    where, text = project_map(kernel_settings, "p1")
    assert "docs/map.yaml" in where and "代码地图" in text
